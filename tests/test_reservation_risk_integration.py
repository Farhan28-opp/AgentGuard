"""Day 5 + Day 6: Behavioural risk integration tests using signed requests.

These tests exercise the risk engine via the HTTP API (SignedReserveRequest
format) to confirm that:
  - LOW risk: reservation succeeds (201)
  - Hard deterministic violation: always rejected (422) regardless of AI
  - MEDIUM risk: blocked for review (409) — no reservation created
  - HIGH risk: subtree revoked (403) — no reservation created, DB updated
"""
import uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models.capability import CapabilityStatus
from app.models.mandate import Mandate, MandateStatus
from app.models.user import User
from app.schemas.risk import RiskLevel, RiskAction, RiskResult
from app.services import risk_engine
from app.services.capability_service import get_capability

from tests.conftest import (
    make_agent_with_key,
    create_root_capability,
    create_child_capability,
    build_signed_envelope,
)

client = TestClient(app)


class MockRiskEngine:
    """Mock risk engine to force LOW, MEDIUM, HIGH responses."""
    def __init__(self, force_level: RiskLevel, action: RiskAction):
        self.force_level = force_level
        self.action = action
        self.is_loaded = True
        self.medium_threshold = 0.05
        self.high_threshold = 0.15

    def evaluate(self, features, capability_id=None):
        return RiskResult(
            risk_level=self.force_level,
            action=self.action,
            anomaly_score=0.99 if self.action == RiskAction.CONTAIN else (
                0.10 if self.action == RiskAction.REVIEW else 0.01
            ),
            reason_codes=[],
            reasons=["Mocked reason"],
            capability_id=capability_id,
        )


def setup_signed_tree():
    """Create mandate → root → child capabilities, both agents with keypairs.

    Returns a dict of plain primitives (no ORM objects) to avoid detached
    instance errors after the session closes.
    """
    db = SessionLocal()
    try:
        user = User(id=uuid.uuid4(), name="RiskTest")
        db.add(user)
        db.commit()

        now = datetime.now(timezone.utc)
        mandate = Mandate(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Risk Mandate",
            purpose="testing",
            currency="INR",
            total_authority=Decimal("10000"),
            status=MandateStatus.ACTIVE,
            not_before=now,
            not_after=now + timedelta(days=7),
        )
        db.add(mandate)
        db.commit()

        root_agent, root_priv = make_agent_with_key(db, f"risk-root-{uuid.uuid4()}", "root")
        child_agent, child_priv = make_agent_with_key(db, f"risk-purchase-{uuid.uuid4()}", "purchase")

        root_cap = create_root_capability(db, mandate, root_agent, total_authority=Decimal("5000"))
        child_cap = create_child_capability(db, root_cap, child_agent, total_authority=Decimal("2000"))

        # Capture all values before closing session
        return {
            "child_cap_id": str(child_cap.id),
            "child_agent_id": child_agent.id,
            "child_priv": child_priv,
            "currency": mandate.currency,
            "category": child_cap.category,
        }
    finally:
        db.close()


def _post_signed_reserve(ctx, amount="100.00", currency=None, category=None, merchant="TestMerchant", idempotency_key=None):
    """Helper: build + post a signed reserve request."""
    now_str = datetime.now(timezone.utc).isoformat()
    currency = currency or ctx["currency"]
    category = category or ctx["category"]
    idempotency_key = idempotency_key or str(uuid.uuid4())

    payload = {
        "amount": amount,
        "currency": currency,
        "merchant": merchant,
        "category": category,
        "transaction_time": now_str,
        "idempotency_key": idempotency_key,
    }
    envelope = build_signed_envelope(
        private_key=ctx["child_priv"],
        agent_id=ctx["child_agent_id"],
        operation="reserve",
        resource_id=ctx["child_cap_id"],
        payload_dict=payload,
    )
    return client.post(
        f"/capabilities/{ctx['child_cap_id']}/reserve",
        json={"envelope": envelope, "payload": payload},
    )


def test_normal_transaction_succeeds():
    risk_engine.set_risk_engine(MockRiskEngine(RiskLevel.LOW, RiskAction.ALLOW))
    ctx = setup_signed_tree()

    res = _post_signed_reserve(ctx, merchant="NormalMerchant")
    assert res.status_code == 201, res.json()
    assert res.json()["status"] == "reserved"


def test_hard_policy_violation_bypasses_ai():
    """Even if AI says LOW risk, a hard violation (wrong currency) must reject."""
    risk_engine.set_risk_engine(MockRiskEngine(RiskLevel.LOW, RiskAction.ALLOW))
    ctx = setup_signed_tree()

    # Currency mismatch is a deterministic hard failure — signed payload with wrong currency
    now_str = datetime.now(timezone.utc).isoformat()
    payload = {
        "amount": "100.00",
        "currency": "XXX",  # Wrong currency
        "merchant": "NormalMerchant",
        "category": ctx["category"],
        "transaction_time": now_str,
        "idempotency_key": str(uuid.uuid4()),
    }
    envelope = build_signed_envelope(
        private_key=ctx["child_priv"],
        agent_id=ctx["child_agent_id"],
        operation="reserve",
        resource_id=ctx["child_cap_id"],
        payload_dict=payload,
    )
    res = client.post(
        f"/capabilities/{ctx['child_cap_id']}/reserve",
        json={"envelope": envelope, "payload": payload},
    )
    assert res.status_code == 422, res.json()
    assert "CurrencyMismatchError" in res.json()["error"]


def test_medium_risk_blocked_for_review():
    risk_engine.set_risk_engine(MockRiskEngine(RiskLevel.MEDIUM, RiskAction.REVIEW))
    ctx = setup_signed_tree()

    res = _post_signed_reserve(ctx, merchant="SuspiciousMerchant")
    assert res.status_code == 409, res.json()
    data = res.json()
    assert data["error"] == "RiskReviewError"
    assert data["risk_level"] == "MEDIUM"
    assert data["action"] == "REVIEW"


def test_high_risk_containment():
    risk_engine.set_risk_engine(MockRiskEngine(RiskLevel.HIGH, RiskAction.CONTAIN))
    ctx = setup_signed_tree()

    # Pre-condition: capability active
    db = SessionLocal()
    try:
        cap_before = get_capability(db, ctx["child_cap_id"])
        assert cap_before.status == CapabilityStatus.ACTIVE
    finally:
        db.close()

    idem_key = f"tx_high_{uuid.uuid4()}"
    res = _post_signed_reserve(ctx, merchant="MaliciousMerchant", idempotency_key=idem_key)

    assert res.status_code == 403, res.json()
    data = res.json()
    assert data["error"] == "HighRiskContainmentError"
    assert data["risk_level"] == "HIGH"
    assert data["action"] == "CONTAIN"

    # Post-condition: capability revoked in DB, no reservation created
    db = SessionLocal()
    try:
        cap_after = get_capability(db, ctx["child_cap_id"])
        assert cap_after.status == CapabilityStatus.REVOKED

        from sqlalchemy import select
        from app.models.reservation import Reservation
        saved = db.scalar(
            select(Reservation).where(Reservation.idempotency_key == idem_key)
        )
        assert saved is None, "HIGH risk must NOT create a reservation"
    finally:
        db.close()
