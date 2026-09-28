import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class LedgerTransaction(Base):
    """Journal of every call AgentGuard made to the Drunix ledger.

    One row per attempted transaction: what was asked (function + arguments),
    what Drunix answered (transaction id, block, validation code, or the
    chaincode's rejection code) and how long it took.

    Rows are written in their own database transaction (see
    ``ledger_sync._journal``) so that a Drunix *rejection* stays on record even
    though the AgentGuard business transaction that asked for it rolls back.

    ``outcome`` values:
      VALID          committed on Drunix, validation code VALID
      REJECTED       the agentauth chaincode refused the operation
      CONFLICT       lost an MVCC race (another transaction changed the state first)
      INVALID_COMMIT ordered but not committed as VALID for another reason
      UNAVAILABLE    the bridge or the Drunix network could not be reached
      TIMEOUT        no final answer in time (outcome on Drunix unknown)
      SYNC_PENDING   a restrictive operation (release / return / revoke) that
                     AgentGuard applied but Drunix has not confirmed yet
      SYNCED         a former SYNC_PENDING entry that has since been applied
      RECOVERED      a retried commit that Drunix had in fact already applied
    """

    __tablename__ = "ledger_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    function = Column(String(64), nullable=False)
    entity_type = Column(String(32), nullable=False)
    entity_id = Column(String(64), nullable=False, index=True)
    args = Column(JSON, nullable=False)
    outcome = Column(String(32), nullable=False, index=True)
    source = Column(String(32), nullable=False, default="agentguard")
    tx_id = Column(String(128), nullable=True, index=True)
    block_number = Column(BigInteger, nullable=True)
    validation_code = Column(String(64), nullable=True)
    category = Column(String(64), nullable=True)
    code = Column(String(64), nullable=True)
    message = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    attempts = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
