"""Agent-level containment, applied after the core HIGH-risk containment.

The core (reservation_service) already revokes the capability subtree that
the anomalous request was made against and releases its holds. For a
persistent agent that can hold several capabilities (one per task), the
product policy additionally:

  1. suspends the agent identity — ``verify_signed_request`` refuses a
     suspended agent's key, so ANY future signed request is rejected;
  2. revokes every other ACTIVE capability the agent holds (recursive,
     releasing their holds).

These are system control actions (not agent-signed requests) and are
attributed in the audit log. A suspended agent is never silently replaced:
the user replaces it explicitly, which provisions a new identity.
"""
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.capability import Capability, CapabilityStatus
from app.services import audit_service, revocation_service


def contain_agent(db: Session, agent: Agent, *, reason: str,
                  user_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
    agent = db.get(Agent, agent.id, with_for_update=True, populate_existing=True)
    agent.status = "suspended"
    extra_revoked = released = 0
    caps = db.scalars(select(Capability).where(
        Capability.issued_to_agent_id == agent.id,
        Capability.status == CapabilityStatus.ACTIVE)).all()
    for cap in caps:
        revoker = cap.issued_by_agent_id
        if revoker is None:
            revoker = db.scalar(select(Agent.id).where(Agent.agent_identifier == "system-agent"))
        rev = revocation_service.revoke_capability(db, cap.id, revoker)
        extra_revoked += rev.revoked_capabilities
        released += rev.released_reservations
    audit_service.record(db, "AGENT_SUSPENDED", "agentguard",
                         {"agent": agent.agent_identifier, "reason": reason,
                          "other_capabilities_revoked": extra_revoked,
                          "holds_released": released,
                          "effect": "agent key no longer accepted for signed requests"},
                         user_id=user_id)
    db.flush()
    return {"agent": agent.agent_identifier, "agent_status": agent.status,
            "other_capabilities_revoked": extra_revoked, "other_holds_released": released}
