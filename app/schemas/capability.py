import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.capability import CapabilityStatus


class CapabilityCreate(BaseModel):
    # None for a root capability (carved directly from a mandate); set for
    # any delegated (child) capability.
    parent_capability_id: Optional[uuid.UUID] = None
    root_mandate_id: uuid.UUID
    issued_to_agent_id: uuid.UUID
    issued_by_agent_id: Optional[uuid.UUID] = None

    total_authority: Decimal = Field(ge=0)

    purpose: str
    category: str
    merchant_allowlist: Optional[List[str]] = None
    merchant_denylist: Optional[List[str]] = None

    max_delegation_depth: int = Field(ge=0)
    max_fanout: int = Field(ge=0)

    not_before: datetime
    not_after: datetime

    @field_validator("not_after")
    @classmethod
    def _not_after_after_not_before(cls, v: datetime, info):
        not_before = info.data.get("not_before")
        if not_before is not None and v <= not_before:
            raise ValueError("not_after must be strictly after not_before")
        return v


class CapabilityIssueRequest(BaseModel):
    """Schema for a client requesting to issue a child capability."""
    parent_capability_id: uuid.UUID
    issued_to_agent_id: uuid.UUID
    issued_by_agent_id: Optional[uuid.UUID] = None

    amount: Decimal = Field(ge=0)

    purpose: str
    category: str
    merchant_allowlist: Optional[List[str]] = None
    merchant_denylist: Optional[List[str]] = None

    max_delegation_depth: int = Field(ge=0)
    max_fanout: int = Field(ge=0)

    not_before: datetime
    not_after: datetime

    @field_validator("not_after")
    @classmethod
    def _not_after_after_not_before(cls, v: datetime, info):
        not_before = info.data.get("not_before")
        if not_before is not None and v <= not_before:
            raise ValueError("not_after must be strictly after not_before")
        return v


class CapabilityRead(BaseModel):
    id: uuid.UUID
    parent_capability_id: Optional[uuid.UUID]
    root_mandate_id: uuid.UUID
    issued_to_agent_id: uuid.UUID
    issued_by_agent_id: Optional[uuid.UUID]

    total_authority: Decimal
    unallocated_authority: Decimal
    reserved_authority: Decimal
    committed_authority: Decimal

    purpose: str
    category: str
    merchant_allowlist: Optional[List[str]]
    merchant_denylist: Optional[List[str]]

    delegation_depth: int
    max_delegation_depth: int
    max_fanout: int

    not_before: datetime
    not_after: datetime
    status: CapabilityStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LedgerCapabilityEntry(BaseModel):
    """One row of the per-mandate ledger view: a single capability's pool
    state, plus its derived (not stored) delegated_authority -- see
    capability_service.compute_ledger.
    """

    capability_id: uuid.UUID
    issued_to_agent_id: uuid.UUID
    total_authority: Decimal
    unallocated_authority: Decimal
    delegated_authority: Decimal
    reserved_authority: Decimal
    committed_authority: Decimal
    delegation_depth: int
    status: CapabilityStatus


class MandateLedger(BaseModel):
    mandate_id: uuid.UUID
    total_authority: Decimal
    total_unallocated: Decimal
    total_delegated: Decimal
    total_reserved: Decimal
    total_committed: Decimal
    capabilities: List[LedgerCapabilityEntry]
