"""Unit tests for the Ed25519 signing/verification layer (Day 6).

These tests do NOT touch PostgreSQL or the application.
"""
import uuid
import pytest

from app.security.keys import (
    generate_keypair,
    serialize_public_key,
    deserialize_public_key,
    serialize_private_key,
    deserialize_private_key,
    sign_bytes,
    verify_signature,
    sign_and_encode,
    decode_signature,
    sha256_payload,
)
from app.security.signing import canonical_signed_bytes
from app.exceptions import InvalidSignatureError


class TestKeyGeneration:
    def test_generate_keypair_produces_distinct_keys(self):
        priv1, pub1 = generate_keypair()
        priv2, pub2 = generate_keypair()
        # Different keypairs have different public keys
        assert serialize_public_key(pub1) != serialize_public_key(pub2)

    def test_public_key_roundtrip(self):
        _, pub = generate_keypair()
        pem = serialize_public_key(pub)
        recovered = deserialize_public_key(pem)
        assert serialize_public_key(recovered) == pem

    def test_private_key_roundtrip(self):
        priv, _ = generate_keypair()
        pem = serialize_private_key(priv)
        recovered = deserialize_private_key(pem)
        # Verify the recovered key signs correctly
        data = b"test message"
        sig1 = sign_bytes(priv, data)
        sig2 = sign_bytes(recovered, data)
        assert sig1 == sig2


class TestSigningVerification:
    def test_valid_signature_passes(self):
        priv, pub = generate_keypair()
        data = b"canonical envelope bytes"
        sig = sign_bytes(priv, data)
        # Should not raise
        verify_signature(pub, data, sig)

    def test_wrong_key_fails(self):
        priv1, _ = generate_keypair()
        _, pub2 = generate_keypair()
        data = b"some data"
        sig = sign_bytes(priv1, data)
        with pytest.raises(InvalidSignatureError):
            verify_signature(pub2, data, sig)

    def test_tampered_data_fails(self):
        priv, pub = generate_keypair()
        data = b"original data"
        sig = sign_bytes(priv, data)
        with pytest.raises(InvalidSignatureError):
            verify_signature(pub, b"tampered data", sig)

    def test_truncated_signature_fails(self):
        priv, pub = generate_keypair()
        data = b"data"
        sig = sign_bytes(priv, data)
        with pytest.raises(InvalidSignatureError):
            verify_signature(pub, data, sig[:32])  # truncated


class TestBase64Transport:
    def test_sign_encode_decode_verify(self):
        priv, pub = generate_keypair()
        data = b"payload"
        b64_sig = sign_and_encode(priv, data)
        raw_sig = decode_signature(b64_sig)
        verify_signature(pub, data, raw_sig)

    def test_invalid_b64_raises(self):
        with pytest.raises(InvalidSignatureError):
            decode_signature("NOT!VALID!BASE64!!!")


class TestPayloadHash:
    def test_deterministic_hash(self):
        payload = {"amount": "100.00", "currency": "INR", "merchant": "TestShop"}
        h1 = sha256_payload(payload)
        h2 = sha256_payload(payload)
        assert h1 == h2

    def test_key_order_independent(self):
        # Same logical dict, different insertion order
        p1 = {"a": 1, "b": 2, "c": 3}
        p2 = {"c": 3, "a": 1, "b": 2}
        assert sha256_payload(p1) == sha256_payload(p2)

    def test_different_values_different_hash(self):
        p1 = {"amount": "100.00"}
        p2 = {"amount": "5000.00"}
        assert sha256_payload(p1) != sha256_payload(p2)

    def test_hash_format(self):
        h = sha256_payload({"x": 1})
        assert h.startswith("sha256:")
        assert len(h) == 7 + 64  # "sha256:" + 64 hex chars


class TestCanonicalBytes:
    def test_same_input_same_bytes(self):
        agent_id = str(uuid.uuid4())
        b1 = canonical_signed_bytes(agent_id, "reserve", "cap-1", "req-1", "ts", "sha256:" + "a" * 64)
        b2 = canonical_signed_bytes(agent_id, "reserve", "cap-1", "req-1", "ts", "sha256:" + "a" * 64)
        assert b1 == b2

    def test_different_operation_different_bytes(self):
        agent_id = str(uuid.uuid4())
        b1 = canonical_signed_bytes(agent_id, "reserve", "cap-1", "req-1", "ts", "sha256:" + "a" * 64)
        b2 = canonical_signed_bytes(agent_id, "commit", "cap-1", "req-1", "ts", "sha256:" + "a" * 64)
        assert b1 != b2

    def test_different_resource_different_bytes(self):
        agent_id = str(uuid.uuid4())
        b1 = canonical_signed_bytes(agent_id, "reserve", "cap-A", "req-1", "ts", "sha256:" + "a" * 64)
        b2 = canonical_signed_bytes(agent_id, "reserve", "cap-B", "req-1", "ts", "sha256:" + "a" * 64)
        assert b1 != b2
