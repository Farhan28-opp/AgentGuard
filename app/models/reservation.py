import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class ReservationStatus(str, enum.Enum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"


class Reservation(Base):
    """An in-flight (or settled) hold against a capability's
    unallocated_authority. The table and status machine exist from Day 1;
    the atomic reserve -> commit/release engine that actually moves money
    between pool fields is a Day 3 feature (see design spec §4.2) and is
    intentionally NOT implemented yet -- see app/api/reservations.py.
    """

    __tablename__ = "reservations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    capability_id = Column(
        UUID(as_uuid=True), ForeignKey("capabilities.id"), nullable=False, index=True
    )
    amount = Column(Numeric(14, 2), nullable=False)
    currency = Column(String(8), nullable=True)
    merchant = Column(String(255), nullable=True)
    category = Column(String(128), nullable=True)
    status = Column(
        SAEnum(ReservationStatus, name="reservation_status"),
        nullable=False,
        default=ReservationStatus.RESERVED,
    )
    idempotency_key = Column(String(255), nullable=True, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    capability = relationship("Capability")

    __table_args__ = (CheckConstraint("amount > 0", name="ck_reservation_amount_positive"),)
