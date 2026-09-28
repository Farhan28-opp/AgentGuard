"""Pure(ish) validation functions implementing the eight Day-1 authority
invariants. Each function raises a specific app.exceptions.AgentGuardError
subclass on violation and returns None (or a derived value, where useful)
on success. Keeping these separate from capability_service's persistence
logic means each invariant can be unit-tested and reasoned about in
isolation.
"""
import uuid
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.exceptions import (
    DelegationDepthExceededError,
    DelegationLoopError,
    FanoutExceededError,
    InsufficientAuthorityError,
    ScopeViolationError,
    ExpiryViolationError,
    ZeroAuthorityViolationError,
    IssuerAuthorizationError,
    TargetAgentError,
    SelfDelegationError,
    CapabilityNotActiveError,
)
from app.models.agent import Agent
from app.models.capability import Capability, CapabilityStatus


def validate_authority_available(parent: Capability, requested_amount: Decimal) -> None:
    """Invariant 1 (No authority creation) / Invariant 8 (Authority
    conservation): a child can only receive authority that is currently
    unallocated on the parent. Authority moves down the tree; it is never
    copied or invented.
    """
    if requested_amount > parent.unallocated_authority:
        raise InsufficientAuthorityError(
            f"Requested authority {requested_amount} exceeds parent capability "
            f"{parent.id}'s unallocated authority {parent.unallocated_authority}."
        )


def validate_zero_authority_rule(parent: Capability, requested_amount: Decimal) -> None:
    """Invariant 7 (No zero-authority payment capability): a capability
    issued with zero total authority (e.g. a search agent) may exist for
    non-financial tasks, but can never become the origin of a descendant
    that holds nonzero payment authority.
    """
    if parent.total_authority == 0 and requested_amount > 0:
        raise ZeroAuthorityViolationError(
            f"Parent capability {parent.id} was issued zero total authority; "
            f"it may only delegate further zero-authority (non-financial) "
            f"capabilities, not the requested {requested_amount}."
        )


def validate_scope_narrowing(
    parent: Capability,
    child_category: str,
    child_allowlist: Optional[List[str]],
    child_denylist: Optional[List[str]],
) -> None:
    """Invariant 2 (Scope can only narrow).

    Day 1 keeps this deterministic and simple, per the spec: category must
    match exactly (sub-categories/hierarchical categories are a later
    refinement), an allowlist may only shrink, and a denylist may only grow.
    """
    if child_category != parent.category:
        raise ScopeViolationError(
            f"Child category '{child_category}' does not match parent "
            f"category '{parent.category}'. Day 1 requires exact category "
            f"match; narrowing within a category is a post-Day-1 feature."
        )

    if parent.merchant_allowlist:
        parent_allow = set(parent.merchant_allowlist)
        if child_allowlist is None or not set(child_allowlist).issubset(parent_allow):
            raise ScopeViolationError(
                "Parent restricts to a merchant allowlist; child's "
                "merchant_allowlist must be a non-null subset of the parent's."
            )

    if parent.merchant_denylist:
        parent_deny = set(parent.merchant_denylist)
        child_deny = set(child_denylist or [])
        if not parent_deny.issubset(child_deny):
            raise ScopeViolationError(
                "Child merchant_denylist must retain everything the parent "
                "denies; a child may deny more merchants, never fewer."
            )


def validate_expiry_narrowing(parent: Capability, child_not_before, child_not_after) -> None:
    """Invariant 3 (Expiry can only narrow): the child's active window must
    sit entirely within the parent's."""
    if child_not_before >= child_not_after:
        raise ExpiryViolationError(
            f"Child not_before ({child_not_before}) must strictly precede not_after ({child_not_after})."
        )
    if child_not_after > parent.not_after:
        raise ExpiryViolationError(
            f"Child not_after ({child_not_after}) must not exceed parent's "
            f"not_after ({parent.not_after})."
        )
    if child_not_before < parent.not_before:
        raise ExpiryViolationError(
            f"Child not_before ({child_not_before}) must not precede parent's "
            f"not_before ({parent.not_before})."
        )


def validate_delegation_depth(parent: Capability, child_max_delegation_depth: int) -> int:
    """Invariant 4 (Delegation depth): child depth = parent depth + 1, and
    a child cannot raise the delegation-depth ceiling above what its parent
    had. Returns the computed child depth for convenience.
    """
    if child_max_delegation_depth > parent.max_delegation_depth:
        raise DelegationDepthExceededError(
            f"Child max_delegation_depth ({child_max_delegation_depth}) may "
            f"not exceed parent's max_delegation_depth "
            f"({parent.max_delegation_depth})."
        )

    child_depth = parent.delegation_depth + 1
    if child_depth > child_max_delegation_depth:
        raise DelegationDepthExceededError(
            f"Delegation depth {child_depth} would exceed max_delegation_depth "
            f"{child_max_delegation_depth}."
        )
    return child_depth


def validate_fanout(db: Session, parent: Capability) -> None:
    """Invariant 5 (Fanout): a capability cannot have more children than
    its max_fanout across its entire lifetime.
    This check is performed under a row lock on the parent capability. To
    prevent race conditions where concurrent issuances might both see the
    same child count, we also acquire a lock on the child rows via a
    `SELECT ... FOR UPDATE` query. This ensures that any concurrent
    transaction attempting to insert a new child will wait until the
    current transaction commits, preserving the fanout invariant.

    NOTE: PostgreSQL does not allow FOR UPDATE on queries containing
    aggregate functions (e.g. COUNT). We therefore SELECT the child IDs
    individually with FOR UPDATE (which acquires the row-level locks) and
    derive the count from the result-set length in Python.
    """
    # Lock individual child rows so that any concurrent INSERT of a new
    # child will block until this transaction commits. Counting in Python
    # avoids the PostgreSQL restriction on FOR UPDATE + aggregates.
    child_ids = db.scalars(
        select(Capability.id)
        .where(Capability.parent_capability_id == parent.id)
        .with_for_update()
    ).all()
    existing_children = len(child_ids)
    if existing_children >= parent.max_fanout:
        raise FanoutExceededError(
            f"Parent capability {parent.id} has reached its max_fanout "
            f"({parent.max_fanout})."
        )


def validate_no_delegation_loop(
    db: Session, parent: Capability, issued_to_agent_id: uuid.UUID
) -> None:
    """Invariant 6 (No delegation loops): walk the ancestor chain from
    `parent` up to the root capability, collecting every agent that has
    already appeared as an issuer or recipient. If the new capability's
    recipient agent is already in that set, creating it would make some
    capability a descendant of one of its own ancestors -- reject it.
    """
    seen_agents = set()
    current: Optional[Capability] = parent
    while current is not None:
        seen_agents.add(current.issued_to_agent_id)
        if current.issued_by_agent_id:
            seen_agents.add(current.issued_by_agent_id)
        if current.parent_capability_id is None:
            break
        current = db.get(Capability, current.parent_capability_id)

    if issued_to_agent_id in seen_agents:
        raise DelegationLoopError(
            f"Agent {issued_to_agent_id} already appears in this capability's "
            f"ancestor chain; delegating to it here would create a loop."
        )


def validate_issuer(parent: Capability, issuing_agent_id: uuid.UUID) -> None:
    if parent.issued_to_agent_id != issuing_agent_id:
        raise IssuerAuthorizationError(
            f"Agent {issuing_agent_id} is not the holder of capability {parent.id} "
            f"(it was issued to {parent.issued_to_agent_id})."
        )


def validate_target_agent(db: Session, target_agent_id: uuid.UUID) -> None:
    target = db.get(Agent, target_agent_id)
    if not target:
        raise TargetAgentError(f"Target agent {target_agent_id} does not exist.")
    if target.status != "active":
        raise TargetAgentError(f"Target agent {target_agent_id} is not active (status: {target.status}).")


def validate_self_delegation(issuing_agent_id: uuid.UUID, target_agent_id: uuid.UUID) -> None:
    if issuing_agent_id == target_agent_id:
        raise SelfDelegationError("Agents cannot delegate capabilities to themselves.")


def validate_parent_status(parent: Capability) -> None:
    """Check that the parent capability is still active (not revoked, expired,
    or exhausted). Authority availability is checked separately via
    validate_authority_available, so we do NOT pre-screen unallocated_authority
    here -- zero-authority parents are valid for non-financial task delegation.
    """
    if parent.status != CapabilityStatus.ACTIVE:
        raise CapabilityNotActiveError(
            f"Parent capability {parent.id} is not active (status={parent.status})."
        )
