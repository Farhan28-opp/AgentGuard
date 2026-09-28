"""Agent registration API — Day 6."""
import uuid
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.signed import AgentRegisterRequest, AgentRead
from app.services.identity_service import register_agent, get_agent

router = APIRouter(tags=["agents"])


@router.post("/agents", response_model=AgentRead, status_code=201)
def create_agent(payload: AgentRegisterRequest, db: Session = Depends(get_db)):
    """Register a new agent with an Ed25519 public key.

    The public key must be in PEM format (SubjectPublicKeyInfo / PKCS#8).
    The server validates the key before storing it.
    Private keys are NEVER submitted here or stored server-side.
    """
    agent = register_agent(
        db,
        agent_identifier=payload.agent_identifier,
        agent_type=payload.agent_type,
        public_key_pem=payload.public_key_pem,
    )
    db.commit()
    return AgentRead.from_orm(agent)


@router.get("/agents/{agent_id}", response_model=AgentRead)
def read_agent(agent_id: uuid.UUID, db: Session = Depends(get_db)):
    """Get agent info (never exposes the private key)."""
    agent = get_agent(db, agent_id)
    return AgentRead.from_orm(agent)
