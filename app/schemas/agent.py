import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AgentCreate(BaseModel):
    agent_identifier: str
    agent_type: str
    parent_agent_id: Optional[uuid.UUID] = None


class AgentRead(BaseModel):
    id: uuid.UUID
    agent_identifier: str
    agent_type: str
    parent_agent_id: Optional[uuid.UUID]
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
