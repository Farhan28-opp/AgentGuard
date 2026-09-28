import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.reservation import ReservationStatus


class ReserveRequest(BaseModel):
    agent_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    currency: str
    merchant: str
    category: str
    transaction_time: datetime
    idempotency_key: str


class ReservationRead(BaseModel):
    id: uuid.UUID
    capability_id: uuid.UUID
    amount: Decimal
    currency: Optional[str]
    merchant: Optional[str]
    category: Optional[str]
    status: ReservationStatus
    idempotency_key: Optional[str]
    expires_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReservationResponse(ReservationRead):
    remaining_unallocated_authority: Decimal
