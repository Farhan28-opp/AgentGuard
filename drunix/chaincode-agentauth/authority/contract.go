/*
SPDX-License-Identifier: Apache-2.0
*/

package authority

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

// ContractVersion is reported by Info so operators can tell which build is deployed.
const ContractVersion = "1.0.0"

// MaxDepth bounds every ancestor walk (and therefore the cost of a transaction).
const MaxDepth = 16

// MaxTTLSeconds bounds a reservation hold.
const MaxTTLSeconds = 3600

// Error codes. Every rejection is returned as "AGENTAUTH:<CODE>: <detail>" so
// that the bridge and AgentGuard can map it without parsing prose.
const (
	ErrUnauthorizedClient   = "UNAUTHORIZED_CLIENT"
	ErrInvalidArgument      = "INVALID_ARGUMENT"
	ErrNotFound             = "NOT_FOUND"
	ErrAlreadyExists        = "ALREADY_EXISTS"
	ErrMandateInactive      = "MANDATE_INACTIVE"
	ErrCapabilityInactive   = "CAPABILITY_INACTIVE"
	ErrAncestorInactive     = "ANCESTOR_INACTIVE"
	ErrHolderMismatch       = "HOLDER_MISMATCH"
	ErrIssuerMismatch       = "ISSUER_MISMATCH"
	ErrInsufficient         = "INSUFFICIENT_AUTHORITY"
	ErrMandateExceeded      = "MANDATE_EXCEEDED"
	ErrCurrencyMismatch     = "CURRENCY_MISMATCH"
	ErrCategoryMismatch     = "CATEGORY_MISMATCH"
	ErrMerchantDenied       = "MERCHANT_DENIED"
	ErrOutsideWindow        = "OUTSIDE_TIME_WINDOW"
	ErrIdempotencyConflict  = "IDEMPOTENCY_CONFLICT"
	ErrInvalidTransition    = "INVALID_TRANSITION"
	ErrAlreadyCommitted     = "ALREADY_COMMITTED"
	ErrReservationExpired   = "RESERVATION_EXPIRED"
	ErrReservationReleased  = "RESERVATION_RELEASED"
	ErrHierarchyViolation   = "HIERARCHY_VIOLATION"
	ErrConservationViolated = "CONSERVATION_VIOLATION"
)

func rejectf(code, format string, args ...interface{}) error {
	return fmt.Errorf("AGENTAUTH:%s: %s", code, fmt.Sprintf(format, args...))
}

// Contract is the agentauth smart contract.
type Contract struct {
	contractapi.Contract
}

// ── helpers ──────────────────────────────────────────────────────────────────

func mandateKey(id string) string     { return "MANDATE~" + id }
func capabilityKey(id string) string  { return "CAP~" + id }
func reservationKey(id string) string { return "RES~" + id }
func idemKey(capID, key string) string {
	return "IDEM~" + capID + "~" + key
}

func writerMSPs() []string {
	raw := os.Getenv("AGENTAUTH_WRITER_MSPS")
	if strings.TrimSpace(raw) == "" {
		raw = "Org1MSP"
	}
	var out []string
	for _, m := range strings.Split(raw, ",") {
		if m = strings.TrimSpace(m); m != "" {
			out = append(out, m)
		}
	}
	return out
}

// requireWriter enforces the access-control rule: only the AgentGuard operator
// organisation may change authority state.
func requireWriter(ctx contractapi.TransactionContextInterface) error {
	msp, err := ctx.GetClientIdentity().GetMSPID()
	if err != nil {
		return rejectf(ErrUnauthorizedClient, "cannot read client MSP: %v", err)
	}
	for _, allowed := range writerMSPs() {
		if msp == allowed {
			return nil
		}
	}
	return rejectf(ErrUnauthorizedClient, "client MSP %q may not change authority state", msp)
}

func txTime(ctx contractapi.TransactionContextInterface) (int64, error) {
	ts, err := ctx.GetStub().GetTxTimestamp()
	if err != nil {
		return 0, fmt.Errorf("cannot read transaction timestamp: %v", err)
	}
	return ts.GetSeconds(), nil
}

func getJSON(ctx contractapi.TransactionContextInterface, key string, out interface{}) (bool, error) {
	raw, err := ctx.GetStub().GetState(key)
	if err != nil {
		return false, fmt.Errorf("failed to read %s: %v", key, err)
	}
	if raw == nil {
		return false, nil
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return false, fmt.Errorf("corrupt document at %s: %v", key, err)
	}
	return true, nil
}

func putJSON(ctx contractapi.TransactionContextInterface, key string, v interface{}) error {
	raw, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return ctx.GetStub().PutState(key, raw)
}

func (c *Contract) loadMandate(ctx contractapi.TransactionContextInterface, id string) (*Mandate, error) {
	var m Mandate
	ok, err := getJSON(ctx, mandateKey(id), &m)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, rejectf(ErrNotFound, "mandate %s not found", id)
	}
	return &m, nil
}

func (c *Contract) loadCapability(ctx contractapi.TransactionContextInterface, id string) (*Capability, error) {
	var cp Capability
	ok, err := getJSON(ctx, capabilityKey(id), &cp)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, rejectf(ErrNotFound, "capability %s not found", id)
	}
	return &cp, nil
}

func (c *Contract) loadReservation(ctx contractapi.TransactionContextInterface, id string) (*Reservation, error) {
	var r Reservation
	ok, err := getJSON(ctx, reservationKey(id), &r)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, rejectf(ErrNotFound, "reservation %s not found", id)
	}
	return &r, nil
}

func checkConservation(cp *Capability) error {
	if cp.Total < 0 || cp.Unallocated < 0 || cp.Reserved < 0 || cp.Committed < 0 {
		return rejectf(ErrConservationViolated, "capability %s has a negative pool value", cp.ID)
	}
	if cp.Unallocated+cp.Reserved+cp.Committed > cp.Total {
		return rejectf(ErrConservationViolated,
			"capability %s: unallocated %d + reserved %d + committed %d exceeds total %d",
			cp.ID, cp.Unallocated, cp.Reserved, cp.Committed, cp.Total)
	}
	return nil
}

func (c *Contract) saveCapability(ctx contractapi.TransactionContextInterface, cp *Capability) error {
	if err := checkConservation(cp); err != nil {
		return err
	}
	cp.UpdatedTx = ctx.GetStub().GetTxID()
	return putJSON(ctx, capabilityKey(cp.ID), cp)
}

// verifyAuthorityChain checks that the capability, every ancestor and the
// mandate are ACTIVE, and that `now` lies inside every time window on the path.
// This is what stops a descendant from bypassing a revoked ancestor: revoking
// any node blocks everything beneath it, without having to rewrite the subtree.
func (c *Contract) verifyAuthorityChain(ctx contractapi.TransactionContextInterface, cp *Capability, now int64) (*Mandate, error) {
	if cp.Status != StatusActive {
		return nil, rejectf(ErrCapabilityInactive, "capability %s is %s", cp.ID, cp.Status)
	}
	if now < cp.NotBefore || now > cp.NotAfter {
		return nil, rejectf(ErrOutsideWindow, "capability %s is valid from %d to %d (now %d)", cp.ID, cp.NotBefore, cp.NotAfter, now)
	}
	node := cp
	for depth := 0; node.ParentID != ""; depth++ {
		if depth >= MaxDepth {
			return nil, rejectf(ErrHierarchyViolation, "authority chain of %s exceeds depth %d", cp.ID, MaxDepth)
		}
		parent, err := c.loadCapability(ctx, node.ParentID)
		if err != nil {
			return nil, rejectf(ErrHierarchyViolation, "capability %s references missing parent %s", node.ID, node.ParentID)
		}
		if parent.MandateID != cp.MandateID {
			return nil, rejectf(ErrHierarchyViolation, "capability %s and ancestor %s belong to different mandates", cp.ID, parent.ID)
		}
		if parent.Status != StatusActive {
			return nil, rejectf(ErrAncestorInactive, "ancestor capability %s is %s", parent.ID, parent.Status)
		}
		if now < parent.NotBefore || now > parent.NotAfter {
			return nil, rejectf(ErrOutsideWindow, "ancestor capability %s is outside its validity window", parent.ID)
		}
		node = parent
	}
	m, err := c.loadMandate(ctx, cp.MandateID)
	if err != nil {
		return nil, err
	}
	if m.Status != StatusActive {
		return nil, rejectf(ErrMandateInactive, "mandate %s is %s", m.ID, m.Status)
	}
	if now < m.NotBefore || now > m.NotAfter {
		return nil, rejectf(ErrOutsideWindow, "mandate %s is valid from %d to %d (now %d)", m.ID, m.NotBefore, m.NotAfter, now)
	}
	return m, nil
}

// isDescendantOrSelf reports whether capability `id` is `ancestorID` or lies beneath it.
func (c *Contract) isDescendantOrSelf(ctx contractapi.TransactionContextInterface, id, ancestorID string) (bool, error) {
	cur := id
	for depth := 0; depth <= MaxDepth; depth++ {
		if cur == ancestorID {
			return true, nil
		}
		cp, err := c.loadCapability(ctx, cur)
		if err != nil {
			return false, err
		}
		if cp.ParentID == "" {
			return false, nil
		}
		cur = cp.ParentID
	}
	return false, rejectf(ErrHierarchyViolation, "authority chain of %s exceeds depth %d", id, MaxDepth)
}

func parseList(raw string) ([]string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" || raw == "null" {
		return []string{}, nil
	}
	var out []string
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil, rejectf(ErrInvalidArgument, "expected a JSON array of strings: %v", err)
	}
	if out == nil {
		out = []string{}
	}
	return out, nil
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	x := append([]string(nil), a...)
	y := append([]string(nil), b...)
	sort.Strings(x)
	sort.Strings(y)
	for i := range x {
		if x[i] != y[i] {
			return false
		}
	}
	return true
}

func requireNonEmpty(fields map[string]string) error {
	names := make([]string, 0, len(fields))
	for k := range fields {
		names = append(names, k)
	}
	sort.Strings(names)
	for _, k := range names {
		if strings.TrimSpace(fields[k]) == "" {
			return rejectf(ErrInvalidArgument, "%s is required", k)
		}
	}
	return nil
}

// RequestHash is the canonical fingerprint of a reserve request. It is computed
// on-chain (never taken from the client) and binds an idempotency key to the
// exact parameters it was first used with.
func RequestHash(capID, holder string, amount int64, currency, merchant, category string) string {
	canonical := strings.Join([]string{capID, holder, fmt.Sprintf("%d", amount), currency, merchant, category}, "|")
	sum := sha256.Sum256([]byte(canonical))
	return hex.EncodeToString(sum[:])
}

func emit(ctx contractapi.TransactionContextInterface, name string, payload interface{}) {
	raw, err := json.Marshal(payload)
	if err == nil {
		_ = ctx.GetStub().SetEvent("agentauth."+name, raw)
	}
}

// ── read functions ──────────────────────────────────────────────────────────

// Info returns deployment metadata; used by health checks.
func (c *Contract) Info(ctx contractapi.TransactionContextInterface) (*Info, error) {
	now, err := txTime(ctx)
	if err != nil {
		return nil, err
	}
	msp, _ := ctx.GetClientIdentity().GetMSPID()
	return &Info{
		Contract:   "agentauth",
		Version:    ContractVersion,
		TxID:       ctx.GetStub().GetTxID(),
		TxTime:     now,
		ClientMSP:  msp,
		WriterMSPs: strings.Join(writerMSPs(), ","),
	}, nil
}

// GetMandate returns a mandate.
func (c *Contract) GetMandate(ctx contractapi.TransactionContextInterface, id string) (*Mandate, error) {
	return c.loadMandate(ctx, id)
}

// GetCapability returns a capability.
func (c *Contract) GetCapability(ctx contractapi.TransactionContextInterface, id string) (*Capability, error) {
	return c.loadCapability(ctx, id)
}

// GetReservation returns a reservation.
func (c *Contract) GetReservation(ctx contractapi.TransactionContextInterface, id string) (*Reservation, error) {
	return c.loadReservation(ctx, id)
}

// GetAuthorityChain returns a capability, its ancestors (nearest first) and its mandate.
func (c *Contract) GetAuthorityChain(ctx contractapi.TransactionContextInterface, id string) (*AuthorityChain, error) {
	cp, err := c.loadCapability(ctx, id)
	if err != nil {
		return nil, err
	}
	chain := &AuthorityChain{Capability: cp, Ancestors: []*Capability{}}
	node := cp
	for depth := 0; node.ParentID != "" && depth < MaxDepth; depth++ {
		parent, err := c.loadCapability(ctx, node.ParentID)
		if err != nil {
			return nil, err
		}
		chain.Ancestors = append(chain.Ancestors, parent)
		node = parent
	}
	if m, err := c.loadMandate(ctx, cp.MandateID); err == nil {
		chain.Mandate = m
	}
	return chain, nil
}

// ── mandate + root registration ─────────────────────────────────────────────

// RegisterMandate records the user's original authorization. Re-registering an
// identical mandate is a no-op; any difference is rejected.
func (c *Contract) RegisterMandate(ctx contractapi.TransactionContextInterface,
	id, owner, controller, currency string, total, notBefore, notAfter int64) (*Mandate, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	if err := requireNonEmpty(map[string]string{"id": id, "owner": owner, "controller": controller, "currency": currency}); err != nil {
		return nil, err
	}
	if total < 0 {
		return nil, rejectf(ErrInvalidArgument, "mandate total must be >= 0")
	}
	if notAfter <= notBefore {
		return nil, rejectf(ErrInvalidArgument, "mandate notAfter must be after notBefore")
	}
	var existing Mandate
	ok, err := getJSON(ctx, mandateKey(id), &existing)
	if err != nil {
		return nil, err
	}
	if ok {
		if existing.Owner == owner && existing.Controller == controller && existing.Currency == currency &&
			existing.Total == total && existing.NotBefore == notBefore && existing.NotAfter == notAfter {
			return &existing, nil
		}
		return nil, rejectf(ErrAlreadyExists, "mandate %s already exists with different terms", id)
	}
	m := &Mandate{
		DocType: DocMandate, ID: id, Owner: owner, Controller: controller, Currency: currency,
		Total: total, Allocated: 0, NotBefore: notBefore, NotAfter: notAfter, Status: StatusActive,
		CreatedTx: ctx.GetStub().GetTxID(), UpdatedTx: ctx.GetStub().GetTxID(),
	}
	if err := putJSON(ctx, mandateKey(id), m); err != nil {
		return nil, err
	}
	emit(ctx, "MandateRegistered", m)
	return m, nil
}

// RevokeMandate deactivates a mandate. Every capability beneath it is blocked
// from reserving, committing and delegating from the next transaction on.
func (c *Contract) RevokeMandate(ctx contractapi.TransactionContextInterface, id, actor string) (*Mandate, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	m, err := c.loadMandate(ctx, id)
	if err != nil {
		return nil, err
	}
	if actor != m.Owner && actor != m.Controller {
		return nil, rejectf(ErrIssuerMismatch, "only the mandate owner or controller may revoke mandate %s", id)
	}
	if m.Status == StatusRevoked {
		return m, nil
	}
	m.Status = StatusRevoked
	m.UpdatedTx = ctx.GetStub().GetTxID()
	if err := putJSON(ctx, mandateKey(id), m); err != nil {
		return nil, err
	}
	emit(ctx, "MandateRevoked", m)
	return m, nil
}

// RegisterRootCapability carves a root capability out of a mandate. The sum of
// all root capabilities of a mandate can never exceed the mandate total.
func (c *Contract) RegisterRootCapability(ctx contractapi.TransactionContextInterface,
	id, mandateID, holder string, total int64, category, allowlistJSON, denylistJSON string,
	notBefore, notAfter int64, grantHash string) (*Capability, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	if err := requireNonEmpty(map[string]string{"id": id, "mandateId": mandateID, "holder": holder, "category": category}); err != nil {
		return nil, err
	}
	allow, err := parseList(allowlistJSON)
	if err != nil {
		return nil, err
	}
	deny, err := parseList(denylistJSON)
	if err != nil {
		return nil, err
	}
	if total < 0 {
		return nil, rejectf(ErrInvalidArgument, "total must be >= 0")
	}
	if notAfter <= notBefore {
		return nil, rejectf(ErrInvalidArgument, "notAfter must be after notBefore")
	}
	var existing Capability
	ok, err := getJSON(ctx, capabilityKey(id), &existing)
	if err != nil {
		return nil, err
	}
	if ok {
		if existing.ParentID == "" && existing.MandateID == mandateID && existing.Holder == holder &&
			existing.Total == total && existing.Category == category && sameSet(existing.MerchantAllowlist, allow) &&
			sameSet(existing.MerchantDenylist, deny) && existing.NotBefore == notBefore && existing.NotAfter == notAfter {
			return &existing, nil
		}
		return nil, rejectf(ErrAlreadyExists, "capability %s already exists with different terms", id)
	}
	m, err := c.loadMandate(ctx, mandateID)
	if err != nil {
		return nil, err
	}
	if m.Status != StatusActive {
		return nil, rejectf(ErrMandateInactive, "mandate %s is %s", m.ID, m.Status)
	}
	if notBefore < m.NotBefore || notAfter > m.NotAfter {
		return nil, rejectf(ErrOutsideWindow, "root capability window must lie within the mandate window")
	}
	if m.Allocated+total > m.Total {
		return nil, rejectf(ErrMandateExceeded, "mandate %s has %d paise unallocated; root capability asks for %d",
			m.ID, m.Total-m.Allocated, total)
	}
	m.Allocated += total
	m.UpdatedTx = ctx.GetStub().GetTxID()
	cp := &Capability{
		DocType: DocCapability, ID: id, ParentID: "", MandateID: mandateID, Holder: holder, Issuer: "",
		Total: total, Unallocated: total, Category: category, MerchantAllowlist: allow, MerchantDenylist: deny,
		NotBefore: notBefore, NotAfter: notAfter, Depth: 0, Status: StatusActive, GrantHash: grantHash,
		CreatedTx: ctx.GetStub().GetTxID(),
	}
	if err := putJSON(ctx, mandateKey(m.ID), m); err != nil {
		return nil, err
	}
	if err := c.saveCapability(ctx, cp); err != nil {
		return nil, err
	}
	emit(ctx, "RootCapabilityRegistered", cp)
	return cp, nil
}

// ── delegation ──────────────────────────────────────────────────────────────

// Delegate transfers `amount` of the parent's unallocated authority into a new
// child capability. Authority is moved, never minted.
func (c *Contract) Delegate(ctx contractapi.TransactionContextInterface,
	parentID, childID, issuer, holder string, amount int64, category, allowlistJSON, denylistJSON string,
	notBefore, notAfter int64, grantHash string) (*DelegateResult, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	if err := requireNonEmpty(map[string]string{"parentId": parentID, "childId": childID, "issuer": issuer, "holder": holder, "category": category}); err != nil {
		return nil, err
	}
	if amount < 0 {
		return nil, rejectf(ErrInvalidArgument, "amount must be >= 0")
	}
	if holder == issuer {
		return nil, rejectf(ErrHierarchyViolation, "an agent cannot delegate authority to itself")
	}
	if notAfter <= notBefore {
		return nil, rejectf(ErrInvalidArgument, "notAfter must be after notBefore")
	}
	allow, err := parseList(allowlistJSON)
	if err != nil {
		return nil, err
	}
	deny, err := parseList(denylistJSON)
	if err != nil {
		return nil, err
	}
	now, err := txTime(ctx)
	if err != nil {
		return nil, err
	}

	var existing Capability
	ok, err := getJSON(ctx, capabilityKey(childID), &existing)
	if err != nil {
		return nil, err
	}
	if ok {
		// Idempotent replay of an identical delegation (e.g. a retried request).
		if existing.ParentID == parentID && existing.Issuer == issuer && existing.Holder == holder &&
			existing.Category == category && sameSet(existing.MerchantAllowlist, allow) &&
			existing.NotBefore == notBefore && existing.NotAfter == notAfter {
			parent, err := c.loadCapability(ctx, parentID)
			if err != nil {
				return nil, err
			}
			return &DelegateResult{Parent: parent, Child: &existing, Replayed: true}, nil
		}
		return nil, rejectf(ErrAlreadyExists, "capability %s already exists with different terms", childID)
	}

	parent, err := c.loadCapability(ctx, parentID)
	if err != nil {
		return nil, err
	}
	if _, err := c.verifyAuthorityChain(ctx, parent, now); err != nil {
		return nil, err
	}
	if parent.Holder != issuer {
		return nil, rejectf(ErrIssuerMismatch, "agent %s does not hold parent capability %s", issuer, parentID)
	}
	if parent.Depth+1 > MaxDepth {
		return nil, rejectf(ErrHierarchyViolation, "delegation depth limit %d reached", MaxDepth)
	}
	if amount > parent.Unallocated {
		return nil, rejectf(ErrInsufficient, "parent %s has %d paise unallocated; delegation asks for %d",
			parentID, parent.Unallocated, amount)
	}
	if category != parent.Category {
		return nil, rejectf(ErrCategoryMismatch, "child category %q must equal parent category %q", category, parent.Category)
	}
	if notBefore < parent.NotBefore || notAfter > parent.NotAfter {
		return nil, rejectf(ErrOutsideWindow, "child window must lie within the parent window")
	}
	// Scope can only narrow: a child's allowlist must be a subset of a restricted parent's.
	if len(parent.MerchantAllowlist) > 0 {
		if len(allow) == 0 {
			allow = append([]string{}, parent.MerchantAllowlist...)
		}
		for _, mch := range allow {
			if !contains(parent.MerchantAllowlist, mch) {
				return nil, rejectf(ErrMerchantDenied, "merchant %q is outside the parent's allowlist", mch)
			}
		}
	}
	// Denylists are inherited.
	for _, mch := range parent.MerchantDenylist {
		if !contains(deny, mch) {
			deny = append(deny, mch)
		}
	}

	parent.Unallocated -= amount
	child := &Capability{
		DocType: DocCapability, ID: childID, ParentID: parentID, MandateID: parent.MandateID,
		Holder: holder, Issuer: issuer, Total: amount, Unallocated: amount, Category: category,
		MerchantAllowlist: allow, MerchantDenylist: deny, NotBefore: notBefore, NotAfter: notAfter,
		Depth: parent.Depth + 1, Status: StatusActive, GrantHash: grantHash, CreatedTx: ctx.GetStub().GetTxID(),
	}
	if err := c.saveCapability(ctx, parent); err != nil {
		return nil, err
	}
	if err := c.saveCapability(ctx, child); err != nil {
		return nil, err
	}
	emit(ctx, "Delegated", map[string]interface{}{"parent": parentID, "child": childID, "amount": amount, "holder": holder})
	return &DelegateResult{Parent: parent, Child: child}, nil
}

// ── reserve / commit / release ──────────────────────────────────────────────

// Reserve moves `amount` from the capability's unallocated pool into a hold.
// This is where Drunix independently enforces the authority rules.
func (c *Contract) Reserve(ctx contractapi.TransactionContextInterface,
	capabilityID, reservationID, holder string, amount int64, currency, merchant, category, idempotencyKey string,
	ttlSeconds int64) (*ReserveResult, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	if err := requireNonEmpty(map[string]string{"capabilityId": capabilityID, "reservationId": reservationID,
		"holder": holder, "currency": currency, "merchant": merchant, "category": category}); err != nil {
		return nil, err
	}
	if amount <= 0 {
		return nil, rejectf(ErrInvalidArgument, "amount must be > 0 paise")
	}
	if ttlSeconds <= 0 || ttlSeconds > MaxTTLSeconds {
		return nil, rejectf(ErrInvalidArgument, "ttlSeconds must be between 1 and %d", MaxTTLSeconds)
	}
	now, err := txTime(ctx)
	if err != nil {
		return nil, err
	}
	reqHash := RequestHash(capabilityID, holder, amount, currency, merchant, category)

	// Idempotency: the same key with the same parameters returns the original hold;
	// the same key with different parameters is rejected.
	if idempotencyKey != "" {
		var rec IdempotencyRecord
		ok, err := getJSON(ctx, idemKey(capabilityID, idempotencyKey), &rec)
		if err != nil {
			return nil, err
		}
		if ok {
			if rec.RequestHash != reqHash {
				return nil, rejectf(ErrIdempotencyConflict,
					"idempotency key %q was already used on capability %s with different parameters", idempotencyKey, capabilityID)
			}
			res, err := c.loadReservation(ctx, rec.ReservationID)
			if err != nil {
				return nil, err
			}
			cp, err := c.loadCapability(ctx, capabilityID)
			if err != nil {
				return nil, err
			}
			return &ReserveResult{Reservation: res, Capability: cp, Replayed: true}, nil
		}
	}

	var dup Reservation
	exists, err := getJSON(ctx, reservationKey(reservationID), &dup)
	if err != nil {
		return nil, err
	}
	if exists {
		return nil, rejectf(ErrAlreadyExists, "reservation %s already exists", reservationID)
	}

	cp, err := c.loadCapability(ctx, capabilityID)
	if err != nil {
		return nil, err
	}
	m, err := c.verifyAuthorityChain(ctx, cp, now)
	if err != nil {
		return nil, err
	}
	if cp.Holder != holder {
		return nil, rejectf(ErrHolderMismatch, "agent %s does not hold capability %s", holder, capabilityID)
	}
	if currency != m.Currency {
		return nil, rejectf(ErrCurrencyMismatch, "currency %s does not match mandate currency %s", currency, m.Currency)
	}
	if category != cp.Category {
		return nil, rejectf(ErrCategoryMismatch, "category %q is outside capability scope %q", category, cp.Category)
	}
	if len(cp.MerchantAllowlist) > 0 && !contains(cp.MerchantAllowlist, merchant) {
		return nil, rejectf(ErrMerchantDenied, "merchant %q is not in the capability allowlist", merchant)
	}
	if contains(cp.MerchantDenylist, merchant) {
		return nil, rejectf(ErrMerchantDenied, "merchant %q is denylisted", merchant)
	}
	if amount > cp.Unallocated {
		return nil, rejectf(ErrInsufficient, "capability %s has %d paise unallocated; reserve asks for %d",
			capabilityID, cp.Unallocated, amount)
	}

	cp.Unallocated -= amount
	cp.Reserved += amount
	res := &Reservation{
		DocType: DocReservation, ID: reservationID, CapabilityID: capabilityID, Holder: holder, Amount: amount,
		Currency: currency, Merchant: merchant, Category: category, Status: ResReserved,
		IdempotencyKey: idempotencyKey, RequestHash: reqHash, CreatedAt: now, ExpiresAt: now + ttlSeconds,
		ReserveTx: ctx.GetStub().GetTxID(),
	}
	if err := c.saveCapability(ctx, cp); err != nil {
		return nil, err
	}
	if err := putJSON(ctx, reservationKey(reservationID), res); err != nil {
		return nil, err
	}
	if idempotencyKey != "" {
		rec := &IdempotencyRecord{DocType: DocIdempotency, CapabilityID: capabilityID, Key: idempotencyKey,
			ReservationID: reservationID, RequestHash: reqHash}
		if err := putJSON(ctx, idemKey(capabilityID, idempotencyKey), rec); err != nil {
			return nil, err
		}
	}
	emit(ctx, "Reserved", map[string]interface{}{"reservation": reservationID, "capability": capabilityID, "amount": amount})
	return &ReserveResult{Reservation: res, Capability: cp}, nil
}

// Commit settles a hold: reserved -> committed. A second commit is rejected.
func (c *Contract) Commit(ctx contractapi.TransactionContextInterface, reservationID, holder, paymentRef string) (*CommitResult, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	if err := requireNonEmpty(map[string]string{"reservationId": reservationID, "holder": holder}); err != nil {
		return nil, err
	}
	now, err := txTime(ctx)
	if err != nil {
		return nil, err
	}
	res, err := c.loadReservation(ctx, reservationID)
	if err != nil {
		return nil, err
	}
	switch res.Status {
	case ResReserved:
	case ResCommitted:
		return nil, rejectf(ErrAlreadyCommitted, "reservation %s was already committed in tx %s", reservationID, res.CommitTx)
	case ResReleased:
		return nil, rejectf(ErrReservationReleased, "reservation %s was released (%s)", reservationID, res.ReleaseReason)
	default:
		return nil, rejectf(ErrInvalidTransition, "reservation %s is %s", reservationID, res.Status)
	}
	if now >= res.ExpiresAt {
		return nil, rejectf(ErrReservationExpired, "reservation %s expired at %d (now %d)", reservationID, res.ExpiresAt, now)
	}
	cp, err := c.loadCapability(ctx, res.CapabilityID)
	if err != nil {
		return nil, err
	}
	if cp.Holder != holder || res.Holder != holder {
		return nil, rejectf(ErrHolderMismatch, "agent %s does not hold the capability backing reservation %s", holder, reservationID)
	}
	if _, err := c.verifyAuthorityChain(ctx, cp, now); err != nil {
		return nil, err
	}
	if cp.Reserved < res.Amount {
		return nil, rejectf(ErrConservationViolated, "capability %s reserved pool %d is below hold %d", cp.ID, cp.Reserved, res.Amount)
	}
	cp.Reserved -= res.Amount
	cp.Committed += res.Amount
	res.Status = ResCommitted
	res.CommitTx = ctx.GetStub().GetTxID()
	res.CommittedAt = now
	res.PaymentRef = paymentRef
	if err := c.saveCapability(ctx, cp); err != nil {
		return nil, err
	}
	if err := putJSON(ctx, reservationKey(reservationID), res); err != nil {
		return nil, err
	}
	emit(ctx, "Committed", map[string]interface{}{"reservation": reservationID, "capability": cp.ID, "amount": res.Amount})
	return &CommitResult{Reservation: res, Capability: cp}, nil
}

// Release returns a hold to the unallocated pool. Releasing an already released
// hold is a no-op; releasing a committed hold is rejected.
func (c *Contract) Release(ctx contractapi.TransactionContextInterface, reservationID, actor, reason string) (*ReleaseResult, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	if err := requireNonEmpty(map[string]string{"reservationId": reservationID, "actor": actor}); err != nil {
		return nil, err
	}
	now, err := txTime(ctx)
	if err != nil {
		return nil, err
	}
	res, err := c.loadReservation(ctx, reservationID)
	if err != nil {
		return nil, err
	}
	cp, err := c.loadCapability(ctx, res.CapabilityID)
	if err != nil {
		return nil, err
	}
	switch res.Status {
	case ResReleased:
		return &ReleaseResult{Reservation: res, Capability: cp, AlreadyReleased: true}, nil
	case ResCommitted:
		return nil, rejectf(ErrAlreadyCommitted, "reservation %s is committed and cannot be released", reservationID)
	case ResReserved:
	default:
		return nil, rejectf(ErrInvalidTransition, "reservation %s is %s", reservationID, res.Status)
	}
	if cp.Reserved < res.Amount {
		return nil, rejectf(ErrConservationViolated, "capability %s reserved pool %d is below hold %d", cp.ID, cp.Reserved, res.Amount)
	}
	cp.Reserved -= res.Amount
	cp.Unallocated += res.Amount
	res.Status = ResReleased
	res.ReleaseTx = ctx.GetStub().GetTxID()
	res.ReleasedAt = now
	if reason == "" {
		reason = "released by " + actor
	}
	res.ReleaseReason = reason
	if err := c.saveCapability(ctx, cp); err != nil {
		return nil, err
	}
	if err := putJSON(ctx, reservationKey(reservationID), res); err != nil {
		return nil, err
	}
	emit(ctx, "Released", map[string]interface{}{"reservation": reservationID, "capability": cp.ID, "amount": res.Amount})
	return &ReleaseResult{Reservation: res, Capability: cp}, nil
}

// ── attenuation + revocation ────────────────────────────────────────────────

// ReturnUnused hands a child capability's unallocated authority back to its
// parent (only the issuer may do this, and only when nothing is in flight).
func (c *Contract) ReturnUnused(ctx contractapi.TransactionContextInterface, capabilityID, issuer string) (*ReturnResult, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	if err := requireNonEmpty(map[string]string{"capabilityId": capabilityID, "issuer": issuer}); err != nil {
		return nil, err
	}
	child, err := c.loadCapability(ctx, capabilityID)
	if err != nil {
		return nil, err
	}
	if child.ParentID == "" {
		return nil, rejectf(ErrHierarchyViolation, "a root capability has no parent to return authority to")
	}
	if child.Issuer != issuer {
		return nil, rejectf(ErrIssuerMismatch, "only the issuing agent may take back unused authority")
	}
	if child.Status != StatusActive {
		return nil, rejectf(ErrCapabilityInactive, "capability %s is %s; its authority cannot be returned", child.ID, child.Status)
	}
	if child.Reserved > 0 {
		return nil, rejectf(ErrInvalidTransition, "capability %s still has %d paise reserved", child.ID, child.Reserved)
	}
	parent, err := c.loadCapability(ctx, child.ParentID)
	if err != nil {
		return nil, err
	}
	amount := child.Unallocated
	if amount > 0 {
		child.Unallocated = 0
		child.Total -= amount
		parent.Unallocated += amount
	}
	if child.Unallocated == 0 && child.Reserved == 0 && child.Delegated() == 0 {
		child.Status = StatusExhausted
	}
	if err := c.saveCapability(ctx, parent); err != nil {
		return nil, err
	}
	if err := c.saveCapability(ctx, child); err != nil {
		return nil, err
	}
	emit(ctx, "AuthorityReturned", map[string]interface{}{"capability": child.ID, "parent": parent.ID, "amount": amount})
	return &ReturnResult{Parent: parent, Child: child, Returned: amount}, nil
}

// Revoke marks a capability REVOKED, marks the listed descendants REVOKED and
// releases the listed in-flight holds within the subtree.
//
// Even if a caller omits descendants, they are blocked: every Reserve, Commit
// and Delegate walks the full ancestor chain and refuses to act beneath a
// revoked node. Committed authority stays committed; unallocated authority of a
// revoked capability stays frozen (it is never returned to the parent).
func (c *Contract) Revoke(ctx contractapi.TransactionContextInterface,
	capabilityID, actor, descendantIDsJSON, reservationIDsJSON, reason string) (*RevokeResult, error) {
	if err := requireWriter(ctx); err != nil {
		return nil, err
	}
	if err := requireNonEmpty(map[string]string{"capabilityId": capabilityID, "actor": actor}); err != nil {
		return nil, err
	}
	descendants, err := parseList(descendantIDsJSON)
	if err != nil {
		return nil, err
	}
	resIDs, err := parseList(reservationIDsJSON)
	if err != nil {
		return nil, err
	}
	now, err := txTime(ctx)
	if err != nil {
		return nil, err
	}
	target, err := c.loadCapability(ctx, capabilityID)
	if err != nil {
		return nil, err
	}
	if target.ParentID != "" {
		if target.Issuer != actor {
			return nil, rejectf(ErrIssuerMismatch, "only the issuer of %s may revoke it", capabilityID)
		}
	} else {
		// A root capability may be revoked by the mandate owner, the mandate
		// controller (AgentGuard's system agent, e.g. for containment) or its
		// own holder renouncing it (AgentGuard re-issuing authority).
		m, err := c.loadMandate(ctx, target.MandateID)
		if err != nil {
			return nil, err
		}
		if actor != m.Controller && actor != m.Owner && actor != target.Holder {
			return nil, rejectf(ErrIssuerMismatch, "root capability revocation requires the mandate owner, controller or the root holder")
		}
		// Mirrors AgentGuard: a revoked root no longer counts against the
		// mandate total (its unspent authority is frozen, never re-usable).
		if target.Status != StatusRevoked {
			m.Allocated -= target.Total
			if m.Allocated < 0 {
				m.Allocated = 0
			}
			m.UpdatedTx = ctx.GetStub().GetTxID()
			if err := putJSON(ctx, mandateKey(m.ID), m); err != nil {
				return nil, err
			}
		}
	}

	out := &RevokeResult{CapabilityID: capabilityID, RevokedCapabilities: []string{}, ReleasedReservations: []string{}, Skipped: []string{}}
	caps := map[string]*Capability{target.ID: target}

	// Release holds first (they may belong to any capability in the subtree).
	for _, rid := range resIDs {
		var res Reservation
		found, err := getJSON(ctx, reservationKey(rid), &res)
		if err != nil {
			return nil, err
		}
		if !found {
			// Never written to the ledger (for example a labelled simulation
			// hold that only exists off-chain): nothing to release.
			out.Skipped = append(out.Skipped, rid)
			continue
		}
		inSubtree, err := c.isDescendantOrSelf(ctx, res.CapabilityID, capabilityID)
		if err != nil {
			return nil, err
		}
		if !inSubtree {
			return nil, rejectf(ErrHierarchyViolation, "reservation %s is outside the revoked subtree", rid)
		}
		if res.Status != ResReserved {
			continue
		}
		cp, ok := caps[res.CapabilityID]
		if !ok {
			cp, err = c.loadCapability(ctx, res.CapabilityID)
			if err != nil {
				return nil, err
			}
			caps[cp.ID] = cp
		}
		cp.Reserved -= res.Amount
		cp.Unallocated += res.Amount
		res.Status = ResReleased
		res.ReleaseTx = ctx.GetStub().GetTxID()
		res.ReleasedAt = now
		res.ReleaseReason = "capability revoked: " + reason
		if err := putJSON(ctx, reservationKey(rid), &res); err != nil {
			return nil, err
		}
		out.ReleasedReservations = append(out.ReleasedReservations, rid)
		out.ReleasedAmount += res.Amount
	}

	ids := append([]string{capabilityID}, descendants...)
	for _, id := range ids {
		cp, ok := caps[id]
		if !ok {
			var probe Capability
			present, err := getJSON(ctx, capabilityKey(id), &probe)
			if err != nil {
				return nil, err
			}
			if !present {
				out.Skipped = append(out.Skipped, id)
				continue
			}
			ok2, err := c.isDescendantOrSelf(ctx, id, capabilityID)
			if err != nil {
				return nil, err
			}
			if !ok2 {
				return nil, rejectf(ErrHierarchyViolation, "capability %s is not beneath %s", id, capabilityID)
			}
			cp, err = c.loadCapability(ctx, id)
			if err != nil {
				return nil, err
			}
			caps[id] = cp
		}
		if cp.Status != StatusRevoked {
			cp.Status = StatusRevoked
			out.RevokedCapabilities = append(out.RevokedCapabilities, id)
		}
	}
	keys := make([]string, 0, len(caps))
	for k := range caps {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if err := c.saveCapability(ctx, caps[k]); err != nil {
			return nil, err
		}
	}
	emit(ctx, "Revoked", out)
	return out, nil
}
