"""Capability issuance and the mandate-level ledger view.

The central function is `issue_capability`. It acquires a row lock on the
parent capability (`with_for_update=True`) and performs all invariant checks
*and* the authority-transfer write inside the same transaction. This is what
stops two concurrent delegation requests against the same parent from both
reading a stale `unallocated_authority` and both succeeding -- the exact
"delegation atomicity" gap that a naive check-then-write implementation would
have. The equivalent lock is taken on the mandate for root-capability issuance.

`create_capability` is a backward-compatibility shim for the test helpers
that still use `CapabilityCreate`; it delegates to `create_root_capability`
or `issue_capability` as appropriate.
"""
import uuid
from decimal import Decimal
from typing import List

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.exceptions import (
    InsufficientAuthorityError,
    MandateNotActiveError,
    NotFoundError,
    ExpiryViolationError,
)
from app.models.capability import Capability, CapabilityStatus
from app.models.mandate import Mandate, MandateStatus
from app.schemas.capability import CapabilityCreate, CapabilityIssueRequest
from app.services import authority_service, ledger_sync


def create_root_capability(db: Session, data: CapabilityCreate) -> Capability:
    """A root capability has no parent_capability_id; its authority is
    carved directly out of the mandate's total_authority (rather than a
    parent capability's unallocated_authority), so it needs its own
    conservation check against sibling root capabilities under the same
    mandate.
    """
    mandate = db.get(Mandate, data.root_mandate_id, with_for_update=True, populate_existing=True)
    if mandate is None:
        raise NotFoundError(f"Mandate {data.root_mandate_id} not found.")
    if mandate.status != MandateStatus.ACTIVE:
        raise MandateNotActiveError(
            f"Mandate {mandate.id} is not active (status={mandate.status})."
        )
    if data.not_before < mandate.not_before or data.not_after > mandate.not_after:
        raise ExpiryViolationError(
            "Root capability's active window must sit within the mandate's window."
        )

    already_allocated = db.scalar(
        select(func.coalesce(func.sum(Capability.total_authority), 0)).where(
            Capability.root_mandate_id == mandate.id,
            Capability.parent_capability_id.is_(None),
            Capability.status != CapabilityStatus.REVOKED,
        )
    )
    remaining_mandate_authority = mandate.total_authority - Decimal(already_allocated)
    if data.total_authority > remaining_mandate_authority:
        raise InsufficientAuthorityError(
            f"Requested root authority {data.total_authority} exceeds the "
            f"mandate's remaining unallocated authority "
            f"{remaining_mandate_authority}."
        )

    capability = Capability(
        id=uuid.uuid4(),
        parent_capability_id=None,
        root_mandate_id=mandate.id,
        issued_to_agent_id=data.issued_to_agent_id,
        issued_by_agent_id=data.issued_by_agent_id,
        total_authority=data.total_authority,
        unallocated_authority=data.total_authority,
        reserved_authority=Decimal("0"),
        committed_authority=Decimal("0"),
        purpose=data.purpose,
        category=data.category,
        merchant_allowlist=data.merchant_allowlist,
        merchant_denylist=data.merchant_denylist,
        delegation_depth=0,
        max_delegation_depth=data.max_delegation_depth,
        max_fanout=data.max_fanout,
        not_before=data.not_before,
        not_after=data.not_after,
        status=CapabilityStatus.ACTIVE,
    )
    
    # Day 6: Capability Grant Attestation
    from app.models.agent import Agent
    from app.security.capability_signing import sign_capability_grant
    # Root capabilities are signed by the 'system-agent'
    system_agent = db.scalar(select(Agent).where(Agent.agent_identifier == "system-agent"))
    if system_agent is None:
        raise NotFoundError("system-agent not found for root capability signing")
    capability.grant_signature = sign_capability_grant(capability, system_agent)

    db.add(capability)
    db.flush()
    # Drunix (enforce mode): the root must also be registered on-chain against
    # the mandate. Fails closed -- a rejection rolls back the caller's transaction.
    ledger_sync.on_capability_created(db, capability)
    return capability


def issue_capability(db: Session, request: CapabilityIssueRequest) -> Capability:
    # Row lock on the parent: every invariant check below and the
    # authority-transfer write at the bottom happen while this lock is
    # held, which is what makes delegation atomic under concurrency.
    parent = db.get(Capability, request.parent_capability_id, with_for_update=True, populate_existing=True)
    if parent is None:
        raise NotFoundError(f"Parent capability {request.parent_capability_id} not found.")

    authority_service.validate_parent_status(parent)

    mandate = db.get(Mandate, parent.root_mandate_id)
    if mandate is None or mandate.status != MandateStatus.ACTIVE:
        raise MandateNotActiveError(
            f"Root mandate {parent.root_mandate_id} is not active."
        )

    if request.issued_by_agent_id:
        authority_service.validate_issuer(parent, request.issued_by_agent_id)

    authority_service.validate_target_agent(db, request.issued_to_agent_id)

    if request.issued_by_agent_id:
        authority_service.validate_self_delegation(request.issued_by_agent_id, request.issued_to_agent_id)

    authority_service.validate_zero_authority_rule(parent, request.amount)
    authority_service.validate_authority_available(parent, request.amount)
    authority_service.validate_scope_narrowing(
        parent, request.category, request.merchant_allowlist, request.merchant_denylist
    )
    authority_service.validate_expiry_narrowing(parent, request.not_before, request.not_after)
    child_depth = authority_service.validate_delegation_depth(
        parent, request.max_delegation_depth
    )
    authority_service.validate_fanout(db, parent)
    authority_service.validate_no_delegation_loop(db, parent, request.issued_to_agent_id)

    # Authority transfer: the parent's unallocated pool shrinks by exactly
    # what the child receives. Nothing is duplicated -- this line and the
    # checks above are inside the same transaction, protected by the row
    # lock acquired above.
    parent.unallocated_authority = parent.unallocated_authority - request.amount

    child = Capability(
        id=uuid.uuid4(),
        parent_capability_id=parent.id,
        root_mandate_id=parent.root_mandate_id,
        issued_to_agent_id=request.issued_to_agent_id,
        issued_by_agent_id=request.issued_by_agent_id or parent.issued_to_agent_id,
        total_authority=request.amount,
        unallocated_authority=request.amount,
        reserved_authority=Decimal("0"),
        committed_authority=Decimal("0"),
        purpose=request.purpose,
        category=request.category,
        merchant_allowlist=request.merchant_allowlist,
        merchant_denylist=request.merchant_denylist,
        delegation_depth=child_depth,
        max_delegation_depth=request.max_delegation_depth,
        max_fanout=request.max_fanout,
        not_before=request.not_before,
        not_after=request.not_after,
        status=CapabilityStatus.ACTIVE,
    )
    
    # Day 6: Capability Grant Attestation
    from app.models.agent import Agent
    from app.security.capability_signing import sign_capability_grant
    
    issuer_id = request.issued_by_agent_id or parent.issued_to_agent_id
    issuer_agent = db.get(Agent, issuer_id)
    if issuer_agent is None:
        raise NotFoundError(f"Issuer agent {issuer_id} not found")
        
    child.grant_signature = sign_capability_grant(child, issuer_agent)

    db.add(child)
    db.flush()
    # Drunix (enforce mode): the agentauth chaincode independently checks the
    # delegation (issuer holds the parent, amount <= parent unallocated, scope
    # and window only narrow) and moves the authority on-chain. Fails closed.
    ledger_sync.on_capability_created(db, child)
    return child


def create_capability(db: Session, data: CapabilityCreate) -> Capability:
    if data.parent_capability_id is None:
        return create_root_capability(db, data)
    # Mapping legacy CapabilityCreate to CapabilityIssueRequest for internal usage/tests
    req = CapabilityIssueRequest(
        parent_capability_id=data.parent_capability_id,
        issued_to_agent_id=data.issued_to_agent_id,
        issued_by_agent_id=data.issued_by_agent_id,
        amount=data.total_authority,
        purpose=data.purpose,
        category=data.category,
        merchant_allowlist=data.merchant_allowlist,
        merchant_denylist=data.merchant_denylist,
        max_delegation_depth=data.max_delegation_depth,
        max_fanout=data.max_fanout,
        not_before=data.not_before,
        not_after=data.not_after
    )
    return issue_capability(db, req)


def get_capability(db: Session, capability_id: uuid.UUID) -> Capability:
    capability = db.get(Capability, capability_id)
    if capability is None:
        raise NotFoundError(f"Capability {capability_id} not found.")
    return capability


def list_capabilities_for_mandate(db: Session, mandate_id: uuid.UUID) -> List[Capability]:
    return list(
        db.scalars(
            select(Capability)
            .where(Capability.root_mandate_id == mandate_id)
            .order_by(Capability.delegation_depth, Capability.created_at)
        ).all()
    )


def compute_ledger(db: Session, mandate_id: uuid.UUID) -> dict:
    """Builds the full authority-pool view for a mandate: per-capability
    breakdown plus tree-wide totals. `delegated_authority` per capability
    (and `total_delegated` overall) is derived by summing children's
    total_authority rather than stored -- see the module docstring in
    app/models/capability.py for why that's the single-source-of-truth
    choice.
    """
    capabilities = list_capabilities_for_mandate(db, mandate_id)

    children_sum_by_parent: dict = {}
    for cap in capabilities:
        if cap.parent_capability_id is not None:
            children_sum_by_parent[cap.parent_capability_id] = (
                children_sum_by_parent.get(cap.parent_capability_id, Decimal("0"))
                + cap.total_authority
            )

    entries = []
    total_unallocated = Decimal("0")
    total_reserved = Decimal("0")
    total_committed = Decimal("0")

    for cap in capabilities:
        delegated = children_sum_by_parent.get(cap.id, Decimal("0"))
        entries.append(
            {
                "capability_id": cap.id,
                "issued_to_agent_id": cap.issued_to_agent_id,
                "total_authority": cap.total_authority,
                "unallocated_authority": cap.unallocated_authority,
                "delegated_authority": delegated,
                "reserved_authority": cap.reserved_authority,
                "committed_authority": cap.committed_authority,
                "delegation_depth": cap.delegation_depth,
                "status": cap.status,
            }
        )
        total_unallocated += cap.unallocated_authority
        total_reserved += cap.reserved_authority
        total_committed += cap.committed_authority

    total_delegated = sum(
        (cap.total_authority for cap in capabilities if cap.parent_capability_id is not None),
        Decimal("0"),
    )

    mandate = db.get(Mandate, mandate_id)
    return {
        "mandate_id": mandate_id,
        "total_authority": mandate.total_authority if mandate else Decimal("0"),
        "total_unallocated": total_unallocated,
        "total_delegated": total_delegated,
        "total_reserved": total_reserved,
        "total_committed": total_committed,
        "capabilities": entries,
    }


def return_unused_authority(db: Session, capability_id: uuid.UUID, by_agent_id: uuid.UUID) -> Decimal:
    """Attenuate a child capability down to what it actually used and hand
    the unused authority back to its parent (the inverse of delegation).

    * only the capability's issuer may do this;
    * the capability must be ACTIVE with no reserved (in-flight) authority;
    * unallocated authority moves child → parent; the child's total shrinks
      by the same amount, so ``parent.total − parent.pool == Σ children``
      still holds — nothing is minted or lost;
    * the attenuated grant is re-signed by the issuer (the grant signature
      covers total_authority), and a fully used child becomes EXHAUSTED.

    Revoked capabilities are never "returned": authority held by a revoked
    (e.g. contained) agent stays frozen in the ledger.
    Locks parent then child (same order as delegation). Caller commits.
    """
    from app.exceptions import IssuerAuthorizationError, InvalidReservationTransitionError
    from app.models.agent import Agent
    from app.security.capability_signing import sign_capability_grant

    child_peek = db.get(Capability, capability_id)
    if child_peek is None:
        raise NotFoundError(f"Capability {capability_id} not found.")
    if child_peek.parent_capability_id is None:
        raise InvalidReservationTransitionError("A root capability has no parent to return authority to.")
    parent = db.get(Capability, child_peek.parent_capability_id, with_for_update=True, populate_existing=True)
    child = db.get(Capability, capability_id, with_for_update=True, populate_existing=True)
    if child.issued_by_agent_id != by_agent_id:
        raise IssuerAuthorizationError("Only the issuing agent can take back unused authority.")
    if child.status != CapabilityStatus.ACTIVE:
        raise InvalidReservationTransitionError(
            f"Capability is {child.status.value}; its authority cannot be returned.")
    if child.reserved_authority > 0:
        raise InvalidReservationTransitionError("Capability still has reserved (in-flight) authority.")

    amount = child.unallocated_authority
    if amount > 0:
        child.unallocated_authority = Decimal("0")
        child.total_authority = child.total_authority - amount
        parent.unallocated_authority = parent.unallocated_authority + amount
        issuer = db.get(Agent, child.issued_by_agent_id)
        child.grant_signature = sign_capability_grant(child, issuer)
    if child.unallocated_authority == 0 and child.reserved_authority == 0:
        delegated = child.total_authority - child.committed_authority
        if delegated == 0:
            child.status = CapabilityStatus.EXHAUSTED
    db.flush()
    # Restrictive: mirrored on Drunix; journalled SYNC_PENDING if unconfirmed.
    ledger_sync.on_return_unused(db, child, by_agent_id)
    return amount
