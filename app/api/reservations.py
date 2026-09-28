"""Signed reservation API — Day 6.

Every mutating endpoint now requires a SignedEnvelope.  The verification
pipeline (signing.py) runs BEFORE any capability/reservation logic.

Verification order (per Section 12-14 of the spec):
  1. Parse signed envelope
  2. Timestamp window check
  3. Load agent + public key
  4. Ed25519 signature verification
  5. Payload hash integrity
  6. Anti-replay nonce consumption
  7. Operation binding (operation field + resource_id field)
  8. Existing deterministic checks (capability ownership, authority, etc.)
  9. Day-5 behavioural risk
  10. Reservation state mutation
"""
import uuid
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.exceptions import HighRiskContainmentError
from app.schemas.reservation import ReserveRequest, ReservationResponse, ReservationRead
from app.schemas.signed import SignedReserveRequest, SignedOperationRequest
from app.security.signing import verify_signed_request
from app.services import reservation_service
from app.services.capability_service import get_capability

router = APIRouter(tags=["reservations"])


def _to_response(db: Session, res) -> ReservationResponse:
    cap = get_capability(db, res.capability_id)
    return ReservationResponse(
        id=res.id,
        capability_id=res.capability_id,
        amount=res.amount,
        currency=res.currency,
        merchant=res.merchant,
        category=res.category,
        status=res.status,
        idempotency_key=res.idempotency_key,
        expires_at=res.expires_at,
        created_at=res.created_at,
        remaining_unallocated_authority=cap.unallocated_authority,
    )


@router.post("/capabilities/{capability_id}/reserve", response_model=ReservationResponse, status_code=201)
def reserve(capability_id: uuid.UUID, request: SignedReserveRequest, db: Session = Depends(get_db)):
    """Reserve financial authority with a signed request.

    The caller must sign:
        agent_id | "reserve" | capability_id | request_id | timestamp | sha256(payload)
    """
    env = request.envelope
    payload = request.payload

    # 1. Operation binding (before crypto — fast check)
    request.validate_operation(capability_id)

    # 2–6. Full cryptographic verification
    payload_dict = payload.model_dump(mode="json")
    # Normalize datetime for hashing
    payload_dict["transaction_time"] = payload.transaction_time.isoformat()
    payload_dict["amount"] = str(payload.amount)

    verify_signed_request(
        db,
        agent_id=env.agent_id,
        operation=env.operation,
        resource_id=env.resource_id,
        request_id=env.request_id,
        timestamp=env.timestamp,
        payload_hash=env.payload_hash,
        signature=env.signature,
        payload_dict=payload_dict,
    )

    # 7. Build the internal ReserveRequest (agent_id comes from verified envelope)
    reserve_req = ReserveRequest(
        agent_id=env.agent_id,
        amount=payload.amount,
        currency=payload.currency,
        merchant=payload.merchant,
        category=payload.category,
        transaction_time=payload.transaction_time,
        idempotency_key=payload.idempotency_key,
    )

    try:
        res = reservation_service.reserve_authority(db, capability_id, reserve_req)
        db.commit()
        db.refresh(res)
        return _to_response(db, res)
    except HighRiskContainmentError:
        db.commit()
        raise


@router.post("/reservations/{reservation_id}/commit", response_model=ReservationResponse)
def commit(reservation_id: uuid.UUID, request: SignedOperationRequest, db: Session = Depends(get_db)):
    """Commit a reservation.  Signed request must bind operation='commit' + reservation_id."""
    env = request.envelope
    request.validate_operation("commit", reservation_id)

    verify_signed_request(
        db,
        agent_id=env.agent_id,
        operation=env.operation,
        resource_id=env.resource_id,
        request_id=env.request_id,
        timestamp=env.timestamp,
        payload_hash=env.payload_hash,
        signature=env.signature,
    )

    # Verify the signing agent owns the capability for this reservation
    _assert_reservation_agent(db, reservation_id, env.agent_id)

    res = reservation_service.commit_reservation(db, reservation_id)
    db.commit()
    db.refresh(res)
    return _to_response(db, res)


@router.post("/reservations/{reservation_id}/release", response_model=ReservationResponse)
def release(reservation_id: uuid.UUID, request: SignedOperationRequest, db: Session = Depends(get_db)):
    """Release a reservation.  Signed request must bind operation='release' + reservation_id."""
    env = request.envelope
    request.validate_operation("release", reservation_id)

    verify_signed_request(
        db,
        agent_id=env.agent_id,
        operation=env.operation,
        resource_id=env.resource_id,
        request_id=env.request_id,
        timestamp=env.timestamp,
        payload_hash=env.payload_hash,
        signature=env.signature,
    )

    _assert_reservation_agent(db, reservation_id, env.agent_id)

    res = reservation_service.release_reservation(db, reservation_id)
    db.commit()
    db.refresh(res)
    return _to_response(db, res)


@router.get("/reservations/{reservation_id}", response_model=ReservationResponse)
def get_reservation(reservation_id: uuid.UUID, db: Session = Depends(get_db)):
    """Read a reservation — no signature required (read-only)."""
    res = reservation_service.get_reservation(db, reservation_id)
    return _to_response(db, res)


# ---------------------------------------------------------------------------
# Helper: verify that the signing agent is authorised for this reservation
# ---------------------------------------------------------------------------

def _assert_reservation_agent(db: Session, reservation_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """The signing agent must hold the capability that backs this reservation."""
    from app.models.reservation import Reservation
    from app.models.capability import Capability
    from app.exceptions import AgentAuthorizationError

    res = db.get(Reservation, reservation_id)
    if res is None:
        from app.exceptions import NotFoundError
        raise NotFoundError(f"Reservation {reservation_id} not found.")
    cap = db.get(Capability, res.capability_id)
    if cap is None or cap.issued_to_agent_id != agent_id:
        raise AgentAuthorizationError(
            "Signing agent does not hold the capability backing this reservation."
        )
