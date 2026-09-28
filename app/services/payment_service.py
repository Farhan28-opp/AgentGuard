"""Simulated Payment Rail — Day 6.

Execution path:
    Agent submits SignedOperationRequest (operation="pay")
        ↓
    Signature/replay verification
        ↓
    commit_reservation() — atomic authority move reserved→committed
        ↓
    Payment record created (status=SUCCEEDED)
        ↓
    Simulated UPI reference returned

No real UPI integration. The simulated rail always succeeds for valid
committed reservations and produces a fake UTR (Unique Transaction Reference).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import NotFoundError, InvalidReservationTransitionError
from app.models.payment import Payment, PaymentStatus
from app.models.reservation import Reservation, ReservationStatus
from app.services import reservation_service


def execute_payment(db: Session, reservation_id: uuid.UUID) -> Payment:
    """Commit the reservation and create a Payment record atomically.

    1. Commit the reservation (moves reserved → committed authority).
    2. Create a Payment row with status=SUCCEEDED.
    3. Return the Payment — caller must commit the transaction.

    The reservation must be in RESERVED status and not expired.
    """
    # Commit the reservation — this enforces all Day-3 state-machine rules
    # (and holds row locks on the capability and the reservation until the
    # caller's transaction ends).
    reservation = reservation_service.commit_reservation(db, reservation_id)

    # Double-settlement guard. commit_reservation() is idempotent for an
    # already-COMMITTED reservation, so without this check a second pay call
    # would create a second Payment row for the same authority. The
    # reservation row lock taken above serialises concurrent pay attempts.
    existing = db.scalar(select(Payment).where(Payment.reservation_id == reservation.id))
    if existing is not None:
        raise InvalidReservationTransitionError(
            f"Reservation {reservation.id} is already settled by payment {existing.id}."
        )

    # Deterministic simulated UTR from reservation_id (the same reference is
    # recorded on Drunix with the Commit -- see ledger_sync).
    from app.services.ledger_sync import simulated_payment_reference
    utr = simulated_payment_reference(reservation_id)

    payment = Payment(
        id=uuid.uuid4(),
        reservation_id=reservation.id,
        merchant=reservation.merchant or "unknown",
        amount=reservation.amount,
        currency=reservation.currency or "INR",
        category=reservation.category or "general",
        status=PaymentStatus.SUCCEEDED,
        created_at=datetime.now(timezone.utc),
        utr_reference=utr,
    )
    db.add(payment)
    db.flush()
    return payment
