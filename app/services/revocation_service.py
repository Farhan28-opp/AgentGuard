import uuid
from decimal import Decimal
from sqlalchemy.orm import Session
from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound

from app.models.capability import Capability, CapabilityStatus
from app.models.reservation import Reservation, ReservationStatus
from app.models.agent import Agent
from app.exceptions import (
    NotFoundError,
    AgentAuthorizationError
)
from app.schemas.revocation import RevocationResponse

def revoke_capability(db: Session, capability_id: uuid.UUID, agent_id: uuid.UUID) -> RevocationResponse:
    """
    Revoke a capability and all its descendants atomically.
    Release all active reservations within this subtree back to their respective
    capabilities' unallocated authority.
    """
    # 1. Fetch capability
    capability = db.scalar(select(Capability).where(Capability.id == capability_id))
    if not capability:
        raise NotFoundError("Capability not found")

    # 2. Authorize
    if capability.parent_capability_id is not None:
        # Non-root capability can only be revoked by the agent that issued it
        if capability.issued_by_agent_id != agent_id:
            raise AgentAuthorizationError("Not authorized to revoke this capability")
    else:
        # Root capability revocation requires root/system authority
        # Checking if agent_type is "root"
        agent = db.scalar(select(Agent).where(Agent.id == agent_id))
        if not agent:
            raise NotFoundError("Agent not found")
        if agent.agent_type != "root":
            raise AgentAuthorizationError("Root capability revocation requires root agent authority")

    # 3 & 4. Recursive Subtree Discovery
    # We use a recursive CTE to find all descendant capability IDs (including the target itself)
    stmt = text("""
        WITH RECURSIVE subtree AS (
            SELECT id
            FROM capabilities
            WHERE id = :target_id

            UNION ALL

            SELECT c.id
            FROM capabilities c
            JOIN subtree s ON c.parent_capability_id = s.id
        )
        SELECT id FROM subtree;
    """)
    result = db.execute(stmt, {"target_id": capability_id})
    subtree_ids = [row[0] for row in result.fetchall()]

    if not subtree_ids:
        # Should at least contain the target itself
        raise NotFoundError("Capability not found during subtree discovery")

    # 5. Lock affected capabilities in deterministic order (by id)
    # This prevents deadlocks with other transactions that might be locking multiple capabilities
    # populate_existing=True ensures we get the latest row state, ignoring cached session state.
    caps_to_revoke = db.scalars(
        select(Capability)
        .where(Capability.id.in_(subtree_ids))
        .order_by(Capability.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()

    # 6. Find all active (RESERVED) reservations in the subtree and lock them in deterministic order
    active_reservations = db.scalars(
        select(Reservation)
        .where(Reservation.capability_id.in_(subtree_ids))
        .where(Reservation.status == ReservationStatus.RESERVED)
        .order_by(Reservation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()

    released_reservations_count = 0
    released_amount = Decimal("0")

    # 7. Release reservations
    # Group capabilities by id for quick lookup to update authority
    cap_dict = {c.id: c for c in caps_to_revoke}

    for res in active_reservations:
        # We need to double check the status just in case (though with_for_update handles concurrent modifications)
        if res.status == ReservationStatus.RESERVED:
            cap = cap_dict[res.capability_id]
            
            # Atomic return of authority
            cap.reserved_authority -= res.amount
            cap.unallocated_authority += res.amount
            
            # Change status
            res.status = ReservationStatus.RELEASED
            
            released_reservations_count += 1
            released_amount += res.amount

    # 8. Mark subtree as revoked
    revoked_capabilities_count = 0
    for cap in caps_to_revoke:
        if cap.status != CapabilityStatus.REVOKED:
            cap.status = CapabilityStatus.REVOKED
            revoked_capabilities_count += 1
        # Even if already revoked, we count it as part of the subtree we processed, 
        # but let's say `revoked_capabilities_count` counts actual state changes, 
        # or we just count all of them in the subtree. The prompt says "revoked_capabilities"
        # "must represent what the transaction actually changed".

    root_mandate_id = capability.root_mandate_id

    # Drunix (enforce mode): mirror the revocation on-chain. From the next
    # block on, the agentauth chaincode refuses every Reserve / Commit /
    # Delegate beneath this capability, even for descendants and holds it was
    # not told about. Restrictive, so AgentGuard's revocation always stands;
    # an unconfirmed ledger update is journalled as SYNC_PENDING and retried.
    from app.services import ledger_sync
    ledger_sync.on_revoke(db, capability_id, agent_id, subtree_ids,
                          [r.id for r in active_reservations], reason="revoked by AgentGuard")

    # The caller will commit
    return RevocationResponse(
        root_capability_id=capability_id,
        status="revoked",
        revoked_capabilities=revoked_capabilities_count,
        released_reservations=released_reservations_count,
        released_amount=released_amount
    )
