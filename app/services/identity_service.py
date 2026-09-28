"""Agent identity service for Day 6.

Handles:
  - Agent registration (with public key)
  - Public key validation and update
  - Agent lookup
"""
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import NotFoundError, UnknownAgentKeyError
from app.models.agent import Agent
from app.security.keys import deserialize_public_key, serialize_public_key


def register_agent(
    db: Session,
    agent_identifier: str,
    agent_type: str,
    public_key_pem: str,
) -> Agent:
    """Create a new agent with a registered Ed25519 public key.

    Validates the PEM before persisting — rejects malformed keys early.
    """
    # Validate the key parses correctly before storing
    try:
        deserialize_public_key(public_key_pem)
    except Exception as exc:
        from app.exceptions import InvalidSignatureError
        raise InvalidSignatureError(
            f"Supplied public_key_pem is not a valid Ed25519 public key: {exc}"
        ) from exc

    agent = Agent(
        id=uuid.uuid4(),
        agent_identifier=agent_identifier,
        agent_type=agent_type,
        status="active",
        public_key=public_key_pem,
    )
    db.add(agent)
    db.flush()
    return agent


def update_agent_public_key(
    db: Session,
    agent_id: uuid.UUID,
    public_key_pem: str,
) -> Agent:
    """Replace a registered agent's public key (key rotation)."""
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id} not found.")

    try:
        deserialize_public_key(public_key_pem)
    except Exception as exc:
        from app.exceptions import InvalidSignatureError
        raise InvalidSignatureError(
            f"Supplied public_key_pem is not a valid Ed25519 public key: {exc}"
        ) from exc

    agent.public_key = public_key_pem
    db.flush()
    return agent


def get_agent(db: Session, agent_id: uuid.UUID) -> Agent:
    """Fetch an agent by ID or raise NotFoundError."""
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise NotFoundError(f"Agent {agent_id} not found.")
    return agent


def get_agent_by_identifier(db: Session, identifier: str) -> Optional[Agent]:
    """Fetch an agent by its string identifier."""
    return db.scalar(select(Agent).where(Agent.agent_identifier == identifier))


# ---------------------------------------------------------------------------
# Locally hosted agent runtimes (product + demo surfaces)
# ---------------------------------------------------------------------------
#
# In this prototype the agent runtimes (Main / Search / Negotiation /
# Purchase) run inside the same process as the AgentGuard server. Each one
# still gets its own Ed25519 keypair: the private key lives only in the local
# dev keystore (dev_keys/, never committed or packaged) and the server side
# only ever sees the registered PUBLIC key in `agents.public_key`.

def provision_local_agent(
    db: Session,
    agent_identifier: str,
    agent_type: str,
    parent_agent_id: Optional[uuid.UUID] = None,
    owner_user_id: Optional[uuid.UUID] = None,
) -> Agent:
    """Provision a locally hosted agent runtime: private key from the key
    backend (random+dev_keys file, or derived from AGENT_KEY_SEED), PUBLIC key
    registered in PostgreSQL. Stages the insert — the caller commits."""
    from app.security.keys import new_agent_private_key

    private_key = new_agent_private_key(agent_identifier)
    agent = register_agent(db, agent_identifier, agent_type,
                           serialize_public_key(private_key.public_key()))
    agent.parent_agent_id = parent_agent_id
    agent.owner_user_id = owner_user_id
    db.flush()
    return agent


def ensure_agent_key(db: Session, agent: Agent) -> bool:
    """Make sure this machine can sign for ``agent``: if its private key is
    unavailable (fresh checkout / redeploy) or no longer matches the
    registered public key (key backend changed), provision a new key and
    rotate the registered public key. Returns True if a rotation happened.
    Grants the agent signed with its old key stop verifying — surfaced by
    the Security Center."""
    from app.security.keys import load_dev_private_key, new_agent_private_key

    try:
        current = serialize_public_key(load_dev_private_key(agent.agent_identifier).public_key())
    except FileNotFoundError:
        current = None
    if current is not None and current.strip() == (agent.public_key or "").strip():
        return False
    private_key = new_agent_private_key(agent.agent_identifier)
    update_agent_public_key(db, agent.id, serialize_public_key(private_key.public_key()))
    return True


def ensure_system_agent(db: Session) -> Agent:
    """The system agent signs root capability grants and is attributed with
    root-level containment. Created on first use; key kept usable."""
    agent = get_agent_by_identifier(db, "system-agent")
    if agent is None:
        return provision_local_agent(db, "system-agent", "root")
    if agent.agent_type != "root":
        # Older builds registered it as "system"; root revocation needs "root".
        agent.agent_type = "root"
    ensure_agent_key(db, agent)
    return agent
