import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.mandate import MandateStatus


class MandateCreate(BaseModel):
    user_id: uuid.UUID
    name: str
    purpose: str
    currency: str = "INR"
    total_authority: Decimal = Field(gt=0)
    not_before: datetime
    not_after: datetime

    @field_validator("not_after")
    @classmethod
    def _not_after_after_not_before(cls, v: datetime, info):
        not_before = info.data.get("not_before")
        if not_before is not None and v <= not_before:
            raise ValueError("not_after must be strictly after not_before")
        return v


class MandateRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    purpose: str
    currency: str
    total_authority: Decimal
    status: MandateStatus
    not_before: datetime
    not_after: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
