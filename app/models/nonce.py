"""Anti-replay nonce table for Day 6.

Each successfully verified signed request is recorded here so that a
captured signed message cannot be replayed.  The `request_id` (UUID)
is the primary key — a second insert with the same value will either
raise an IntegrityError (caught by the verification layer) or be
pre-empted by the explicit existence check in `signing.py`.

Old nonces can be purged by a background job once they are older than
the clock-skew TTL, but purging is not required for correctness — a
stale nonce simply keeps the request permanently non-replayable.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class RequestNonce(Base):
    """One row per consumed signed request.  Primary key is the request_id
    supplied by the agent in its signed envelope."""

    __tablename__ = "request_nonces"

    request_id = Column(String(128), primary_key=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    operation = Column(String(64), nullable=False)
    timestamp = Column(String(64), nullable=False)  # original timestamp string from envelope
    consumed_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
