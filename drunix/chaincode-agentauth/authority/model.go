/*
SPDX-License-Identifier: Apache-2.0

agentauth — on-chain financial-authority ledger for AgentGuard, running on Drunix.

Every monetary value is an integer number of paise (1 INR = 100 paise); there is
no floating point anywhere in this package. Every time comparison uses the
transaction timestamp (ctx.GetStub().GetTxTimestamp()) so that all endorsing
peers compute the same result.

Drunix's SQL state database stores the value of every public key in a JSONB
column, so every document written by this chaincode — including index entries —
is a JSON object.
*/

package authority

// Document types (stored in the docType field of every JSON document).
const (
	DocMandate     = "mandate"
	DocCapability  = "capability"
	DocReservation = "reservation"
	DocIdempotency = "idempotency"
	DocMeta        = "meta"
)

// Status values.
const (
	StatusActive    = "ACTIVE"
	StatusRevoked   = "REVOKED"
	StatusExhausted = "EXHAUSTED"

	ResReserved  = "RESERVED"
	ResCommitted = "COMMITTED"
	ResReleased  = "RELEASED"
)

// Mandate is the user's original authorization: the root of the authority tree.
type Mandate struct {
	DocType    string `json:"docType"`
	ID         string `json:"id"`
	Owner      string `json:"owner"`      // user id
	Controller string `json:"controller"` // agent allowed to revoke root capabilities (AgentGuard system agent)
	Currency   string `json:"currency"`
	Total      int64  `json:"total"`     // paise
	Allocated  int64  `json:"allocated"` // paise carved into root capabilities
	NotBefore  int64  `json:"notBefore"` // unix seconds
	NotAfter   int64  `json:"notAfter"`  // unix seconds
	Status     string `json:"status"`
	CreatedTx  string `json:"createdTx"`
	UpdatedTx  string `json:"updatedTx"`
}

// Capability is a delegated pool of financial authority.
//
// Conservation invariant (checked on every write):
//
//	total >= unallocated + reserved + committed >= 0
//
// and total - (unallocated + reserved + committed) is the authority currently
// delegated to children.
type Capability struct {
	DocType           string   `json:"docType"`
	ID                string   `json:"id"`
	ParentID          string   `json:"parentId"` // "" for a root capability
	MandateID         string   `json:"mandateId"`
	Holder            string   `json:"holder"` // agent id the authority is issued to
	Issuer            string   `json:"issuer"` // agent id that delegated it ("" for root)
	Total             int64    `json:"total"`
	Unallocated       int64    `json:"unallocated"`
	Reserved          int64    `json:"reserved"`
	Committed         int64    `json:"committed"`
	Category          string   `json:"category"`
	MerchantAllowlist []string `json:"merchantAllowlist"`
	MerchantDenylist  []string `json:"merchantDenylist"`
	NotBefore         int64    `json:"notBefore"`
	NotAfter          int64    `json:"notAfter"`
	Depth             int      `json:"depth"`
	Status            string   `json:"status"`
	GrantHash         string   `json:"grantHash"` // sha256 of AgentGuard's Ed25519 grant signature (provenance only)
	CreatedTx         string   `json:"createdTx"`
	UpdatedTx         string   `json:"updatedTx"`
}

// Delegated returns the authority currently held by this capability's children.
func (c *Capability) Delegated() int64 {
	return c.Total - c.Unallocated - c.Reserved - c.Committed
}

// Reservation is an authority hold against a capability.
type Reservation struct {
	DocType        string `json:"docType"`
	ID             string `json:"id"`
	CapabilityID   string `json:"capabilityId"`
	Holder         string `json:"holder"`
	Amount         int64  `json:"amount"`
	Currency       string `json:"currency"`
	Merchant       string `json:"merchant"`
	Category       string `json:"category"`
	Status         string `json:"status"`
	IdempotencyKey string `json:"idempotencyKey"`
	RequestHash    string `json:"requestHash"`
	CreatedAt      int64  `json:"createdAt"`
	ExpiresAt      int64  `json:"expiresAt"`
	ReserveTx      string `json:"reserveTx"`
	CommitTx       string `json:"commitTx"`
	CommittedAt    int64  `json:"committedAt"`
	PaymentRef     string `json:"paymentRef"`
	ReleaseTx      string `json:"releaseTx"`
	ReleasedAt     int64  `json:"releasedAt"`
	ReleaseReason  string `json:"releaseReason"`
}

// IdempotencyRecord maps (capability, idempotency key) to the reservation it created.
type IdempotencyRecord struct {
	DocType       string `json:"docType"`
	CapabilityID  string `json:"capabilityId"`
	Key           string `json:"key"`
	ReservationID string `json:"reservationId"`
	RequestHash   string `json:"requestHash"`
}

// ReserveResult is returned by Reserve. Replayed is true when the call matched
// an existing idempotency key with identical parameters (no state changed).
type ReserveResult struct {
	Reservation *Reservation `json:"reservation"`
	Capability  *Capability  `json:"capability"`
	Replayed    bool         `json:"replayed"`
}

// CommitResult is returned by Commit.
type CommitResult struct {
	Reservation *Reservation `json:"reservation"`
	Capability  *Capability  `json:"capability"`
}

// ReleaseResult is returned by Release. AlreadyReleased is true for an idempotent repeat.
type ReleaseResult struct {
	Reservation     *Reservation `json:"reservation"`
	Capability      *Capability  `json:"capability"`
	AlreadyReleased bool         `json:"alreadyReleased"`
}

// DelegateResult is returned by Delegate.
type DelegateResult struct {
	Parent   *Capability `json:"parent"`
	Child    *Capability `json:"child"`
	Replayed bool        `json:"replayed"`
}

// ReturnResult is returned by ReturnUnused.
type ReturnResult struct {
	Parent   *Capability `json:"parent"`
	Child    *Capability `json:"child"`
	Returned int64       `json:"returned"`
}

// RevokeResult is returned by Revoke.
type RevokeResult struct {
	CapabilityID         string   `json:"capabilityId"`
	RevokedCapabilities  []string `json:"revokedCapabilities"`
	ReleasedReservations []string `json:"releasedReservations"`
	ReleasedAmount       int64    `json:"releasedAmount"`
	Skipped              []string `json:"skipped"` // ids that were never on the ledger
}

// AuthorityChain is a capability together with its ancestors (nearest first) and mandate.
type AuthorityChain struct {
	Capability *Capability   `json:"capability"`
	Ancestors  []*Capability `json:"ancestors"`
	Mandate    *Mandate      `json:"mandate"`
}

// Info describes the deployed contract (used by health checks).
type Info struct {
	Contract   string `json:"contract"`
	Version    string `json:"version"`
	TxID       string `json:"txId"`
	TxTime     int64  `json:"txTime"`
	ClientMSP  string `json:"clientMsp"`
	WriterMSPs string `json:"writerMsps"`
}
