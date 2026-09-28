package authority

import (
	"encoding/json"
	"math/rand"
	"strings"
	"testing"
)

const (
	now      = int64(1_800_000_000)
	day      = int64(86_400)
	main     = "agent-main"
	purchase = "agent-purchase"
	system   = "agent-system"
	user     = "user-1"
)

func mustCode(t *testing.T, err error, code string) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected rejection %s, got success", code)
	}
	if !strings.Contains(err.Error(), "AGENTAUTH:"+code+":") {
		t.Fatalf("expected rejection %s, got %v", code, err)
	}
}

func mustOK(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

// setup: mandate ₹10,000 → root (Main Agent) ₹10,000 → purchase capability ₹3,000.
func setup(t *testing.T) *harness {
	h := newHarness(t)
	_, err := tx(h, func() (*Mandate, error) {
		return h.c.RegisterMandate(h.ctx, "M1", user, system, "INR", 1_000_000, now-day, now+30*day)
	})
	mustOK(t, err)
	_, err = tx(h, func() (*Capability, error) {
		return h.c.RegisterRootCapability(h.ctx, "ROOT", "M1", main, 1_000_000, "groceries",
			`["quickkart","freshbasket","dailymart"]`, `[]`, now-day, now+30*day, "grant-root")
	})
	mustOK(t, err)
	_, err = tx(h, func() (*DelegateResult, error) {
		return h.c.Delegate(h.ctx, "ROOT", "PC1", main, purchase, 300_000, "groceries", `["quickkart","freshbasket"]`, `[]`,
			now-60, now+day, "grant-pc1")
	})
	mustOK(t, err)
	return h
}

func (h *harness) cap(id string) *Capability {
	h.t.Helper()
	cp, err := tx(h, func() (*Capability, error) { return h.c.GetCapability(h.ctx, id) })
	mustOK(h.t, err)
	return cp
}

func (h *harness) res(id string) *Reservation {
	h.t.Helper()
	r, err := tx(h, func() (*Reservation, error) { return h.c.GetReservation(h.ctx, id) })
	mustOK(h.t, err)
	return r
}

func (h *harness) reserve(cap, res, holder string, amount int64, merchant, key string) (*ReserveResult, error) {
	return tx(h, func() (*ReserveResult, error) {
		return h.c.Reserve(h.ctx, cap, res, holder, amount, "INR", merchant, "groceries", key, 90)
	})
}

func (h *harness) commit(res, holder string) (*CommitResult, error) {
	return tx(h, func() (*CommitResult, error) { return h.c.Commit(h.ctx, res, holder, "SIMREF") })
}

func (h *harness) release(res string) (*ReleaseResult, error) {
	return tx(h, func() (*ReleaseResult, error) { return h.c.Release(h.ctx, res, purchase, "test") })
}

func (h *harness) revoke(cap, actor, desc, res string) (*RevokeResult, error) {
	return tx(h, func() (*RevokeResult, error) { return h.c.Revoke(h.ctx, cap, actor, desc, res, "test") })
}

// ── mandate + root ───────────────────────────────────────────────────────────

func TestRegisterMandateIdempotentAndConflict(t *testing.T) {
	h := setup(t)
	m, err := tx(h, func() (*Mandate, error) {
		return h.c.RegisterMandate(h.ctx, "M1", user, system, "INR", 1_000_000, now-day, now+30*day)
	})
	mustOK(t, err)
	if m.Allocated != 1_000_000 {
		t.Fatalf("identical re-registration must return existing mandate, got allocated %d", m.Allocated)
	}
	_, err = tx(h, func() (*Mandate, error) {
		return h.c.RegisterMandate(h.ctx, "M1", user, system, "INR", 2_000_000, now-day, now+30*day)
	})
	mustCode(t, err, ErrAlreadyExists)
	_, err = tx(h, func() (*Mandate, error) {
		return h.c.RegisterMandate(h.ctx, "M2", user, system, "INR", -1, now-day, now+day)
	})
	mustCode(t, err, ErrInvalidArgument)
	_, err = tx(h, func() (*Mandate, error) {
		return h.c.RegisterMandate(h.ctx, "M3", user, system, "INR", 10, now, now-1)
	})
	mustCode(t, err, ErrInvalidArgument)
}

func TestRootCapabilityCannotExceedMandate(t *testing.T) {
	h := setup(t)
	_, err := tx(h, func() (*Capability, error) {
		return h.c.RegisterRootCapability(h.ctx, "ROOT2", "M1", main, 1, "groceries", `[]`, `[]`, now-day, now+day, "")
	})
	mustCode(t, err, ErrMandateExceeded)
	_, err = tx(h, func() (*Capability, error) {
		return h.c.RegisterRootCapability(h.ctx, "ROOT3", "NOPE", main, 1, "groceries", `[]`, `[]`, now-day, now+day, "")
	})
	mustCode(t, err, ErrNotFound)
	_, err = tx(h, func() (*Mandate, error) {
		return h.c.RegisterMandate(h.ctx, "M9", user, system, "INR", 500, now-day, now+day)
	})
	mustOK(t, err)
	_, err = tx(h, func() (*Capability, error) {
		return h.c.RegisterRootCapability(h.ctx, "ROOT4", "M9", main, 100, "groceries", `[]`, `[]`, now-2*day, now+day, "")
	})
	mustCode(t, err, ErrOutsideWindow)
}

func TestUnauthorizedMSPCannotWrite(t *testing.T) {
	h := setup(t)
	h.ctx.id = fakeIdentity{msp: "Org2MSP"}
	_, err := h.reserve("PC1", "R1", purchase, 1000, "quickkart", "k1")
	mustCode(t, err, ErrUnauthorizedClient)
	// reads are allowed
	h.cap("PC1")
}

// ── delegation ───────────────────────────────────────────────────────────────

func TestDelegateMovesAuthority(t *testing.T) {
	h := setup(t)
	root, pc := h.cap("ROOT"), h.cap("PC1")
	if root.Unallocated != 700_000 || root.Delegated() != 300_000 {
		t.Fatalf("root pool wrong: %+v", root)
	}
	if pc.Total != 300_000 || pc.Unallocated != 300_000 || pc.Depth != 1 || pc.Issuer != main {
		t.Fatalf("child wrong: %+v", pc)
	}
	// identical replay is idempotent
	r, err := tx(h, func() (*DelegateResult, error) {
		return h.c.Delegate(h.ctx, "ROOT", "PC1", main, purchase, 300_000, "groceries", `["quickkart","freshbasket"]`, `[]`, now-60, now+day, "")
	})
	mustOK(t, err)
	if !r.Replayed || h.cap("ROOT").Unallocated != 700_000 {
		t.Fatalf("replay must not move authority twice")
	}
}

func TestDelegateRejections(t *testing.T) {
	h := setup(t)
	cases := []struct {
		name, parent, child, issuer, holder string
		amount                              int64
		category, allow                     string
		nb, na                              int64
		code                                string
	}{
		{"insufficient", "ROOT", "X1", main, purchase, 700_001, "groceries", `[]`, now - 60, now + day, ErrInsufficient},
		{"issuer does not hold parent", "ROOT", "X2", purchase, "agent-x", 100, "groceries", `[]`, now - 60, now + day, ErrIssuerMismatch},
		{"self delegation", "ROOT", "X3", main, main, 100, "groceries", `[]`, now - 60, now + day, ErrHierarchyViolation},
		{"category escalation", "ROOT", "X4", main, purchase, 100, "electronics", `[]`, now - 60, now + day, ErrCategoryMismatch},
		{"merchant escalation", "ROOT", "X5", main, purchase, 100, "groceries", `["amazon"]`, now - 60, now + day, ErrMerchantDenied},
		{"window escalation", "ROOT", "X6", main, purchase, 100, "groceries", `[]`, now - 60, now + 60*day, ErrOutsideWindow},
		{"missing parent", "NOPE", "X7", main, purchase, 100, "groceries", `[]`, now - 60, now + day, ErrNotFound},
		{"existing child different terms", "ROOT", "PC1", main, purchase, 5, "groceries", `[]`, now - 60, now + 2*day, ErrAlreadyExists},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := tx(h, func() (*DelegateResult, error) {
				return h.c.Delegate(h.ctx, tc.parent, tc.child, tc.issuer, tc.holder, tc.amount, tc.category, tc.allow, `[]`, tc.nb, tc.na, "")
			})
			mustCode(t, err, tc.code)
		})
	}
	if h.cap("ROOT").Unallocated != 700_000 {
		t.Fatalf("rejected delegations must not move authority")
	}
}

func TestDelegateFromRevokedParentRejected(t *testing.T) {
	h := setup(t)
	_, err := h.revoke("PC1", main, `[]`, `[]`)
	mustOK(t, err)
	_, err = tx(h, func() (*DelegateResult, error) {
		return h.c.Delegate(h.ctx, "PC1", "GC1", purchase, "agent-sub", 100, "groceries", `[]`, `[]`, now-60, now+day, "")
	})
	mustCode(t, err, ErrCapabilityInactive)
}

// ── reserve ──────────────────────────────────────────────────────────────────

func TestReserveMovesUnallocatedToReserved(t *testing.T) {
	h := setup(t)
	r, err := h.reserve("PC1", "R1", purchase, 245_000, "quickkart", "k1")
	mustOK(t, err)
	if r.Replayed || r.Reservation.Status != ResReserved || r.Reservation.ExpiresAt != now+90 {
		t.Fatalf("bad reservation: %+v", r.Reservation)
	}
	pc := h.cap("PC1")
	if pc.Unallocated != 55_000 || pc.Reserved != 245_000 {
		t.Fatalf("pool not moved: %+v", pc)
	}
	if r.Reservation.RequestHash != RequestHash("PC1", purchase, 245_000, "INR", "quickkart", "groceries") {
		t.Fatalf("request hash must be computed on-chain")
	}
}

func TestReserveRejections(t *testing.T) {
	h := setup(t)
	cases := []struct {
		name, cap, holder, merchant, currency, category string
		amount                                          int64
		code                                            string
	}{
		{"over limit", "PC1", purchase, "quickkart", "INR", "groceries", 300_001, ErrInsufficient},
		{"wrong holder", "PC1", main, "quickkart", "INR", "groceries", 100, ErrHolderMismatch},
		{"merchant not allowlisted", "PC1", purchase, "dailymart", "INR", "groceries", 100, ErrMerchantDenied},
		{"category violation", "PC1", purchase, "quickkart", "INR", "electronics", 100, ErrCategoryMismatch},
		{"currency mismatch", "PC1", purchase, "quickkart", "USD", "groceries", 100, ErrCurrencyMismatch},
		{"zero amount", "PC1", purchase, "quickkart", "INR", "groceries", 0, ErrInvalidArgument},
		{"negative amount", "PC1", purchase, "quickkart", "INR", "groceries", -5, ErrInvalidArgument},
		{"unknown capability", "NOPE", purchase, "quickkart", "INR", "groceries", 100, ErrNotFound},
	}
	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := tx(h, func() (*ReserveResult, error) {
				return h.c.Reserve(h.ctx, tc.cap, "RX"+string(rune('a'+i)), tc.holder, tc.amount, tc.currency, tc.merchant, tc.category, "", 90)
			})
			mustCode(t, err, tc.code)
		})
	}
	if pc := h.cap("PC1"); pc.Unallocated != 300_000 || pc.Reserved != 0 {
		t.Fatalf("rejected reserves must not move authority: %+v", pc)
	}
}

func TestReserveOutsideTimeWindow(t *testing.T) {
	h := setup(t)
	h.ctx.stub.now = now + 2*day // purchase capability expires at now+day
	_, err := h.reserve("PC1", "R1", purchase, 100, "quickkart", "")
	mustCode(t, err, ErrOutsideWindow)
	h.ctx.stub.now = now - 3600 // before notBefore
	_, err = h.reserve("PC1", "R2", purchase, 100, "quickkart", "")
	mustCode(t, err, ErrOutsideWindow)
}

func TestReserveBlockedByRevokedCapabilityAncestorAndMandate(t *testing.T) {
	h := setup(t)
	// A grandchild beneath the purchase capability.
	_, err := tx(h, func() (*DelegateResult, error) {
		return h.c.Delegate(h.ctx, "PC1", "GC1", purchase, "agent-sub", 50_000, "groceries", `["quickkart"]`, `[]`, now-60, now+day, "")
	})
	mustOK(t, err)
	// Revoke the ROOT only, without listing descendants: they must still be blocked.
	_, err = h.revoke("ROOT", system, `[]`, `[]`)
	mustOK(t, err)
	_, err = h.reserve("GC1", "R1", "agent-sub", 100, "quickkart", "")
	mustCode(t, err, ErrAncestorInactive)
	_, err = h.reserve("PC1", "R2", purchase, 100, "quickkart", "")
	mustCode(t, err, ErrAncestorInactive)
	_, err = h.reserve("ROOT", "R3", main, 100, "quickkart", "")
	mustCode(t, err, ErrCapabilityInactive)

	h2 := setup(t)
	_, err = tx(h2, func() (*Mandate, error) { return h2.c.RevokeMandate(h2.ctx, "M1", user) })
	mustOK(t, err)
	_, err = h2.reserve("PC1", "R1", purchase, 100, "quickkart", "")
	mustCode(t, err, ErrMandateInactive)
	_, err = tx(h2, func() (*Mandate, error) { return h2.c.RevokeMandate(h2.ctx, "M1", purchase) })
	mustCode(t, err, ErrIssuerMismatch)
}

func TestReserveIdempotency(t *testing.T) {
	h := setup(t)
	_, err := h.reserve("PC1", "R1", purchase, 100_000, "quickkart", "order-42")
	mustOK(t, err)
	// same key, same parameters (even a different reservation id) → the original hold
	again, err := h.reserve("PC1", "R1-retry", purchase, 100_000, "quickkart", "order-42")
	mustOK(t, err)
	if !again.Replayed || again.Reservation.ID != "R1" {
		t.Fatalf("expected idempotent replay of R1, got %+v", again)
	}
	if pc := h.cap("PC1"); pc.Reserved != 100_000 {
		t.Fatalf("replay must not reserve twice: %+v", pc)
	}
	// same key, different amount / merchant → rejected
	_, err = h.reserve("PC1", "R2", purchase, 100_001, "quickkart", "order-42")
	mustCode(t, err, ErrIdempotencyConflict)
	_, err = h.reserve("PC1", "R3", purchase, 100_000, "freshbasket", "order-42")
	mustCode(t, err, ErrIdempotencyConflict)
	// reusing a reservation id is rejected
	_, err = h.reserve("PC1", "R1", purchase, 1, "quickkart", "other-key")
	mustCode(t, err, ErrAlreadyExists)
}

// ── commit ───────────────────────────────────────────────────────────────────

func TestCommitAndDoubleCommit(t *testing.T) {
	h := setup(t)
	_, err := h.reserve("PC1", "R1", purchase, 245_000, "quickkart", "k1")
	mustOK(t, err)
	c, err := h.commit("R1", purchase)
	mustOK(t, err)
	if c.Reservation.Status != ResCommitted || c.Reservation.PaymentRef != "SIMREF" || c.Reservation.CommitTx == "" {
		t.Fatalf("bad commit: %+v", c.Reservation)
	}
	pc := h.cap("PC1")
	if pc.Reserved != 0 || pc.Committed != 245_000 || pc.Unallocated != 55_000 {
		t.Fatalf("commit pool wrong: %+v", pc)
	}
	_, err = h.commit("R1", purchase)
	mustCode(t, err, ErrAlreadyCommitted)
	if pc2 := h.cap("PC1"); pc2.Committed != 245_000 {
		t.Fatalf("double commit changed state: %+v", pc2)
	}
}

func TestCommitRejections(t *testing.T) {
	h := setup(t)
	_, err := h.commit("NOPE", purchase)
	mustCode(t, err, ErrNotFound)

	_, err = h.reserve("PC1", "R1", purchase, 1000, "quickkart", "")
	mustOK(t, err)
	_, err = h.commit("R1", main)
	mustCode(t, err, ErrHolderMismatch)

	// expired
	h.ctx.stub.now = now + 90
	_, err = h.commit("R1", purchase)
	mustCode(t, err, ErrReservationExpired)
	h.ctx.stub.now = now

	// released
	_, err = h.reserve("PC1", "R2", purchase, 1000, "quickkart", "")
	mustOK(t, err)
	_, err = h.release("R2")
	mustOK(t, err)
	_, err = h.commit("R2", purchase)
	mustCode(t, err, ErrReservationReleased)
}

func TestCommitAfterRevocationRejectedAndHoldReleased(t *testing.T) {
	h := setup(t)
	_, err := h.reserve("PC1", "R1", purchase, 200_000, "quickkart", "")
	mustOK(t, err)
	rv, err := h.revoke("PC1", main, `[]`, `["R1"]`)
	mustOK(t, err)
	if len(rv.ReleasedReservations) != 1 || rv.ReleasedAmount != 200_000 {
		t.Fatalf("revoke did not release hold: %+v", rv)
	}
	_, err = h.commit("R1", purchase)
	mustCode(t, err, ErrReservationReleased)
	pc := h.cap("PC1")
	if pc.Status != StatusRevoked || pc.Reserved != 0 || pc.Unallocated != 300_000 {
		t.Fatalf("revoked pool wrong: %+v", pc)
	}
	// authority of a revoked capability stays frozen: it cannot be returned
	_, err = tx(h, func() (*ReturnResult, error) { return h.c.ReturnUnused(h.ctx, "PC1", main) })
	mustCode(t, err, ErrCapabilityInactive)
}

func TestCommitBlockedWhenAncestorRevokedWithoutListingHold(t *testing.T) {
	h := setup(t)
	_, err := h.reserve("PC1", "R1", purchase, 200_000, "quickkart", "")
	mustOK(t, err)
	// Revoke the root without listing the hold: the commit must still be refused.
	_, err = h.revoke("ROOT", system, `[]`, `[]`)
	mustOK(t, err)
	_, err = h.commit("R1", purchase)
	mustCode(t, err, ErrAncestorInactive)
}

// ── release ─────────────────────────────────────────────────────────────────

func TestReleaseIsIdempotentAndCannotUndoCommit(t *testing.T) {
	h := setup(t)
	_, err := h.reserve("PC1", "R1", purchase, 1000, "quickkart", "")
	mustOK(t, err)
	r, err := h.release("R1")
	mustOK(t, err)
	if r.AlreadyReleased || h.cap("PC1").Unallocated != 300_000 {
		t.Fatalf("release did not return authority")
	}
	r, err = h.release("R1")
	mustOK(t, err)
	if !r.AlreadyReleased || h.cap("PC1").Unallocated != 300_000 {
		t.Fatalf("second release must be a no-op")
	}
	_, err = h.reserve("PC1", "R2", purchase, 1000, "quickkart", "")
	mustOK(t, err)
	_, err = h.commit("R2", purchase)
	mustOK(t, err)
	_, err = h.release("R2")
	mustCode(t, err, ErrAlreadyCommitted)
}

// ── return unused ───────────────────────────────────────────────────────────

func TestReturnUnused(t *testing.T) {
	h := setup(t)
	_, err := h.reserve("PC1", "R1", purchase, 245_000, "quickkart", "")
	mustOK(t, err)
	_, err = tx(h, func() (*ReturnResult, error) { return h.c.ReturnUnused(h.ctx, "PC1", main) })
	mustCode(t, err, ErrInvalidTransition) // hold in flight
	_, err = h.commit("R1", purchase)
	mustOK(t, err)
	_, err = tx(h, func() (*ReturnResult, error) { return h.c.ReturnUnused(h.ctx, "PC1", purchase) })
	mustCode(t, err, ErrIssuerMismatch)
	r, err := tx(h, func() (*ReturnResult, error) { return h.c.ReturnUnused(h.ctx, "PC1", main) })
	mustOK(t, err)
	if r.Returned != 55_000 || r.Child.Total != 245_000 || r.Child.Status != StatusExhausted {
		t.Fatalf("bad return: %+v", r.Child)
	}
	if root := h.cap("ROOT"); root.Unallocated != 755_000 || root.Delegated() != 245_000 {
		t.Fatalf("parent pool wrong: %+v", root)
	}
	_, err = tx(h, func() (*ReturnResult, error) { return h.c.ReturnUnused(h.ctx, "ROOT", system) })
	mustCode(t, err, ErrHierarchyViolation)
}

// ── revoke ──────────────────────────────────────────────────────────────────

func TestRevokeAuthorizationAndSubtree(t *testing.T) {
	h := setup(t)
	_, err := tx(h, func() (*DelegateResult, error) {
		return h.c.Delegate(h.ctx, "PC1", "GC1", purchase, "agent-sub", 50_000, "groceries", `["quickkart"]`, `[]`, now-60, now+day, "")
	})
	mustOK(t, err)
	_, err = h.reserve("GC1", "RG", "agent-sub", 10_000, "quickkart", "")
	mustOK(t, err)

	_, err = h.revoke("PC1", purchase, `[]`, `[]`) // holder is not the issuer
	mustCode(t, err, ErrIssuerMismatch)
	_, err = h.revoke("ROOT", purchase, `[]`, `[]`) // root needs owner, controller or holder
	mustCode(t, err, ErrIssuerMismatch)
	_, err = h.revoke("PC1", main, `["ROOT"]`, `[]`) // cannot revoke outside the subtree
	mustCode(t, err, ErrHierarchyViolation)

	rv, err := h.revoke("PC1", main, `["GC1"]`, `["RG"]`)
	mustOK(t, err)
	if len(rv.RevokedCapabilities) != 2 || rv.ReleasedAmount != 10_000 {
		t.Fatalf("bad revoke result: %+v", rv)
	}
	if h.cap("GC1").Status != StatusRevoked || h.res("RG").Status != ResReleased {
		t.Fatalf("subtree not revoked")
	}
	// revoke is idempotent
	rv, err = h.revoke("PC1", main, `["GC1"]`, `["RG"]`)
	mustOK(t, err)
	if len(rv.RevokedCapabilities) != 0 || rv.ReleasedAmount != 0 {
		t.Fatalf("second revoke changed state: %+v", rv)
	}
	// root stays usable (revocation only affects the subtree)
	_, err = h.reserve("ROOT", "RR", main, 1000, "dailymart", "")
	mustOK(t, err)
}

func TestAuthorityChainRead(t *testing.T) {
	h := setup(t)
	ch, err := tx(h, func() (*AuthorityChain, error) { return h.c.GetAuthorityChain(h.ctx, "PC1") })
	mustOK(t, err)
	if len(ch.Ancestors) != 1 || ch.Ancestors[0].ID != "ROOT" || ch.Mandate == nil || ch.Mandate.ID != "M1" {
		t.Fatalf("bad chain: %+v", ch)
	}
	info, err := tx(h, func() (*Info, error) { return h.c.Info(h.ctx) })
	mustOK(t, err)
	if info.Version != ContractVersion || info.ClientMSP != "Org1MSP" {
		t.Fatalf("bad info: %+v", info)
	}
}

// ── invariants ──────────────────────────────────────────────────────────────

// Every stored value must be a JSON object: Drunix's SQL state DB keeps public
// state in a JSONB column.
func TestAllStateIsJSONObjects(t *testing.T) {
	h := setup(t)
	_, _ = h.reserve("PC1", "R1", purchase, 1000, "quickkart", "k")
	for k, v := range h.ctx.stub.state {
		var obj map[string]interface{}
		if err := json.Unmarshal(v, &obj); err != nil {
			t.Fatalf("key %s is not a JSON object: %s", k, v)
		}
	}
}

// Randomised operation sequences never break conservation, never mint
// authority and never let committed + reserved exceed what was delegated.
func TestConservationUnderRandomOperations(t *testing.T) {
	for seed := int64(1); seed <= 25; seed++ {
		rng := rand.New(rand.NewSource(seed))
		h := setup(t)
		var open []string
		for i := 0; i < 200; i++ {
			switch rng.Intn(5) {
			case 0, 1:
				id := "R" + string(rune('A'+i%26)) + string(rune('a'+i/26))
				amt := int64(rng.Intn(120_000) + 1)
				if _, err := h.reserve("PC1", id, purchase, amt, "quickkart", ""); err == nil {
					open = append(open, id)
				}
			case 2:
				if len(open) > 0 {
					j := rng.Intn(len(open))
					_, _ = h.commit(open[j], purchase)
					open = append(open[:j], open[j+1:]...)
				}
			case 3:
				if len(open) > 0 {
					j := rng.Intn(len(open))
					_, _ = h.release(open[j])
					open = append(open[:j], open[j+1:]...)
				}
			case 4:
				h.ctx.stub.now += int64(rng.Intn(30))
			}
			pc, root := h.cap("PC1"), h.cap("ROOT")
			if err := checkConservation(pc); err != nil {
				t.Fatalf("seed %d step %d: %v", seed, i, err)
			}
			if pc.Total != 300_000 || root.Total != 1_000_000 || root.Delegated() != pc.Total {
				t.Fatalf("seed %d step %d: authority minted or lost: pc=%+v root=%+v", seed, i, pc, root)
			}
			if pc.Unallocated+pc.Reserved+pc.Committed != pc.Total {
				t.Fatalf("seed %d step %d: pool does not sum to total: %+v", seed, i, pc)
			}
		}
	}
}

func TestRevokedRootFreesMandateAllocation(t *testing.T) {
	h := setup(t)
	_, err := tx(h, func() (*Capability, error) {
		return h.c.RegisterRootCapability(h.ctx, "ROOT2", "M1", main, 1, "groceries", `[]`, `[]`, now-day, now+day, "")
	})
	mustCode(t, err, ErrMandateExceeded)
	_, err = h.revoke("ROOT", main, `["PC1"]`, `[]`) // the holder renounces its root
	mustOK(t, err)
	m, _ := tx(h, func() (*Mandate, error) { return h.c.GetMandate(h.ctx, "M1") })
	if m.Allocated != 0 {
		t.Fatalf("revoked root must release its mandate allocation, got %d", m.Allocated)
	}
	_, err = tx(h, func() (*Capability, error) {
		return h.c.RegisterRootCapability(h.ctx, "ROOT2", "M1", main, 1_000_000, "groceries", `[]`, `[]`, now-day, now+day, "")
	})
	mustOK(t, err)
	// the old subtree stays dead
	_, err = h.reserve("PC1", "R1", purchase, 100, "quickkart", "")
	mustCode(t, err, ErrCapabilityInactive)
}

func TestRevokeSkipsIdsNeverOnLedger(t *testing.T) {
	h := setup(t)
	_, err := h.reserve("PC1", "R1", purchase, 1000, "quickkart", "")
	mustOK(t, err)
	rv, err := h.revoke("PC1", main, `["GHOST-CAP"]`, `["R1","SIM-HOLD-1"]`)
	mustOK(t, err)
	if rv.ReleasedAmount != 1000 || len(rv.Skipped) != 2 {
		t.Fatalf("expected R1 released and 2 skipped ids: %+v", rv)
	}
}
