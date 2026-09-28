"""Tests for Capability Issuance Service (Day 2 requirements)."""
from decimal import Decimal
import pytest
from app.exceptions import (
    IssuerAuthorizationError,
    SelfDelegationError,
    TargetAgentError,
    CapabilityNotActiveError,
    InsufficientAuthorityError
)
from app.models.capability import CapabilityStatus
from app.schemas.capability import CapabilityIssueRequest
from app.services import capability_service
from tests.conftest import create_root_capability, make_agent

def test_issuer_authorization(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-issuance", "root")
    imposter_agent = make_agent(db, "imposter-agent", "root")
    target_agent = make_agent(db, "target-agent", "purchase")
    root = create_root_capability(db, seed_mandate, root_agent)
    
    req = CapabilityIssueRequest(
        parent_capability_id=root.id,
        issued_to_agent_id=target_agent.id,
        issued_by_agent_id=imposter_agent.id,
        amount=Decimal("100"),
        purpose="test",
        category=root.category,
        max_delegation_depth=1,
        max_fanout=1,
        not_before=root.not_before,
        not_after=root.not_after
    )
    with pytest.raises(IssuerAuthorizationError):
        capability_service.issue_capability(db, req)
    db.rollback()

def test_self_delegation(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-self", "root")
    root = create_root_capability(db, seed_mandate, root_agent)
    
    req = CapabilityIssueRequest(
        parent_capability_id=root.id,
        issued_to_agent_id=root_agent.id,
        issued_by_agent_id=root_agent.id,
        amount=Decimal("100"),
        purpose="test",
        category=root.category,
        max_delegation_depth=1,
        max_fanout=1,
        not_before=root.not_before,
        not_after=root.not_after
    )
    with pytest.raises(SelfDelegationError):
        capability_service.issue_capability(db, req)
    db.rollback()

def test_inactive_parent(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-inactive", "root")
    target_agent = make_agent(db, "target-agent-inactive", "purchase")
    root = create_root_capability(db, seed_mandate, root_agent)
    root.status = CapabilityStatus.REVOKED
    db.commit()
    
    req = CapabilityIssueRequest(
        parent_capability_id=root.id,
        issued_to_agent_id=target_agent.id,
        issued_by_agent_id=root_agent.id,
        amount=Decimal("100"),
        purpose="test",
        category=root.category,
        max_delegation_depth=1,
        max_fanout=1,
        not_before=root.not_before,
        not_after=root.not_after
    )
    with pytest.raises(CapabilityNotActiveError):
        capability_service.issue_capability(db, req)
    db.rollback()


def test_nonexistent_target_agent(db, seed_mandate):
    """Issuing to a target agent UUID that doesn't exist in the agents table
    must be rejected before any authority moves."""
    import uuid as _uuid
    root_agent = make_agent(db, "shopping-agent-badtarget", "root")
    root = create_root_capability(db, seed_mandate, root_agent)

    req = CapabilityIssueRequest(
        parent_capability_id=root.id,
        issued_to_agent_id=_uuid.uuid4(),  # Nonexistent
        issued_by_agent_id=root_agent.id,
        amount=Decimal("100"),
        purpose="test",
        category=root.category,
        max_delegation_depth=1,
        max_fanout=1,
        not_before=root.not_before,
        not_after=root.not_after
    )
    with pytest.raises(TargetAgentError):
        capability_service.issue_capability(db, req)
    db.rollback()


def test_inactive_target_agent(db, seed_mandate):
    """Issuing to an agent that exists but is suspended must also be rejected."""
    root_agent = make_agent(db, "shopping-agent-badtarget2", "root")
    suspended_agent = make_agent(db, "suspended-agent", "purchase")
    suspended_agent.status = "suspended"
    db.commit()

    root = create_root_capability(db, seed_mandate, root_agent)

    req = CapabilityIssueRequest(
        parent_capability_id=root.id,
        issued_to_agent_id=suspended_agent.id,
        issued_by_agent_id=root_agent.id,
        amount=Decimal("100"),
        purpose="test",
        category=root.category,
        max_delegation_depth=1,
        max_fanout=1,
        not_before=root.not_before,
        not_after=root.not_after
    )
    with pytest.raises(TargetAgentError):
        capability_service.issue_capability(db, req)
    db.rollback()

def test_transaction_rollback_on_failure(db, seed_mandate):
    root_agent = make_agent(db, "shopping-agent-rollback", "root")
    target_agent = make_agent(db, "target-agent-rollback", "purchase")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("1000"))
    
    # Request exceeds unallocated authority -> validation will fail inside issue_capability
    req = CapabilityIssueRequest(
        parent_capability_id=root.id,
        issued_to_agent_id=target_agent.id,
        issued_by_agent_id=root_agent.id,
        amount=Decimal("5000"),
        purpose="test",
        category=root.category,
        max_delegation_depth=1,
        max_fanout=1,
        not_before=root.not_before,
        not_after=root.not_after
    )
    
    try:
        capability_service.issue_capability(db, req)
    except InsufficientAuthorityError:
        db.rollback()
        
    db.refresh(root)
    assert root.unallocated_authority == Decimal("1000") # Parent authority unchanged
