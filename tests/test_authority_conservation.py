"""Tests for Invariant 8 (Authority conservation): authority moves down
the delegation tree, it is never created, and the tree-wide totals always
reconcile against the mandate's total_authority.
"""
import uuid as _uuid
from decimal import Decimal

import pytest

from app.exceptions import InsufficientAuthorityError
from app.services import capability_service
from tests.conftest import build_child_data, create_root_capability, make_agent

def _uid():
    return str(_uuid.uuid4())[:8]


def test_authority_conservation_after_multiple_delegations(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-9-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("10000"))

    search_agent = make_agent(db, f"search-agent-9-{_uid()}", "search")
    negotiation_agent = make_agent(db, f"negotiation-agent-9-{_uid()}", "negotiation")
    purchase_agent = make_agent(db, f"purchase-agent-9-{_uid()}", "purchase")

    capability_service.create_capability(
        db, build_child_data(root, search_agent, total_authority=Decimal("0"))
    )
    capability_service.create_capability(
        db, build_child_data(root, negotiation_agent, total_authority=Decimal("2000"))
    )
    capability_service.create_capability(
        db, build_child_data(root, purchase_agent, total_authority=Decimal("6000"))
    )
    db.commit()
    db.refresh(root)

    ledger = capability_service.compute_ledger(db, seed_mandate.id)

    assert root.unallocated_authority == Decimal("2000")
    assert ledger["total_delegated"] == Decimal("8000")

    # Conservation invariant verified per-capability.
    # For the root: unallocated + delegated + reserved + committed == root.total_authority
    # For each child: unallocated + reserved + committed == child.total_authority
    #
    # Note: compute_ledger's total_unallocated = SUM of unallocated across ALL
    # capabilities (root + children), while total_delegated = SUM of children's
    # total_authority. Adding both at the tree level double-counts child
    # authority, so we verify conservation per-capability instead.
    for entry in ledger["capabilities"]:
        cap_total = (
            entry["unallocated_authority"]
            + entry["delegated_authority"]
            + entry["reserved_authority"]
            + entry["committed_authority"]
        )
        assert cap_total == entry["total_authority"], (
            f"Conservation violated for capability {entry['capability_id']}: "
            f"{entry['unallocated_authority']} + {entry['delegated_authority']} + "
            f"{entry['reserved_authority']} + {entry['committed_authority']} "
            f"!= {entry['total_authority']}"
        )


def test_two_child_allocations_cannot_mathematically_exceed_root(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-10-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"))

    agent_a = make_agent(db, f"agent-10a-{_uid()}", "negotiation")
    agent_b = make_agent(db, f"agent-10b-{_uid()}", "purchase")

    capability_service.create_capability(
        db, build_child_data(root, agent_a, total_authority=Decimal("3000"))
    )
    db.commit()
    db.refresh(root)

    # The root now has only ₹2,000 unallocated. A second child requesting
    # ₹3,000 -- which, summed with the first child, would total ₹6,000
    # against a ₹5,000 root -- must be rejected outright, not partially
    # honored.
    with pytest.raises(InsufficientAuthorityError):
        capability_service.create_capability(
            db, build_child_data(root, agent_b, total_authority=Decimal("3000"))
        )
    db.rollback()
    db.refresh(root)

    assert root.unallocated_authority == Decimal("2000")

from datetime import datetime, timezone
from app.schemas.reservation import ReserveRequest
from app.services import reservation_service

def _now():
    return datetime.now(timezone.utc)

def _key():
    return str(_uuid.uuid4())

def test_authority_conservation_with_reservations(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs11-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("10000"), category="groceries")

    # Delegate 2000
    child_agent = make_agent(db, f"purchase-agent-rs11-{_uid()}", "purchase")
    child = capability_service.create_capability(
        db, build_child_data(root, child_agent, total_authority=Decimal("2000"), category="groceries")
    )
    db.commit()

    # Reserve 1000 on root
    req1 = ReserveRequest(
        agent_id=root_agent.id, amount=Decimal("1000"), currency="INR", merchant="M", category="groceries", transaction_time=_now(), idempotency_key=_key()
    )
    res1 = reservation_service.reserve_authority(db, root.id, req1)
    db.commit()

    # Reserve 500 on child
    req2 = ReserveRequest(
        agent_id=child_agent.id, amount=Decimal("500"), currency="INR", merchant="M", category="groceries", transaction_time=_now(), idempotency_key=_key()
    )
    res2 = reservation_service.reserve_authority(db, child.id, req2)
    db.commit()

    # Commit child reservation
    reservation_service.commit_reservation(db, res2.id)
    db.commit()

    # Release root reservation
    reservation_service.release_reservation(db, res1.id)
    db.commit()

    # Final check of conservation
    ledger = capability_service.compute_ledger(db, seed_mandate.id)
    
    # Root: total 10000, delegated 2000. It reserved 1000, released 1000. Unallocated = 10000 - 2000 - 0 = 8000
    # Child: total 2000. Reserved 500, committed 500. Unallocated = 1500
    
    assert ledger["total_delegated"] == Decimal("2000") # from root to child
    assert ledger["total_reserved"] == Decimal("0")
    assert ledger["total_committed"] == Decimal("500")
    assert ledger["total_unallocated"] == Decimal("8000") + Decimal("1500")

    reconciled_total = (
        ledger["total_unallocated"]
        + ledger["total_delegated"]
        + ledger["total_reserved"]
        + ledger["total_committed"]
    )
    
    # 9500 + 2000 + 0 + 500 = 12000. Wait, total_delegated shouldn't be added if we are summing all unallocated.
    # The existing test checks:
    # unallocated + delegated + reserved + committed == mandate_total.
    # BUT total_unallocated is the sum of unallocated across ALL capabilities, including the root AND the child.
    # Root unallocated: 8000
    # Child unallocated: 1500
    # Total unallocated: 9500
    # If we add delegated (2000), reserved (0), committed (500) = 12000. That's wrong.
    # The conservation identity applies PER CAPABILITY.
    # On root: unallocated (8000) + reserved (0) + committed (0) + delegated (2000) = total (10000)
    # On child: unallocated (1500) + reserved (0) + committed (500) + delegated (0) = total (2000)
    
    # Let's verify per capability.
    db.refresh(root)
    assert root.unallocated_authority + root.reserved_authority + root.committed_authority + ledger["total_delegated"] == root.total_authority
    
    db.refresh(child)
    assert child.unallocated_authority + child.reserved_authority + child.committed_authority == child.total_authority
