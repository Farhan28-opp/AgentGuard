import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class MandateStatus(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    COMPLETED = "completed"


class Mandate(Base):
    """The user's original financial authorization -- the root of the
    authority tree. A mandate's total_authority is the ceiling that every
    capability descending from it (directly or transitively) must stay
    within; see Capability's pool fields and app/services/authority_service.py
    for how that ceiling is enforced.
    """

    __tablename__ = "mandates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)
    purpose = Column(String(500), nullable=False)
    currency = Column(String(8), nullable=False, default="INR")
    total_authority = Column(Numeric(14, 2), nullable=False)
    status = Column(
        SAEnum(MandateStatus, name="mandate_status"),
        nullable=False,
        default=MandateStatus.ACTIVE,
    )
    not_before = Column(DateTime(timezone=True), nullable=False)
    not_after = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("User")

    __table_args__ = (
        CheckConstraint("total_authority >= 0", name="ck_mandate_total_nonnegative"),
        CheckConstraint("not_after > not_before", name="ck_mandate_window_valid"),
    )
