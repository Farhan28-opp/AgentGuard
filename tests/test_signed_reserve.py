"""E2E integration tests for signed reserve/commit/release and payment rail.

These tests go through the FastAPI TestClient so they exercise the full
signature verification pipeline against real PostgreSQL.
"""
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models.mandate import Mandate, MandateStatus
from app.models.user import User
from app.security.keys import sha256_payload
from tests.conftest import (
    make_agent_with_key,
    create_root_capability,
    create_child_capability,
    build_signed_envelope,
)

client = TestClient(app)


def _setup_signed_tree():
    """Create mandate → root → child with keypairs.

    Returns plain primitives only (no ORM objects) to avoid DetachedInstanceError.
    """
    db = SessionLocal()
    try:
        user = User(id=uuid.uuid4(), name="SignedTest")
        db.add(user)
        db.commit()

        now = datetime.now(timezone.utc)
        mandate = Mandate(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Signed Mandate",
            purpose="testing",
            currency="INR",
            total_authority=Decimal("10000"),
            status=MandateStatus.ACTIVE,
            not_before=now,
            not_after=now + timedelta(days=7),
        )
        db.add(mandate)
        db.commit()

        root_agent, root_priv = make_agent_with_key(db, f"root-{uuid.uuid4()}", "root")
        child_agent, child_priv = make_agent_with_key(db, f"purchase-{uuid.uuid4()}", "purchase")

        root_cap = create_root_capability(db, mandate, root_agent, total_authority=Decimal("5000"))
        child_cap = create_child_capability(db, root_cap, child_agent, total_authority=Decimal("2000"))

        # Capture UUIDs and private keys before closing
        return {
            "mandate_currency": mandate.currency,
            "child_cap_id": child_cap.id,
            "child_cap_category": child_cap.category,
            "root_agent_id": root_agent.id,
            "root_priv": root_priv,
            "child_agent_id": child_agent.id,
            "child_priv": child_priv,
        }
    finally:
        db.close()


class TestSignedReserve:
    def test_valid_signed_reserve_succeeds(self):
        ctx = _setup_signed_tree()

        now_str = datetime.now(timezone.utc).isoformat()
        payload = {
            "amount": "100.00",
            "currency": ctx["mandate_currency"],
            "merchant": "TestShop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        envelope = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="reserve",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=payload,
        )

        res = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": envelope, "payload": payload},
        )
        assert res.status_code == 201, res.json()
        assert res.json()["status"] == "reserved"

    def test_wrong_agent_key_rejected(self):
        """Agent A's key submitted with Agent B's agent_id → 401."""
        ctx = _setup_signed_tree()

        now_str = datetime.now(timezone.utc).isoformat()
        payload = {
            "amount": "100.00",
            "currency": ctx["mandate_currency"],
            "merchant": "TestShop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        # Sign with root private key but claim to be the child agent
        envelope = build_signed_envelope(
            private_key=ctx["root_priv"],  # WRONG KEY
            agent_id=ctx["child_agent_id"],
            operation="reserve",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=payload,
        )
        res = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": envelope, "payload": payload},
        )
        assert res.status_code == 401, res.json()
        assert "InvalidSignatureError" in res.json()["error"]

    def test_replay_rejected(self):
        """Submitting the same request_id twice → 409 RequestReplayError."""
        ctx = _setup_signed_tree()

        now_str = datetime.now(timezone.utc).isoformat()
        request_id = str(uuid.uuid4())
        payload = {
            "amount": "50.00",
            "currency": ctx["mandate_currency"],
            "merchant": "TestShop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        envelope = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="reserve",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=payload,
            request_id=request_id,
        )

        r1 = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": envelope, "payload": payload},
        )
        assert r1.status_code == 201, r1.json()

        # Second request with same request_id → replay
        r2 = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": envelope, "payload": payload},
        )
        assert r2.status_code == 409, r2.json()
        assert "RequestReplayError" in r2.json()["error"]

    def test_tampered_payload_rejected(self):
        """Envelope signed for amount=100 submitted with amount=5000 → 400 PayloadIntegrityError."""
        ctx = _setup_signed_tree()

        now_str = datetime.now(timezone.utc).isoformat()
        original_payload = {
            "amount": "100.00",
            "currency": ctx["mandate_currency"],
            "merchant": "TestShop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        envelope = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="reserve",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=original_payload,
        )

        # Tamper AFTER signing
        tampered_payload = dict(original_payload)
        tampered_payload["amount"] = "5000.00"

        res = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": envelope, "payload": tampered_payload},
        )
        assert res.status_code == 400, res.json()
        assert "PayloadIntegrityError" in res.json()["error"]

    def test_wrong_operation_binding_rejected(self):
        """Envelope bytes include operation='commit'; mutating to 'reserve' after signing fails verification."""
        ctx = _setup_signed_tree()

        now_str = datetime.now(timezone.utc).isoformat()
        payload = {
            "amount": "100.00",
            "currency": ctx["mandate_currency"],
            "merchant": "TestShop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        # Sign with 'commit' in the envelope bytes
        envelope = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="commit",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=payload,
        )
        # Swap back to 'reserve' so Pydantic pattern accepts it, but signature covers 'commit'
        envelope["operation"] = "reserve"

        res = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": envelope, "payload": payload},
        )
        # Signature covers 'commit' but we submitted 'reserve' → mismatch → 401
        assert res.status_code == 401, res.json()


class TestSignedCommitRelease:
    def _reserve(self, ctx) -> str:
        """Helper: create a valid reservation and return its ID."""
        now_str = datetime.now(timezone.utc).isoformat()
        payload = {
            "amount": "150.00",
            "currency": ctx["mandate_currency"],
            "merchant": "Shop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        envelope = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="reserve",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=payload,
        )
        r = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": envelope, "payload": payload},
        )
        assert r.status_code == 201, r.json()
        return r.json()["id"]

    def test_signed_commit_succeeds(self):
        ctx = _setup_signed_tree()
        reservation_id = self._reserve(ctx)

        envelope = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="commit",
            resource_id=reservation_id,
        )
        res = client.post(
            f"/reservations/{reservation_id}/commit",
            json={"envelope": envelope},
        )
        assert res.status_code == 200, res.json()
        assert res.json()["status"] == "committed"

    def test_signed_release_succeeds(self):
        ctx = _setup_signed_tree()
        reservation_id = self._reserve(ctx)

        envelope = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="release",
            resource_id=reservation_id,
        )
        res = client.post(
            f"/reservations/{reservation_id}/release",
            json={"envelope": envelope},
        )
        assert res.status_code == 200, res.json()
        assert res.json()["status"] == "released"

    def test_wrong_agent_cannot_commit(self):
        """Root agent cannot commit a reservation belonging to child agent."""
        ctx = _setup_signed_tree()
        reservation_id = self._reserve(ctx)

        envelope = build_signed_envelope(
            private_key=ctx["root_priv"],
            agent_id=ctx["root_agent_id"],
            operation="commit",
            resource_id=reservation_id,
        )
        res = client.post(
            f"/reservations/{reservation_id}/commit",
            json={"envelope": envelope},
        )
        assert res.status_code == 403, res.json()


class TestSignedPaymentRail:
    def test_signed_pay_succeeds(self):
        ctx = _setup_signed_tree()

        # Reserve
        now_str = datetime.now(timezone.utc).isoformat()
        payload = {
            "amount": "200.00",
            "currency": ctx["mandate_currency"],
            "merchant": "PayShop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        env_r = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="reserve",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=payload,
        )
        r1 = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": env_r, "payload": payload},
        )
        assert r1.status_code == 201, r1.json()
        reservation_id = r1.json()["id"]

        # Pay
        env_p = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="pay",
            resource_id=reservation_id,
        )
        r2 = client.post(
            f"/reservations/{reservation_id}/pay",
            json={"envelope": env_p},
        )
        assert r2.status_code == 201, r2.json()
        data = r2.json()
        assert data["status"] == "succeeded"
        assert data["utr_reference"].startswith("SIM")
        assert data["amount"] == "200.00"

    def test_wrong_agent_cannot_pay(self):
        """Root agent cannot pay a reservation belonging to child agent."""
        ctx = _setup_signed_tree()

        now_str = datetime.now(timezone.utc).isoformat()
        payload = {
            "amount": "100.00",
            "currency": ctx["mandate_currency"],
            "merchant": "PayShop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        env_r = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="reserve",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=payload,
        )
        r1 = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": env_r, "payload": payload},
        )
        assert r1.status_code == 201, r1.json()
        reservation_id = r1.json()["id"]

        # Try to pay with root agent
        env_p = build_signed_envelope(
            private_key=ctx["root_priv"],
            agent_id=ctx["root_agent_id"],
            operation="pay",
            resource_id=reservation_id,
        )
        r2 = client.post(
            f"/reservations/{reservation_id}/pay",
            json={"envelope": env_p},
        )
        assert r2.status_code == 403, r2.json()

    def test_replay_pay_rejected(self):
        """Replaying a pay request → 409."""
        ctx = _setup_signed_tree()

        now_str = datetime.now(timezone.utc).isoformat()
        payload = {
            "amount": "75.00",
            "currency": ctx["mandate_currency"],
            "merchant": "ReplayShop",
            "category": ctx["child_cap_category"],
            "transaction_time": now_str,
            "idempotency_key": str(uuid.uuid4()),
        }
        env_r = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="reserve",
            resource_id=str(ctx["child_cap_id"]),
            payload_dict=payload,
        )
        r1 = client.post(
            f"/capabilities/{ctx['child_cap_id']}/reserve",
            json={"envelope": env_r, "payload": payload},
        )
        assert r1.status_code == 201
        reservation_id = r1.json()["id"]

        request_id = str(uuid.uuid4())
        env_p = build_signed_envelope(
            private_key=ctx["child_priv"],
            agent_id=ctx["child_agent_id"],
            operation="pay",
            resource_id=reservation_id,
            request_id=request_id,
        )

        # First pay succeeds
        r2a = client.post(f"/reservations/{reservation_id}/pay", json={"envelope": env_p})
        assert r2a.status_code == 201

        # Replay rejected
        r2b = client.post(f"/reservations/{reservation_id}/pay", json={"envelope": env_p})
        assert r2b.status_code == 409
        assert "RequestReplayError" in r2b.json()["error"]
