import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class Payment(Base):
    """A record of an actual (simulated) payment attempt tied to a reservation.

    Day 6: The simulated UPI rail populates this table.  `utr_reference` holds
    the simulated Unique Transaction Reference (UTR) returned by the payment rail.
    """

    __tablename__ = "payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id = Column(
        UUID(as_uuid=True), ForeignKey("reservations.id"), nullable=False, index=True
    )
    merchant = Column(String(255), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    currency = Column(String(8), nullable=False, default="INR")
    category = Column(String(128), nullable=False)
    status = Column(
        SAEnum(PaymentStatus, name="payment_status"),
        nullable=False,
        default=PaymentStatus.PENDING,
    )
    # Day 6: simulated UTR from the payment rail
    utr_reference = Column(String(128), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    reservation = relationship("Reservation")

    __table_args__ = (CheckConstraint("amount > 0", name="ck_payment_amount_positive"),)
