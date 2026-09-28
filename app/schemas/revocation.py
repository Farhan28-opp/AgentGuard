import uuid
from decimal import Decimal
from pydantic import BaseModel


class RevocationRequest(BaseModel):
    agent_id: uuid.UUID


class RevocationResponse(BaseModel):
    root_capability_id: uuid.UUID
    status: str
    revoked_capabilities: int
    released_reservations: int
    released_amount: Decimal
