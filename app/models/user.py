import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, String
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class User(Base):
    """The origin of a root financial mandate. Day 1 has no user-facing
    signup/API for this table -- users are provisioned via the seed script
    (see seed.py). See README "Assumptions" for why."""

    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    # Demo data is flagged so /demo/reset can delete it without touching
    # anything else in the database.
    is_demo = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
