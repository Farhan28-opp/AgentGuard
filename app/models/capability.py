import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class CapabilityStatus(str, enum.Enum):
    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class Capability(Base):
    """A delegated unit of financial authority.

    Authority is tracked as an explicit pool, not a single shrinking
    `remaining_amount` field, because a capability can both (a) delegate
    authority to children and (b) hold in-flight payment reservations --
    collapsing those into one number makes it ambiguous which of the two
    a decrement refers to. The pool's four numbers must always satisfy:

        total_authority >= unallocated_authority
                          + reserved_authority
                          + committed_authority

    The remainder (total_authority minus that sum) is, by definition,
    authority currently delegated to this capability's children -- it is
    not stored redundantly here; see
    app/services/capability_service.py::compute_ledger for how it's derived
    from the sum of children's total_authority.

    Delegation (issuing a child) and payment reservation are each atomic
    operations against this pool -- see app/services/capability_service.py
    for delegation (Day 1) and the design spec for payment reservation
    (Day 3, not yet implemented).
    """

    __tablename__ = "capabilities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_capability_id = Column(
        UUID(as_uuid=True), ForeignKey("capabilities.id"), nullable=True, index=True
    )
    root_mandate_id = Column(
        UUID(as_uuid=True), ForeignKey("mandates.id"), nullable=False, index=True
    )

    issued_to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    # Null only for the root capability, whose authority originates from the
    # mandate/user directly rather than from another agent.
    issued_by_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)

    total_authority = Column(Numeric(14, 2), nullable=False)
    unallocated_authority = Column(Numeric(14, 2), nullable=False)
    reserved_authority = Column(Numeric(14, 2), nullable=False, default=0)
    committed_authority = Column(Numeric(14, 2), nullable=False, default=0)

    purpose = Column(String(500), nullable=False)
    category = Column(String(128), nullable=False)
    merchant_allowlist = Column(JSON, nullable=True)
    merchant_denylist = Column(JSON, nullable=True)

    # Day 6: Cryptographic signature of the immutable grant definition
    grant_signature = Column(String(128), nullable=True)

    delegation_depth = Column(Integer, nullable=False, default=0)
    max_delegation_depth = Column(Integer, nullable=False)
    max_fanout = Column(Integer, nullable=False)

    not_before = Column(DateTime(timezone=True), nullable=False)
    not_after = Column(DateTime(timezone=True), nullable=False)

    status = Column(
        SAEnum(CapabilityStatus, name="capability_status"),
        nullable=False,
        default=CapabilityStatus.ACTIVE,
    )
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    parent = relationship("Capability", remote_side=[id], backref="children")
    root_mandate = relationship("Mandate")
    issued_to_agent = relationship("Agent", foreign_keys=[issued_to_agent_id])
    issued_by_agent = relationship("Agent", foreign_keys=[issued_by_agent_id])

    __table_args__ = (
        CheckConstraint("total_authority >= 0", name="ck_capability_total_nonnegative"),
        CheckConstraint("unallocated_authority >= 0", name="ck_capability_unallocated_nonnegative"),
        CheckConstraint("reserved_authority >= 0", name="ck_capability_reserved_nonnegative"),
        CheckConstraint("committed_authority >= 0", name="ck_capability_committed_nonnegative"),
        CheckConstraint(
            "unallocated_authority + reserved_authority + committed_authority <= total_authority",
            name="ck_capability_pool_conservation",
        ),
        CheckConstraint("delegation_depth >= 0", name="ck_capability_depth_nonnegative"),
        CheckConstraint("max_fanout >= 0", name="ck_capability_fanout_nonnegative"),
        CheckConstraint("not_after > not_before", name="ck_capability_window_valid"),
    )
