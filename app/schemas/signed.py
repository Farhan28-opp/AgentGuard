"""Signed request envelope schemas for Day 6.

These are the wire-format schemas that agents must submit for signed operations.
Each schema includes:
  - The signed envelope fields (agent_id, operation, resource_id, request_id,
    timestamp, payload_hash, signature)
  - The actual operation payload (operation-specific fields)

The server reconstructs canonical bytes from the envelope fields, verifies the
signature against the agent's registered public key, then extracts the payload.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SignedEnvelope(BaseModel):
    """The mandatory cryptographic wrapper that every signed operation must carry.

    Fields:
      agent_id     — UUID of the claiming agent (looked up in DB for public key)
      operation    — one of: reserve, commit, release, revoke, pay, attenuate
      resource_id  — capability_id (for reserve/revoke) or reservation_id (for commit/release/pay)
      request_id   — client-generated UUID; stored in request_nonces for anti-replay
      timestamp    — ISO-8601 UTC string; must be within ±clock_skew_seconds of server time
      payload_hash — sha256:<hex> of canonical JSON of the operation payload
      signature    — URL-safe base64 Ed25519 signature over canonical envelope bytes
    """

    agent_id: uuid.UUID
    operation: str = Field(..., pattern=r"^(reserve|commit|release|revoke|pay|attenuate)$")
    resource_id: str  # UUID string; coerced by the route
    request_id: str   # Unique nonce; UUID recommended
    timestamp: str    # ISO-8601 UTC, e.g. "2026-09-24T10:30:00Z"
    payload_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    signature: str    # URL-safe base64, 88 chars for Ed25519


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------

class SignedReservePayload(BaseModel):
    """Operation payload for a signed reserve request."""
    amount: Decimal = Field(gt=0)
    currency: str
    merchant: str
    category: str
    transaction_time: datetime
    idempotency_key: str


class SignedReserveRequest(BaseModel):
    """Full signed reserve submission = envelope + payload."""
    envelope: SignedEnvelope
    payload: SignedReservePayload

    def validate_operation(self, capability_id: uuid.UUID) -> None:
        if self.envelope.operation != "reserve":
            from app.exceptions import UnauthorizedOperationError
            raise UnauthorizedOperationError(
                f"Envelope operation is '{self.envelope.operation}', expected 'reserve'."
            )
        if self.envelope.resource_id != str(capability_id):
            from app.exceptions import UnauthorizedOperationError
            raise UnauthorizedOperationError(
                f"Envelope resource_id {self.envelope.resource_id!r} does not match "
                f"capability_id {capability_id}."
            )


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

class SignedOperationRequest(BaseModel):
    """Generic signed operation — used for commit, release, pay (no extra payload)."""
    envelope: SignedEnvelope

    def validate_operation(self, expected_op: str, reservation_id: uuid.UUID) -> None:
        if self.envelope.operation != expected_op:
            from app.exceptions import UnauthorizedOperationError
            raise UnauthorizedOperationError(
                f"Envelope operation is '{self.envelope.operation}', expected '{expected_op}'."
            )
        if self.envelope.resource_id != str(reservation_id):
            from app.exceptions import UnauthorizedOperationError
            raise UnauthorizedOperationError(
                f"Envelope resource_id {self.envelope.resource_id!r} does not match "
                f"reservation_id {reservation_id}."
            )


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------

class SignedRevokeRequest(BaseModel):
    """Signed revoke submission — envelope only (no additional payload needed)."""
    envelope: SignedEnvelope

    def validate_operation(self, capability_id: uuid.UUID) -> None:
        if self.envelope.operation != "revoke":
            from app.exceptions import UnauthorizedOperationError
            raise UnauthorizedOperationError(
                f"Envelope operation is '{self.envelope.operation}', expected 'revoke'."
            )
        if self.envelope.resource_id != str(capability_id):
            from app.exceptions import UnauthorizedOperationError
            raise UnauthorizedOperationError(
                f"Envelope resource_id {self.envelope.resource_id!r} does not match "
                f"capability_id {capability_id}."
            )


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------

class AgentRegisterRequest(BaseModel):
    """Register a new agent with a public key."""
    agent_identifier: str = Field(..., min_length=1, max_length=255)
    agent_type: str = Field(default="purchase")
    public_key_pem: str = Field(..., description="PEM-encoded Ed25519 public key")


class AgentRead(BaseModel):
    """Public agent info (never returns private key)."""
    id: uuid.UUID
    agent_identifier: str
    agent_type: str
    status: str
    has_public_key: bool

    @classmethod
    def from_orm(cls, agent) -> "AgentRead":
        return cls(
            id=agent.id,
            agent_identifier=agent.agent_identifier,
            agent_type=agent.agent_type,
            status=agent.status,
            has_public_key=bool(agent.public_key),
        )
