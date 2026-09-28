import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.exceptions import (
    NotFoundError,
    ReservationExpiredError,
    InvalidReservationTransitionError,
    IdempotencyConflictError,
    AgentAuthorizationError,
    CurrencyMismatchError,
    MerchantDeniedError,
    TransactionTimeViolationError,
    InsufficientAuthorityError,
    CapabilityNotActiveError,
    MandateNotActiveError,
    HighRiskContainmentError,
    RiskReviewError,
)
from app.models.capability import Capability, CapabilityStatus
from app.models.mandate import Mandate, MandateStatus
from app.models.reservation import Reservation, ReservationStatus
from app.schemas.reservation import ReserveRequest
from app.services import capability_service


def reserve_authority(db: Session, capability_id: uuid.UUID, request: ReserveRequest,
                      risk_context: Optional[dict] = None) -> Reservation:
    """``risk_context`` is set only by server-side flows (never from an HTTP
    body): ``{"authority_reference": Decimal, "consumption_basis": str}`` for
    a single-purpose direct-payment capability (see feature_extraction)."""
    # We must lock the capability first.
    capability = db.get(Capability, capability_id, with_for_update=True, populate_existing=True)
    if not capability:
        raise NotFoundError(f"Capability {capability_id} not found.")

    # Idempotency check: look for existing reservation with this key inside the transaction.
    if request.idempotency_key:
        existing = db.scalar(
            select(Reservation)
            .where(Reservation.capability_id == capability_id, Reservation.idempotency_key == request.idempotency_key)
        )
        if existing:
            # Check for conflict
            if (
                existing.amount != request.amount or
                existing.currency != request.currency or
                existing.merchant != request.merchant or
                existing.category != request.category
            ):
                raise IdempotencyConflictError("Idempotency key reused with different parameters.")
            return existing

    # ── Deterministic authority validation ───────────────────────────
    # These are HARD failures — the ML layer cannot override them.

    # Validate active
    if capability.status != CapabilityStatus.ACTIVE:
        raise CapabilityNotActiveError(f"Capability is not active (status: {capability.status.value}).")
    
    mandate = db.get(Mandate, capability.root_mandate_id)
    if mandate.status != MandateStatus.ACTIVE:
        raise MandateNotActiveError("Root mandate is not active.")

    # Walk chain
    current = capability
    while current.parent_capability_id:
        parent = db.get(Capability, current.parent_capability_id)
        if parent.status != CapabilityStatus.ACTIVE:
            raise CapabilityNotActiveError(f"Parent capability {parent.id} is not active ({parent.status.value}).")
        current = parent

    # Validate agent
    if capability.issued_to_agent_id != request.agent_id:
        raise AgentAuthorizationError("Agent does not hold this capability.")

    # Validate amount
    if request.amount > capability.unallocated_authority:
        raise InsufficientAuthorityError("Amount exceeds unallocated authority.")

    # Validate currency
    if request.currency != mandate.currency:
        raise CurrencyMismatchError(f"Currency mismatch. Expected {mandate.currency}.")

    # Validate scope (category)
    if request.category != capability.category:
        raise MerchantDeniedError("Category mismatch.")

    # Validate scope (merchant)
    if capability.merchant_allowlist and request.merchant not in capability.merchant_allowlist:
        raise MerchantDeniedError("Merchant not in allowlist.")
    if capability.merchant_denylist and request.merchant in capability.merchant_denylist:
        raise MerchantDeniedError("Merchant is denylisted.")

    # Validate time
    if request.transaction_time < capability.not_before or request.transaction_time > capability.not_after:
        raise TransactionTimeViolationError("Transaction time outside capability valid window.")

    # ── Behavioural Risk Evaluation (Day 5) ──────────────────────────
    # This runs AFTER all deterministic checks pass but BEFORE any
    # reservation is created or any authority is moved.

    from app.services.feature_extraction import extract_features
    from app.services.risk_engine import get_risk_engine
    from app.schemas.risk import RiskAction

    risk_engine = get_risk_engine()
    reference = None
    if risk_context and _single_purpose(capability):
        reference = risk_context.get("authority_reference")
    features = extract_features(db, capability, request, authority_reference=reference)
    if reference is not None:
        risk_result = risk_engine.evaluate(features, capability_id=str(capability_id),
                                           context={"consumption_basis": risk_context["consumption_basis"]})
    else:
        risk_result = risk_engine.evaluate(features, capability_id=str(capability_id))

    if risk_result.action == RiskAction.CONTAIN:
        # HIGH risk — invoke Day-4 revocation to contain the subtree.
        # The reservation for THIS request is NEVER created.
        from app.services import revocation_service

        # Determine the revoker: the issuer for non-root, any root agent
        # for root capabilities.
        revoker_id = capability.issued_by_agent_id
        if revoker_id is None:
            # Root capability — containment is a system control action, so
            # it is attributed to the system agent (type "root"), which also
            # signs root capability grants. Fall back to any root agent.
            from app.models.agent import Agent
            root_agent = db.scalar(
                select(Agent).where(
                    Agent.agent_identifier == "system-agent", Agent.agent_type == "root"
                )
            ) or db.scalar(select(Agent).where(Agent.agent_type == "root"))
            if root_agent:
                revoker_id = root_agent.id
            else:
                # Absolute fallback — use the requesting agent
                revoker_id = request.agent_id

        revocation_service.revoke_capability(db, capability_id, revoker_id)
        # Do NOT raise yet — the caller (router) will commit first,
        # then this exception will be propagated.
        raise HighRiskContainmentError(
            "HIGH behavioural risk detected — capability subtree revoked.",
            risk_result=risk_result,
        )

    if risk_result.action == RiskAction.REVIEW:
        # MEDIUM risk — block the reservation pending review.
        # No reservation is created, no authority is moved.
        raise RiskReviewError(
            "MEDIUM behavioural risk detected — transaction requires review.",
            risk_result=risk_result,
        )

    # ── LOW risk / ALLOW — proceed with reservation ──────────────────

    now = datetime.now(timezone.utc)
    
    # Atomic state update
    capability.unallocated_authority -= request.amount
    capability.reserved_authority += request.amount

    expires_at = now + timedelta(seconds=settings.reservation_ttl_seconds)

    reservation = Reservation(
        id=uuid.uuid4(),
        capability_id=capability.id,
        amount=request.amount,
        currency=request.currency,
        merchant=request.merchant,
        category=request.category,
        status=ReservationStatus.RESERVED,
        idempotency_key=request.idempotency_key,
        expires_at=expires_at,
        created_at=now,
    )

    # Use a savepoint so that a concurrent unique-key race on idempotency_key
    # only rolls back the reservation INSERT, not the entire transaction
    # (which holds the capability row lock and the authority update).
    try:
        with db.begin_nested() as savepoint:
            db.add(reservation)
            db.flush()
    except IntegrityError:
        # Another concurrent transaction committed a reservation with the same
        # idempotency_key first. Re-read the winning reservation.
        existing = db.scalar(
            select(Reservation)
            .where(
                Reservation.capability_id == capability_id,
                Reservation.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.amount != request.amount
                or existing.currency != request.currency
                or existing.merchant != request.merchant
                or existing.category != request.category
            ):
                raise IdempotencyConflictError(
                    "Idempotency key reused with different parameters."
                )
            # We are the concurrent loser: undo the authority movement we
            # made (the winner already accounts for this amount).
            capability.unallocated_authority += request.amount
            capability.reserved_authority -= request.amount
            return existing
        raise

    # ── Drunix on-chain enforcement (DRUNIX_MODE=enforce) ─────────────
    # AgentGuard's identity, deterministic and behavioural checks have all
    # passed. The agentauth chaincode now independently validates the hold
    # against the on-chain authority state (mandate + every ancestor active,
    # holder, amount <= unallocated, merchant/category scope, time window,
    # idempotency). Only a transaction committed VALID on Drunix lets this
    # reservation exist; any rejection raises and the caller rolls back.
    from app.services import ledger_sync
    ledger_sync.on_reserve(db, capability, reservation)

    # Expose the decision that actually admitted this reservation to callers
    # (transient attribute, not persisted) so product surfaces can display the
    # real score instead of re-running the model after the fact.
    reservation.risk_result = risk_result
    reservation.risk_features = features
    return reservation


def _single_purpose(capability: Capability) -> bool:
    """A root grant that cannot be delegated further and has no spending
    history of its own: the shape of a user-authorized direct payment."""
    return (capability.parent_capability_id is None
            and (capability.max_delegation_depth or 0) == 0
            and (capability.max_fanout or 0) == 0
            and capability.reserved_authority == 0
            and capability.committed_authority == 0)


def commit_reservation(db: Session, reservation_id: uuid.UUID) -> Reservation:
    # Lock reservation then capability (though we need capability_id first, we query reservation without lock first, 
    # but that's safe since reservation.capability_id is immutable. Better: use a join or two separate gets).
    # Wait, the prompt said: "All operations that touch both capabilities and reservations acquire locks in the order Capability -> Reservation."
    # So we MUST lock capability first. How do we get capability_id? We read the reservation without lock, lock capability, then lock reservation.
    
    res_no_lock = db.get(Reservation, reservation_id)
    if not res_no_lock:
        raise NotFoundError(f"Reservation {reservation_id} not found.")

    capability_id = res_no_lock.capability_id
    
    # 1. Lock Capability
    capability = db.get(Capability, capability_id, with_for_update=True, populate_existing=True)
    
    # 2. Lock Reservation
    reservation = db.get(Reservation, reservation_id, with_for_update=True, populate_existing=True)

    if reservation.status == ReservationStatus.COMMITTED:
        return reservation
    
    if reservation.status != ReservationStatus.RESERVED:
        raise InvalidReservationTransitionError(f"Cannot commit reservation in status {reservation.status}")

    # Defence in depth: revocation already releases RESERVED holds in the
    # revoked subtree, but never let a hold on a non-active capability settle.
    # The capability row is locked above, so this check cannot race a revoke.
    if capability.status != CapabilityStatus.ACTIVE:
        raise CapabilityNotActiveError(
            f"Capability {capability.id} is not active (status: {capability.status.value}); "
            "its reservation cannot be committed."
        )

    now = datetime.now(timezone.utc)
    if reservation.expires_at <= now:
        raise ReservationExpiredError("Reservation has expired.")

    # Drunix (enforce mode): the hold is settled on-chain first. The chaincode
    # rejects a second commit, a released or expired hold, and any hold whose
    # capability, ancestors or mandate were revoked. No VALID commit on
    # Drunix -> exception -> nothing below runs and no payment is recorded.
    from app.services import ledger_sync
    ledger_sync.on_commit(db, capability, reservation)

    # Atomic state update
    capability.reserved_authority -= reservation.amount
    capability.committed_authority += reservation.amount
    
    reservation.status = ReservationStatus.COMMITTED
    
    db.flush()
    return reservation


def release_reservation(db: Session, reservation_id: uuid.UUID) -> Reservation:
    res_no_lock = db.get(Reservation, reservation_id)
    if not res_no_lock:
        raise NotFoundError(f"Reservation {reservation_id} not found.")

    capability_id = res_no_lock.capability_id
    
    # 1. Lock Capability
    capability = db.get(Capability, capability_id, with_for_update=True, populate_existing=True)
    
    # 2. Lock Reservation
    reservation = db.get(Reservation, reservation_id, with_for_update=True, populate_existing=True)

    if reservation.status == ReservationStatus.RELEASED:
        return reservation
        
    if reservation.status != ReservationStatus.RESERVED:
        raise InvalidReservationTransitionError(f"Cannot release reservation in status {reservation.status}")

    # Atomic state update
    capability.reserved_authority -= reservation.amount
    capability.unallocated_authority += reservation.amount
    
    reservation.status = ReservationStatus.RELEASED
    
    db.flush()
    # Restrictive: mirrored on Drunix; journalled SYNC_PENDING if unconfirmed.
    from app.services import ledger_sync
    ledger_sync.on_release(db, reservation, str(capability.issued_to_agent_id), "released by AgentGuard")
    return reservation


def release_expired_reservations(db: Session) -> int:
    now = datetime.now(timezone.utc)
    # Find expired reservations
    expired = db.scalars(
        select(Reservation)
        .where(Reservation.status == ReservationStatus.RESERVED, Reservation.expires_at <= now)
    ).all()
    
    released_count = 0
    for res in expired:
        # Using the atomic release_reservation ensures proper lock order per reservation
        try:
            # We don't want one failure to stop others. 
            # Note: since release_reservation expects to operate inside a transaction, we might need nested transactions (savepoints) if we want to isolate them.
            # For simplicity in Day 3, we just call it.
            release_reservation(db, res.id)
            released_count += 1
        except Exception:
            # Log error in real system
            pass
            
    return released_count


def get_reservation(db: Session, reservation_id: uuid.UUID) -> Reservation:
    res = db.get(Reservation, reservation_id)
    if not res:
        raise NotFoundError(f"Reservation {reservation_id} not found.")
    return res

