"""Ed25519 keypair utilities for AgentGuard Day 6.

Responsibilities:
  - generate_keypair()          → (private_key, public_key) objects
  - serialize_public_key()      → PEM string stored in DB
  - deserialize_public_key()    → public key object from PEM
  - serialize_private_key()     → PEM string for local dev keystore
  - deserialize_private_key()   → private key object from PEM
  - sign_bytes(private_key, data) → raw bytes (64-byte Ed25519 sig)
  - verify_signature(public_key, data, signature) → None (raises on failure)

Private keys MUST NOT be stored in PostgreSQL or committed to Git.
The local dev keystore is ``dev_keys/`` (excluded via .gitignore).
"""
import base64
import hashlib
import json
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature as _CryptoInvalidSignature


# ---------------------------------------------------------------------------
# Keypair generation
# ---------------------------------------------------------------------------

def generate_keypair() -> Tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a fresh Ed25519 keypair."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def serialize_public_key(public_key: Ed25519PublicKey) -> str:
    """Encode a public key as a PEM string (for storage in `agents.public_key`)."""
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem.decode("utf-8")


def deserialize_public_key(pem: str) -> Ed25519PublicKey:
    """Load an Ed25519 public key from its PEM representation."""
    return serialization.load_pem_public_key(pem.encode("utf-8"))


def serialize_private_key(private_key: Ed25519PrivateKey) -> str:
    """Encode a private key as a PEM string for the local dev keystore.

    No encryption is applied — the keystore directory must be restricted
    to local development only and must never be committed to Git.
    """
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


def deserialize_private_key(pem: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from its PEM string."""
    return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)


# ---------------------------------------------------------------------------
# Signing and verification
# ---------------------------------------------------------------------------

def sign_bytes(private_key: Ed25519PrivateKey, data: bytes) -> bytes:
    """Sign arbitrary bytes with Ed25519.  Returns 64-byte raw signature."""
    return private_key.sign(data)


def verify_signature(
    public_key: Ed25519PublicKey,
    data: bytes,
    signature: bytes,
) -> None:
    """Verify an Ed25519 signature.

    Raises ``InvalidSignatureError`` (AgentGuard domain error) on failure.
    Does NOT raise on success.
    """
    try:
        public_key.verify(signature, data)
    except _CryptoInvalidSignature as exc:
        from app.exceptions import InvalidSignatureError
        raise InvalidSignatureError("Ed25519 signature verification failed.") from exc


# ---------------------------------------------------------------------------
# Convenience: sign and encode / decode for transport
# ---------------------------------------------------------------------------

def sign_and_encode(private_key: Ed25519PrivateKey, data: bytes) -> str:
    """Sign ``data`` and return the signature as URL-safe base64."""
    raw_sig = sign_bytes(private_key, data)
    return base64.urlsafe_b64encode(raw_sig).decode("ascii")


def decode_signature(b64_signature: str) -> bytes:
    """Decode a URL-safe base64 signature string to raw bytes."""
    import re
    # Validate that the string is actually valid base64url before decoding
    if not re.match(r'^[A-Za-z0-9_\-]+=*$', b64_signature):
        from app.exceptions import InvalidSignatureError
        raise InvalidSignatureError(f"Signature contains invalid base64url characters.")
    try:
        # Add padding if needed
        padding = 4 - len(b64_signature) % 4
        if padding != 4:
            b64_signature = b64_signature + '=' * padding
        result = base64.urlsafe_b64decode(b64_signature)
        # Ed25519 signatures are always 64 bytes
        if len(result) != 64:
            from app.exceptions import InvalidSignatureError
            raise InvalidSignatureError(f"Signature has wrong length: {len(result)} bytes (expected 64).")
        return result
    except Exception as exc:
        from app.exceptions import InvalidSignatureError
        raise InvalidSignatureError(f"Cannot decode signature: {exc}") from exc


# ---------------------------------------------------------------------------
# Payload hash
# ---------------------------------------------------------------------------

def sha256_payload(payload_dict: dict) -> str:
    """Produce a deterministic SHA-256 hash of a dict.

    Keys are sorted, no extra whitespace, so the same logical payload
    always produces the same hash regardless of insertion order.
    """
    canonical = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Local dev keystore helpers
# ---------------------------------------------------------------------------

_DEFAULT_DEV_KEYS_DIR = Path(__file__).resolve().parent.parent.parent / "dev_keys"

# Two key backends for the in-process agent runtimes:
#   * "file"    (default)  random keypair per agent, private key written to
#                          the local dev keystore (dev_keys/, git-ignored).
#   * "derived" (AGENT_KEY_SEED set)  private key = HMAC-SHA256(seed, agent
#                          identifier). Nothing is written to disk, keys
#                          survive redeploys on ephemeral filesystems
#                          (Railway), and the only secret is an environment
#                          variable — a stand-in for a KMS/HSM.
# Either way only PUBLIC keys are stored in PostgreSQL.


def _seed() -> str:
    from app.config import settings
    return settings.agent_key_seed or ""


def key_backend() -> str:
    return "derived" if _seed() else "file"


def _derived_private_key(agent_identifier: str) -> Ed25519PrivateKey:
    import hmac
    digest = hmac.new(_seed().encode("utf-8"), f"agentguard-agent-key:{agent_identifier}".encode("utf-8"),
                      hashlib.sha256).digest()
    return Ed25519PrivateKey.from_private_bytes(digest)


def dev_keys_dir() -> Path:
    """Directory of the local dev keystore (file backend).

    Defaults to ``<project>/dev_keys``; override with ``DEV_KEYS_DIR``.
    Holds unencrypted private keys: git-ignored, never packaged.
    """
    from app.config import settings
    return Path(settings.dev_keys_dir) if settings.dev_keys_dir else _DEFAULT_DEV_KEYS_DIR


def new_agent_private_key(agent_identifier: str) -> Ed25519PrivateKey:
    """Private key for a newly provisioned agent runtime (backend-aware)."""
    if key_backend() == "derived":
        return _derived_private_key(agent_identifier)
    private_key, _ = generate_keypair()
    save_dev_private_key(agent_identifier, private_key)
    return private_key


def save_dev_private_key(agent_identifier: str, private_key: Ed25519PrivateKey) -> Path:
    """Persist a private key to ``<dev_keys_dir>/<identifier>.pem`` (mode 0600)."""
    directory = dev_keys_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{agent_identifier}.pem"
    path.write_text(serialize_private_key(private_key))
    path.chmod(0o600)
    return path


def load_dev_private_key(agent_identifier: str) -> Ed25519PrivateKey:
    """Load an agent runtime's private key (derived backend, or the file
    ``<dev_keys_dir>/<identifier>.pem``)."""
    if key_backend() == "derived":
        return _derived_private_key(agent_identifier)
    path = dev_keys_dir() / f"{agent_identifier}.pem"
    if not path.exists():
        raise FileNotFoundError(
            f"No dev key found for agent '{agent_identifier}' at {path}. "
            "Keys are generated locally when agents are registered; they are "
            "never shipped with the project."
        )
    return deserialize_private_key(path.read_text())
