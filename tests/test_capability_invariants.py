"""Tests for Day-1 invariants 1-7 (single-issuance invariants). Invariant 8
(authority conservation across multiple delegations) and the concurrency
angle of invariant 1 are covered in test_authority_conservation.py.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from app.exceptions import (
    DelegationDepthExceededError,
    DelegationLoopError,
    FanoutExceededError,
    InsufficientAuthorityError,
    ScopeViolationError,
    ExpiryViolationError,
    ZeroAuthorityViolationError,
)
from app.services import capability_service
from tests.conftest import build_child_data, create_root_capability, make_agent


def test_valid_child_capability_creation_succeeds(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-1", "root")
    child_agent = make_agent(db, "negotiation-agent-1", "negotiation")
    root = create_root_capability(db, seed_mandate, root_agent)

    child = capability_service.create_capability(
        db, build_child_data(root, child_agent, total_authority=Decimal("2000"))
    )
    db.commit()
    db.refresh(child)
    db.refresh(root)

    assert child.total_authority == Decimal("2000")
    assert child.unallocated_authority == Decimal("2000")
    assert child.delegation_depth == root.delegation_depth + 1
    assert root.unallocated_authority == Decimal("8000")


def test_child_authority_exceeding_parent_available_fails(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-2", "root")
    child_agent = make_agent(db, "purchase-agent-2", "purchase")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"))

    with pytest.raises(InsufficientAuthorityError):
        capability_service.create_capability(
            db, build_child_data(root, child_agent, total_authority=Decimal("6000"))
        )
    db.rollback()


def test_child_scope_broader_than_parent_fails(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-3", "root")
    child_agent = make_agent(db, "purchase-agent-3", "purchase")
    root = create_root_capability(db, seed_mandate, root_agent)

    with pytest.raises(ScopeViolationError):
        capability_service.create_capability(
            db, build_child_data(root, child_agent, category="electronics")
        )
    db.rollback()


def test_child_expiry_beyond_parent_expiry_fails(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-4", "root")
    child_agent = make_agent(db, "purchase-agent-4", "purchase")
    root = create_root_capability(db, seed_mandate, root_agent)

    with pytest.raises(ExpiryViolationError):
        capability_service.create_capability(
            db,
            build_child_data(
                root, child_agent, not_after=root.not_after + timedelta(days=1)
            ),
        )
    db.rollback()


def test_child_not_before_must_precede_not_after(db, seed_mandate):
    """A capability request with not_after <= not_before must be rejected.
    Pydantic's schema validator catches this at the API boundary; this test
    confirms the schema validator is in place.
    """
    from datetime import timedelta
    root_agent = make_agent(db, "shopping-agent-bounds", "root")
    root = create_root_capability(db, seed_mandate, root_agent)

    # not_after == not_before => degenerate window; Pydantic raises ValueError
    with pytest.raises(ValueError, match="not_after must be strictly after not_before"):
        from app.schemas.capability import CapabilityIssueRequest
        CapabilityIssueRequest(
            parent_capability_id=root.id,
            issued_to_agent_id=root.issued_to_agent_id,
            issued_by_agent_id=root.issued_to_agent_id,
            amount=Decimal("100"),
            purpose="test",
            category=root.category,
            max_delegation_depth=1,
            max_fanout=1,
            not_before=root.not_before,
            not_after=root.not_before,  # equal => invalid
        )


def test_delegation_depth_beyond_maximum_fails(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-5", "root")
    child_agent = make_agent(db, "purchase-agent-5", "purchase")
    grandchild_agent = make_agent(db, "sub-purchase-agent-5", "purchase")
    root = create_root_capability(db, seed_mandate, root_agent, max_delegation_depth=1)

    child = capability_service.create_capability(
        db, build_child_data(root, child_agent, total_authority=Decimal("1000"))
    )
    db.commit()
    db.refresh(child)

    with pytest.raises(DelegationDepthExceededError):
        capability_service.create_capability(
            db, build_child_data(child, grandchild_agent, total_authority=Decimal("100"))
        )
    db.rollback()


def test_fanout_beyond_maximum_fails(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-6", "root")
    root = create_root_capability(db, seed_mandate, root_agent, max_fanout=1)

    agent_a = make_agent(db, "agent-6a", "purchase")
    agent_b = make_agent(db, "agent-6b", "purchase")

    capability_service.create_capability(
        db, build_child_data(root, agent_a, total_authority=Decimal("100"))
    )
    db.commit()

    with pytest.raises(FanoutExceededError):
        capability_service.create_capability(
            db, build_child_data(root, agent_b, total_authority=Decimal("100"))
        )
    db.rollback()


def test_historical_fanout_counting(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-historical-fanout", "root")
    root = create_root_capability(db, seed_mandate, root_agent, max_fanout=1)

    agent_a = make_agent(db, "agent-hist-a", "purchase")
    agent_b = make_agent(db, "agent-hist-b", "purchase")

    child_a = capability_service.create_capability(
        db, build_child_data(root, agent_a, total_authority=Decimal("100"))
    )
    db.commit()

    # Revoke the first child
    child_a.status = "revoked"
    db.commit()

    # Even though child A is revoked, fanout is still exhausted
    with pytest.raises(FanoutExceededError):
        capability_service.create_capability(
            db, build_child_data(root, agent_b, total_authority=Decimal("100"))
        )
    db.rollback()


def test_delegation_loop_fails(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-7", "root")
    child_agent = make_agent(db, "purchase-agent-7", "purchase")
    root = create_root_capability(db, seed_mandate, root_agent)

    child = capability_service.create_capability(
        db, build_child_data(root, child_agent, total_authority=Decimal("1000"))
    )
    db.commit()
    db.refresh(child)

    with pytest.raises(DelegationLoopError):
        # Attempting to delegate from the child back to the root agent
        # would make the root a descendant of its own descendant.
        capability_service.create_capability(
            db, build_child_data(child, root_agent, total_authority=Decimal("100"))
        )
    db.rollback()


def test_zero_authority_capability_cannot_later_produce_payment_authority(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-8", "root")
    search_agent = make_agent(db, "search-agent-8", "search")
    downstream_agent = make_agent(db, "downstream-agent-8", "purchase")

    root = create_root_capability(db, seed_mandate, root_agent)
    search = capability_service.create_capability(
        db, build_child_data(root, search_agent, total_authority=Decimal("0"))
    )
    db.commit()
    db.refresh(search)

    with pytest.raises(ZeroAuthorityViolationError):
        capability_service.create_capability(
            db, build_child_data(search, downstream_agent, total_authority=Decimal("500"))
        )
    db.rollback()
