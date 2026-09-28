import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class AuditLog(Base):
    """Provenance / audit record for a capability or mandate event.

    `previous_hash` and `event_hash` are reserved columns for the Day 4
    hash-chained (and later checkpointed) audit trail described in the
    design spec §7. On Day 1 they are simply left null -- no hash-chain
    logic runs yet.
    """

    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    capability_id = Column(
        UUID(as_uuid=True), ForeignKey("capabilities.id"), nullable=True, index=True
    )
    mandate_id = Column(
        UUID(as_uuid=True), ForeignKey("mandates.id"), nullable=True, index=True
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    event_type = Column(String(64), nullable=False)
    actor = Column(String(255), nullable=False)
    amount = Column(Numeric(14, 2), nullable=True)
    payload = Column(JSON, nullable=True)
    previous_hash = Column(String(128), nullable=True)
    event_hash = Column(String(128), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
