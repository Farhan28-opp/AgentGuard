"""Security Lab scenarios (/demo/*) — real signatures, real ledger, real model.

The reset is scoped to demo users, so other tests' data may coexist; these
tests only look at the lab-* agents' capabilities.
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app
from app.models.agent import Agent
from app.models.user import User

client = TestClient(app)
pytestmark = pytest.mark.usefixtures("real_risk_engine")


def _lab():
    return {c["agent_identifier"]: c for c in client.get("/dashboard/capabilities-tree?limit=500").json()
            if c["agent_identifier"].startswith("lab-")}


def _fresh_lab():
    assert client.post("/demo/reset").status_code == 200
    assert client.post("/demo/initialize").status_code == 200


class TestDemoScenarios:
    def test_end_to_end_demo_scenario(self, db):
        _fresh_lab()
        lab = _lab()
        assert set(lab) == {"lab-shopping-agent", "lab-search-agent", "lab-negotiation-agent", "lab-purchase-agent"}

        resp = client.post("/demo/normal-payment")
        assert resp.status_code == 200 and resp.json()["status"] == "success"
        purchase = _lab()["lab-purchase-agent"]
        assert Decimal(purchase["committed_authority"]) == Decimal("650")
        assert Decimal(purchase["reserved_authority"]) == 0

        data = client.post("/demo/behavioural-anomaly").json()
        assert data["status"] == "contained"
        assert data["label"] == "Controlled Security Simulation"
        assert data["risk"]["level"] == "HIGH" and data["risk"]["anomaly_score"] >= 0.05
        assert data["containment"]["released_reservations"] >= 1
        assert data["containment"]["reserved_after"] == "0.00"
        assert data["containment"]["agent"]["agent_status"] == "suspended"
        assert data["follow_up_request"]["blocked"] is True

        purchase = _lab()["lab-purchase-agent"]
        assert purchase["status"] == "REVOKED"
        assert Decimal(purchase["committed_authority"]) == Decimal("650")   # committed is not "released"
        assert Decimal(purchase["reserved_authority"]) == 0

        # Future payments are rejected: the suspended agent's key is refused.
        resp = client.post("/demo/normal-payment")
        assert resp.status_code == 401
        assert resp.json()["error"] == "UnknownAgentKeyError"

    def test_tampered_request(self, db):
        _fresh_lab()
        body = client.post("/demo/tamper-request").json()
        assert body["status"] == "rejected"
        errors = {a["attempt"]: a["error"] for a in body["attempts"]}
        assert errors["payload_altered_after_signing"] == "PayloadIntegrityError"
        assert errors["forged_signature"] == "InvalidSignatureError"
        assert Decimal(_lab()["lab-purchase-agent"]["reserved_authority"]) == 0

    def test_policy_violation_is_deterministic(self, db, monkeypatch):
        """Over-limit request is rejected by the ledger; the model is never consulted."""
        _fresh_lab()
        from app.services import risk_engine as re_mod
        calls = {"n": 0}
        real = re_mod.get_risk_engine()
        orig_eval = real.evaluate

        def spy(*a, **k):
            calls["n"] += 1
            return orig_eval(*a, **k)

        monkeypatch.setattr(real, "evaluate", spy)
        body = client.post("/demo/policy-violation").json()
        assert body["status"] == "rejected" and body["error"] == "InsufficientAuthorityError"
        assert calls["n"] == 0

    def test_concurrent_race(self, db):
        _fresh_lab()
        data = client.post("/demo/concurrent-race").json()
        assert sorted(r["status"] for r in data["results"]) == ["failed", "success"]
        assert [r["error"] for r in data["results"] if r["status"] == "failed"] == ["InsufficientAuthorityError"]
        assert len(data["released_after_race"]) == 1
        assert Decimal(_lab()["lab-negotiation-agent"]["reserved_authority"]) == 0

    def test_race_after_normal_payment_still_exercises_lock(self, db):
        _fresh_lab()
        client.post("/demo/normal-payment")
        data = client.post("/demo/concurrent-race").json()
        assert sorted(r["status"] for r in data["results"]) == ["failed", "success"]

    def test_events_feed(self, db):
        _fresh_lab()
        client.post("/demo/normal-payment")
        types = [e["event_type"] for e in client.get("/events?limit=200").json()]
        for ev in ["MANDATE_CREATED", "CAPABILITY_ISSUED", "SIGNED_RESERVE_REQUEST", "AUTHORITY_RESERVED",
                   "PAYMENT_SUCCESS", "RESERVATION_COMMITTED"]:
            assert ev in types, ev

    def test_reset_is_scoped_to_demo_data(self, db):
        """A non-demo user and agent survive the demo reset."""
        import uuid
        from tests.conftest import make_agent_with_key
        keep_user = User(id=uuid.uuid4(), name="Not demo", is_demo=False)
        db.add(keep_user)
        db.commit()
        keep_agent, _ = make_agent_with_key(db, f"keep-{uuid.uuid4().hex[:6]}", "purchase")
        _fresh_lab()
        with SessionLocal() as s:
            assert s.get(User, keep_user.id) is not None
            assert s.get(Agent, keep_agent.id) is not None
            assert s.scalar(select(func.count(User.id)).where(User.is_demo.is_(True))) == 1
            # the clean demo state: standing agents only, no lab agents
            ids = {a.agent_identifier for a in s.scalars(select(Agent).where(Agent.owner_user_id.isnot(None))).all()}
        assert {"main-agent", "search-agent", "optimization-agent", "purchase-agent", "wallet-key"} <= ids
        assert "lab-purchase-agent" in ids   # re-created by _fresh_lab's initialize

    def test_demo_mode_off_blocks_demo_endpoints(self, db, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "demo_mode", False)
        assert client.post("/demo/reset").status_code == 403
