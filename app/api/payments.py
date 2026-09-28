"""Simulated Payment Rail API — Day 6.

POST /reservations/{reservation_id}/pay

Requires a signed request (operation="pay").  Commits the reservation
and creates a Payment record in one atomic DB transaction.
"""
import uuid
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.payment import PaymentStatus
from app.schemas.signed import SignedOperationRequest
from app.security.signing import verify_signed_request
from app.services import payment_service

router = APIRouter(tags=["payments"])


class PaymentResponse(BaseModel):
    payment_id: uuid.UUID
    reservation_id: uuid.UUID
    merchant: str
    amount: str
    currency: str
    status: PaymentStatus
    utr_reference: str


@router.post("/reservations/{reservation_id}/pay", response_model=PaymentResponse, status_code=201)
def pay(reservation_id: uuid.UUID, request: SignedOperationRequest, db: Session = Depends(get_db)):
    """Execute a simulated payment for a reserved authority.

    The signed envelope must bind:
        operation = "pay"
        resource_id = str(reservation_id)

    The signing agent must be the holder of the capability backing this reservation.

    On success:
    - The reservation transitions from RESERVED → COMMITTED
    - A Payment record is created with status=SUCCEEDED
    - A simulated UTR reference is returned
    """
    env = request.envelope
    request.validate_operation("pay", reservation_id)

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

    # Verify the signing agent owns the capability backing this reservation
    from app.api.reservations import _assert_reservation_agent
    _assert_reservation_agent(db, reservation_id, env.agent_id)

    payment = payment_service.execute_payment(db, reservation_id)
    db.commit()

    return PaymentResponse(
        payment_id=payment.id,
        reservation_id=payment.reservation_id,
        merchant=payment.merchant,
        amount=str(payment.amount),
        currency=payment.currency,
        status=payment.status,
        utr_reference=payment.utr_reference,
    )
