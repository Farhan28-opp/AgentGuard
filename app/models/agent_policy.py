"""The user's agent policy and the standing authority it was issued as.

overall_authority and allowed_merchants are ENFORCED BY THE LEDGER: they
become the standing mandate / the Main Agent's root capability total and
merchant allowlist (changing them re-issues that authority).
per_transaction_limit, allowed_categories and approval_mode are enforced by
the backend policy check before any authority is delegated or settled.
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base

APPROVAL_MODES = ("always", "above_threshold", "autonomous")


class AgentPolicy(Base):
    __tablename__ = "agent_policies"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    overall_authority = Column(Numeric(12, 2), nullable=False)
    per_transaction_limit = Column(Numeric(12, 2), nullable=False)
    allowed_categories = Column(JSON, nullable=False)
    allowed_merchants = Column(JSON, nullable=False)
    approval_mode = Column(String(24), nullable=False, default="always")
    approval_threshold = Column(Numeric(12, 2), nullable=False)

    # Standing authority issued from this policy (see policy_service).
    mandate_id = Column(UUID(as_uuid=True), nullable=True)
    root_capability_id = Column(UUID(as_uuid=True), nullable=True)
    main_agent_id = Column(UUID(as_uuid=True), nullable=True)
    search_agent_id = Column(UUID(as_uuid=True), nullable=True)
    search_capability_id = Column(UUID(as_uuid=True), nullable=True)
    negotiation_agent_id = Column(UUID(as_uuid=True), nullable=True)
    negotiation_capability_id = Column(UUID(as_uuid=True), nullable=True)
    purchase_agent_id = Column(UUID(as_uuid=True), nullable=True)
    wallet_agent_id = Column(UUID(as_uuid=True), nullable=True)

    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        nullable=False)
