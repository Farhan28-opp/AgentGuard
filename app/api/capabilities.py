"""Capabilities API — Day 6 update.

The revoke endpoint now requires a SignedRevokeRequest.  The issuing
agent (for non-root capabilities) or the system root agent must sign
the revocation.  Cryptographic identity is verified first; then the
existing Day-4 authorization rules (issuer check) are applied.
"""
import uuid
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.capability import CapabilityIssueRequest, CapabilityRead
from app.schemas.signed import SignedRevokeRequest
from app.security.signing import verify_signed_request
from app.services import capability_service, mandate_service, revocation_service
from app.schemas.revocation import RevocationResponse

router = APIRouter(tags=["capabilities"])


@router.post("/capabilities", response_model=CapabilityRead, status_code=201)
def issue_capability(payload: CapabilityIssueRequest, db: Session = Depends(get_db)):
    capability = capability_service.issue_capability(db, payload)
    db.commit()
    db.refresh(capability)
    return capability


@router.get("/capabilities/{capability_id}", response_model=CapabilityRead)
def get_capability(capability_id: uuid.UUID, db: Session = Depends(get_db)):
    return capability_service.get_capability(db, capability_id)


@router.get("/mandates/{mandate_id}/capabilities", response_model=List[CapabilityRead])
def list_capabilities(mandate_id: uuid.UUID, db: Session = Depends(get_db)):
    mandate_service.get_mandate(db, mandate_id)
    return capability_service.list_capabilities_for_mandate(db, mandate_id)


@router.post("/capabilities/{capability_id}/revoke", response_model=RevocationResponse)
def revoke_capability(
    capability_id: uuid.UUID,
    request: SignedRevokeRequest,
    db: Session = Depends(get_db),
):
    """Revoke a capability subtree with a signed request.

    The signing agent must be:
    - For non-root capabilities: the agent that issued the capability
    - For root capabilities: an agent with agent_type == 'root'

    The envelope must bind:
        operation = "revoke"
        resource_id = str(capability_id)
    """
    env = request.envelope

    # 1. Operation binding (fast check before crypto)
    request.validate_operation(capability_id)

    # 2. Cryptographic verification (no payload — empty dict hash)
    verify_signed_request(
        db,
        agent_id=env.agent_id,
        operation=env.operation,
        resource_id=env.resource_id,
        request_id=env.request_id,
        timestamp=env.timestamp,
        payload_hash=env.payload_hash,
        signature=env.signature,
    )

    # 3. Existing Day-4 authorization + revocation (uses agent_id from verified envelope)
    response = revocation_service.revoke_capability(db, capability_id, env.agent_id)
    db.commit()
    return response
