"""Capability Grant Signatures / Attestation for Day 6.

Provides the cryptographic guarantee that a capability grant was
authorized by its issuer, and that its immutable parameters
(authority, scope, constraints) have not been tampered with.

The capability signature explicitly EXCLUDES mutable ledger state
like unallocated_authority, reserved_authority, committed_authority,
and status.
"""
from typing import Dict, Any
from datetime import timezone

from app.models.capability import Capability
from app.models.agent import Agent
from app.exceptions import InvalidSignatureError
from app.security.keys import (
    load_dev_private_key,
    deserialize_public_key,
    sign_and_encode,
    decode_signature,
    verify_signature,
    sha256_payload,
)


def canonicalize_capability_grant(capability: Capability) -> Dict[str, Any]:
    """Extract the immutable fields of a capability into a deterministic dict.

    This explicitly ignores mutable ledger state.
    """
    return {
        "capability_id": str(capability.id),
        "parent_capability_id": str(capability.parent_capability_id) if capability.parent_capability_id else None,
        "root_mandate_id": str(capability.root_mandate_id),
        "issued_to_agent_id": str(capability.issued_to_agent_id),
        "issued_by_agent_id": str(capability.issued_by_agent_id) if capability.issued_by_agent_id else None,
        "total_authority": f"{capability.total_authority:.2f}" if capability.total_authority else "0.00",
        "purpose": capability.purpose,
        "category": capability.category,
        "merchant_allowlist": capability.merchant_allowlist,
        "merchant_denylist": capability.merchant_denylist,
        "delegation_depth": capability.delegation_depth,
        "max_delegation_depth": capability.max_delegation_depth,
        "max_fanout": capability.max_fanout,
        "not_before": capability.not_before.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "not_after": capability.not_after.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


def sign_capability_grant(capability: Capability, issuer_agent: Agent) -> str:
    """Sign the immutable capability grant using the issuer's private key.

    Loads the private key from the local dev keystore using the agent's identifier.
    Returns the base64 URL-safe signature string.
    """
    grant_payload = canonicalize_capability_grant(capability)
    payload_hash = sha256_payload(grant_payload)

    # In a real system, the agent process would sign this locally.
    # For this architecture, we emulate the agent by loading its dev private key.
    private_key = load_dev_private_key(issuer_agent.agent_identifier)

    # We sign the canonical payload hash string (e.g. "sha256:abcd...") encoded as bytes
    signature = sign_and_encode(private_key, payload_hash.encode("utf-8"))
    return signature


def verify_capability_grant(capability: Capability, issuer_agent: Agent) -> None:
    """Verify that the capability's grant_signature matches its immutable fields.

    Validates against the registered public key of the given issuer_agent.
    Raises InvalidSignatureError if verification fails.
    """
    if not capability.grant_signature:
        raise InvalidSignatureError(f"Capability {capability.id} has no grant_signature.")

    if not issuer_agent.public_key:
        raise InvalidSignatureError(f"Issuer agent {issuer_agent.id} has no registered public key.")

    grant_payload = canonicalize_capability_grant(capability)
    payload_hash = sha256_payload(grant_payload)

    public_key = deserialize_public_key(issuer_agent.public_key)
    raw_sig = decode_signature(capability.grant_signature)
    
    verify_signature(public_key, payload_hash.encode("utf-8"), raw_sig)
