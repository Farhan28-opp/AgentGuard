"""Persistent payment request: an agent shopping task, or (refs.kind ==
"direct") a user-authorized direct payment — survives restarts and lets
several server workers share state. The authority ledger remains the source
of truth for money; this row records the orchestration state machine:

  CREATED → RUNNING → AWAITING_AUTHORIZATION → AUTHORIZED → COMPLETED
  failure: FAILED | REVIEW_REQUIRED | CONTAINED | CANCELLED | EXPIRED | PAYMENT_FAILED
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


def _now():
    return datetime.now(timezone.utc)


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id = Column(String(16), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(32), nullable=False, index=True)
    instruction = Column(String(200), nullable=False)
    budget = Column(Numeric(12, 2), nullable=False)
    category = Column(String(32), nullable=False)
    cart_id = Column(UUID(as_uuid=True), nullable=True)
    steps = Column(JSON, nullable=False, default=list)
    refs = Column(JSON, nullable=False, default=dict)
    authorization = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    simulation = Column(JSON, nullable=True)
    error = Column(JSON, nullable=True)
    # Consumer "remove from Activity": presentation only. The task, its
    # payment, order, ledger journal and audit events are never deleted.
    hidden_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
