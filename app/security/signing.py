"""Signed request envelope and server-side verification pipeline.

An agent signs a canonical byte string formed from:

    agent_id | operation | resource_id | request_id | timestamp | payload_hash

``|`` represents newline separation to avoid ambiguity.

The server:
1. Reconstructs the same canonical bytes.
2. Verifies the Ed25519 signature against the agent's registered public key.
3. Checks the timestamp against the clock-skew window.
4. Checks `request_id` uniqueness in `request_nonces` (anti-replay).
5. Recomputes the payload hash and compares with the signed value.

Operation binding: the signed `operation` and `resource_id` must match
what the route is actually doing, so a valid reserve-signature cannot
be replayed as a commit or against a different capability.
"""
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.exceptions import (
    ExpiredSignedRequestError,
    InvalidSignatureError,
    PayloadIntegrityError,
    RequestReplayError,
    UnknownAgentKeyError,
)
from app.models.agent import Agent
from app.models.nonce import RequestNonce
from app.security.keys import (
    decode_signature,
    deserialize_public_key,
    sha256_payload,
    verify_signature,
)


# ---------------------------------------------------------------------------
# Canonical byte construction
# ---------------------------------------------------------------------------

def canonical_signed_bytes(
    agent_id: str,
    operation: str,
    resource_id: str,
    request_id: str,
    timestamp: str,
    payload_hash: str,
) -> bytes:
    """Build the canonical byte string that is signed by the agent.

    Format (newline-delimited, UTF-8 encoded):
        agent_id\\n
        operation\\n
        resource_id\\n
        request_id\\n
        timestamp\\n
        payload_hash
    """
    parts = [
        str(agent_id),
        str(operation),
        str(resource_id),
        str(request_id),
        str(timestamp),
        str(payload_hash),
    ]
    return "\n".join(parts).encode("utf-8")


# ---------------------------------------------------------------------------
# Agent public key lookup
# ---------------------------------------------------------------------------

def _load_agent_public_key(db: Session, agent_id: uuid.UUID):
    """Return the agent's deserialised public key.  Raises if missing."""
    agent = db.get(Agent, agent_id)
    if agent is None or not agent.public_key:
        raise UnknownAgentKeyError(
            f"No registered public key for agent {agent_id}."
        )
    if agent.status != "active":
        # A suspended (e.g. contained) agent's key is no longer accepted for
        # any operation — identity-level containment.
        raise UnknownAgentKeyError(
            f"Agent {agent.agent_identifier} is {agent.status}; its key is not accepted."
        )
    return deserialize_public_key(agent.public_key), agent


# ---------------------------------------------------------------------------
# Anti-replay nonce check
# ---------------------------------------------------------------------------

def _consume_nonce(
    db: Session,
    request_id: str,
    agent_id: uuid.UUID,
    operation: str,
    timestamp_str: str,
) -> None:
    """Record this request_id as consumed.  Raises RequestReplayError if seen before."""
    existing = db.get(RequestNonce, request_id)
    if existing is not None:
        raise RequestReplayError(
            f"Request ID '{request_id}' has already been consumed. "
            "Signed requests cannot be replayed."
        )
    nonce = RequestNonce(
        request_id=request_id,
        agent_id=agent_id,
        operation=operation,
        timestamp=timestamp_str,
        consumed_at=datetime.now(timezone.utc),
    )
    db.add(nonce)
    db.flush()  # persisted but not committed yet — caller owns the transaction


# ---------------------------------------------------------------------------
# Timestamp validation
# ---------------------------------------------------------------------------

def _validate_timestamp(timestamp_str: str, skew_seconds: int) -> None:
    """Reject requests whose timestamp is outside ±skew_seconds of now."""
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidSignatureError(f"Cannot parse timestamp: {timestamp_str}") from exc

    now = datetime.now(timezone.utc)
    delta = abs((now - ts).total_seconds())
    if delta > skew_seconds:
        raise ExpiredSignedRequestError(
            f"Signed request timestamp is {delta:.0f}s from server time "
            f"(max skew: {skew_seconds}s)."
        )


# ---------------------------------------------------------------------------
# Main verification entry point
# ---------------------------------------------------------------------------

def verify_signed_request(
    db: Session,
    *,
    agent_id: uuid.UUID,
    operation: str,
    resource_id: str,
    request_id: str,
    timestamp: str,
    payload_hash: str,
    signature: str,
    payload_dict: Optional[dict] = None,
) -> Agent:
    """Full server-side verification of a signed request envelope.

    1. Timestamp window check
    2. Load agent + public key (raises if unknown)
    3. Signature verification
    4. Payload hash integrity (if payload_dict supplied)
    5. Anti-replay nonce consumption

    Returns the `Agent` ORM object so the caller can continue with
    capability / reservation ownership checks.

    All mutations (nonce insert) are staged via ``db.flush()`` — the
    caller must commit.
    """
    # 1. Timestamp
    skew = getattr(settings, "clock_skew_seconds", 300)
    _validate_timestamp(timestamp, skew)

    # 2. Load public key
    public_key, agent = _load_agent_public_key(db, agent_id)

    # 3. Signature
    raw_sig = decode_signature(signature)
    signed_bytes = canonical_signed_bytes(
        agent_id=str(agent_id),
        operation=operation,
        resource_id=resource_id,
        request_id=request_id,
        timestamp=timestamp,
        payload_hash=payload_hash,
    )
    verify_signature(public_key, signed_bytes, raw_sig)

    # 4. Payload integrity
    if payload_dict is not None:
        expected_hash = sha256_payload(payload_dict)
        if payload_hash != expected_hash:
            raise PayloadIntegrityError(
                f"Payload hash mismatch: envelope claims {payload_hash} "
                f"but computed {expected_hash}."
            )

    # 5. Anti-replay
    _consume_nonce(db, request_id, agent_id, operation, timestamp)

    return agent
