import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class Agent(Base):
    """A node in the agent identity hierarchy (root shopping agent, search
    agent, negotiation agent, purchase agent, logistics agent, ...).

    This table exists independently of Capability because agent identity
    and delegated financial authority are different concerns: an agent can
    exist (and be referenced in the ancestor/loop-detection logic) before
    it ever holds a capability, and the same agent could in principle hold
    multiple capabilities over time.

    Day 6: `public_key` stores the PEM-encoded Ed25519 public key for the
    agent.  The corresponding private key is held by the agent's runtime
    process (local dev: ``dev_keys/<identifier>.pem``).
    """

    __tablename__ = "agents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_identifier = Column(String(255), nullable=False, unique=True)
    agent_type = Column(String(64), nullable=False)  # e.g. root, search, negotiation, purchase, logistics
    parent_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    status = Column(String(32), nullable=False, default="active")  # active | suspended
    # The user whose authority this agent acts under (None for system-agent).
    owner_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    # Day 6: PEM-encoded Ed25519 public key (nullable so legacy rows survive migration)
    public_key = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
