"""Final pre-submission pass: simple-payment authorization, the simple-payment
risk fix, persistent identity, budgets through the agent chain, the Merchant
Optimization Agent, Activity transaction management, the unified receipt and
page/API wiring.

Everything runs against PostgreSQL with real Ed25519 signing, the real
authority ledger and — unless a test says otherwise — the real trained
IsolationForest. Drunix enforcement is exercised against the fake bridge of
tests/test_drunix_integration.py (the same HTTP contract as drunix/bridge).
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models.agent import Agent
from app.models.agent_task import AgentTask
from app.models.audit_log import AuditLog
from app.models.capability import Capability, CapabilityStatus
from app.models.mandate import Mandate
from app.models.payment import Payment
from app.models.reservation import Reservation, ReservationStatus
from app.schemas.risk import ReasonCode, RiskAction, RiskLevel, RiskResult
from app.services import policy_service
from app.services.risk_engine import RiskEngine, get_risk_engine, set_risk_engine
from tests.test_drunix_integration import FakeBridge, _error, bridge, enforce  # noqa: F401  (fixtures)

client = TestClient(app)
ROOT = Path(__file__).resolve().parent.parent

RECHARGE = ("/product/payments/recharge", {"mobile_number": "9000000001", "operator": "Jio",
                                           "plan_amount": 299, "plan_description": "2GB/day 28 days"})
BILL = ("/product/payments/bill", {"consumer_number": "CN-0001", "provider": "BESCOM", "amount": 850})
SEND = ("/product/payments/send-money", {"recipient_upi": "friend@okbank", "amount": 500, "purpose": "Dinner"})


@pytest.fixture()
def fresh(real_risk_engine):
    assert client.post("/demo/reset").status_code == 200
    yield


def _db():
    return SessionLocal()


def _count(model, *where):
    with _db() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def _prepare(req):
    r = client.post(req[0], json=req[1])
    return r, r.json()


def _pay(req):
    r, body = _prepare(req)
    assert r.status_code == 200 and body["status"] == "AWAITING_AUTHORIZATION", body
    done = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
    return body, done


class _Stub:
    """Risk engine stand-in with a fixed decision (only where a test must
    force a specific verdict; everything else uses the real model)."""
    is_loaded = True
    medium_threshold, high_threshold = 0.02, 0.05

    def __init__(self, level, action, score):
        self.level, self.action, self.score = level, action, score

    def evaluate(self, features, capability_id=None, context=None):
        return RiskResult(risk_level=self.level, action=self.action, anomaly_score=self.score,
                          reason_codes=[ReasonCode.HIGH_TRANSACTION_VELOCITY], reasons=["forced by test"],
                          capability_id=capability_id)


# ── 1–3, 5–6 (review), 10: simple payments require explicit authorization ───

class TestSimplePaymentAuthorization:
    @pytest.mark.parametrize("req", [RECHARGE, BILL, SEND], ids=["recharge", "bill", "send-money"])
    def test_prepare_issues_nothing_then_authorize_pays(self, fresh, req):
        mandates, payments = _count(Mandate), _count(Payment)
        r, body = _prepare(req)
        assert r.status_code == 200 and body["status"] == "AWAITING_AUTHORIZATION"
        a = body["authorization"]
        # The review shows real data, and nothing was issued, held or paid yet.
        assert a["flow"] == "direct" and a["actor"]["agent"] == "wallet-key"
        assert a["policy"]["status"] == "WITHIN LIMIT" and a["risk"]["stage"] == "pre-check"
        assert a["reservation_status"] == "PENDING YOUR APPROVAL" and a["checks"]["authority"] == "ON APPROVAL"
        assert _count(Mandate) == mandates and _count(Payment) == payments
        done = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
        assert done.status_code == 200, done.json()
        d = done.json()
        assert d["status"] == "COMPLETED" and d["result"]["utr_reference"].startswith("SIM")
        assert d["result"]["signing"]["operations_verified"] == ["reserve", "pay"]
        with _db() as db:
            p = db.get(Payment, uuid.UUID(d["result"]["payment_id"]))
            res = db.get(Reservation, p.reservation_id)
            cap = db.get(Capability, res.capability_id)
            assert res.status == ReservationStatus.COMMITTED
            # 13. the payment never exceeds the authority behind it
            assert p.amount == res.amount == cap.total_authority == cap.committed_authority
            assert cap.max_delegation_depth == 0 and cap.max_fanout == 0 and cap.parent_capability_id is None

    def test_double_authorization_pays_once(self, fresh):
        body, done = _pay(BILL)
        assert done.json()["status"] == "COMPLETED"
        again = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
        assert again.status_code == 409
        assert _count(Payment, Payment.merchant == "BESCOM") == 1

    def test_cancel_before_authorization_issues_nothing(self, fresh):
        mandates = _count(Mandate)
        _, body = _prepare(SEND)
        out = client.post(f"/product/payments/requests/{body['request_id']}/cancel").json()
        assert out["status"] == "CANCELLED" and _count(Mandate) == mandates
        assert client.post(f"/product/payments/requests/{body['request_id']}/authorize").status_code == 409

    def test_expired_request_cannot_be_authorized(self, fresh):
        _, body = _prepare(RECHARGE)
        with _db() as db:
            row = db.get(AgentTask, body["request_id"])
            row.authorization = {**row.authorization,
                                 "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
            db.commit()
        r = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
        assert r.status_code == 409 and r.json()["request"]["status"] == "EXPIRED"
        assert _count(Payment, Payment.merchant == "Jio Recharge") == 0

    def test_shopping_endpoints_do_not_act_on_direct_requests(self, fresh):
        _, body = _prepare(BILL)
        rid = body["request_id"]
        assert client.post(f"/product/agent/tasks/{rid}/authorize-payment").status_code == 404
        assert client.get(f"/product/agent/tasks/{rid}").status_code == 404


# ── 4: policy ────────────────────────────────────────────────────────────────

class TestSimplePaymentPolicy:
    def test_recharge_above_per_payment_limit_rejected_before_anything_is_issued(self, fresh):
        mandates = _count(Mandate)
        r = client.post("/product/payments/recharge", json={**RECHARGE[1], "plan_amount": 3500})
        assert r.status_code == 409 and r.json()["error"] == "PolicyRejection"
        assert "per-payment limit ₹3,000.00" in r.json()["detail"]
        assert r.json()["request"]["status"] == "FAILED" and _count(Mandate) == mandates

    def test_limit_change_after_review_is_enforced_at_authorization(self, fresh):
        _, body = _prepare(BILL)
        # The policy API refuses edits while a payment awaits authorization; the
        # re-check at authorization is defence in depth for any other change path.
        assert client.put("/product/policy", json={"per_transaction_limit": 500}).status_code == 409
        with _db() as db:
            pol = policy_service.get_policy(db, policy_service.demo_user(db))
            pol.per_transaction_limit = Decimal("500")
            db.commit()
        r = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
        assert r.status_code == 409 and r.json()["error"] == "PolicyRejection"
        assert _count(Payment, Payment.merchant == "BESCOM") == 0

    def test_policy_documents_what_direct_payments_are_checked_against(self, fresh):
        p = client.get("/product/policy").json()
        assert "direct payment" in p["enforcement"]["per_transaction_limit"]


# ── 5: risk (the rejection root cause, and anomalies still blocked) ──────────

class TestSimplePaymentRisk:
    def test_reported_rejection_sequence_no_longer_false_positive(self, fresh):
        """Transfer ₹500 → bill ₹850 → recharge ₹149 used to be MEDIUM (0.0391,
        HTTP 409) only because the single-use capability made projected
        consumption 100 %. Measured against the per-payment limit it is LOW, and
        the real amount-deviation signal is still reported."""
        for req in (SEND, BILL):
            assert _pay(req)[1].json()["status"] == "COMPLETED"
        body, done = _pay(("/product/payments/recharge", {**RECHARGE[1], "plan_amount": 149}))
        d = done.json()
        assert d["status"] == "COMPLETED", d
        risk = d["result"]["risk"]
        assert risk["level"] == "LOW" and risk["stage"] == "authoritative"
        assert risk["features"]["authority_consumption_rate"] == pytest.approx(149 / 3000, abs=1e-4)
        assert any("standard deviations" in x for x in risk["reasons"])

    def test_consumption_basis_only_for_single_purpose_grants(self, fresh):
        """Agent purchases are unchanged: consumption is still measured against
        the Purchase capability itself."""
        tid = client.post("/product/agent/tasks", json={"instruction": "Buy groceries for me under ₹3,000"}).json()["task_id"]
        client.post(f"/product/agent/tasks/{tid}/cart/items", json={"product_id": "tata-salt-1kg", "quantity": 2})
        client.post(f"/product/agent/tasks/{tid}/merchant", json={"mode": "user", "merchant_id": "dailymart"})
        body = client.post(f"/product/agent/tasks/{tid}/execute").json()
        assert body["status"] == "AWAITING_AUTHORIZATION"
        assert not any("per-payment limit" in r for r in body["authorization"]["risk"]["reasons"])

    def test_genuinely_anomalous_payment_is_still_blocked(self, fresh):
        """Real model, real features: a burst of recent wallet payments at many
        merchants followed by a far larger amount at a new merchant."""
        # A normal history through the product flow itself: small, similar
        # transfers to one known recipient...
        for amount in (100, 110, 120):
            body, done = _pay(("/product/payments/send-money", {"recipient_upi": "friend@okbank", "amount": amount,
                                                                "purpose": "x"}))
            assert done.json()["status"] == "COMPLETED", done.json()
        mandates = _count(Mandate)
        # ...then, within minutes, ~25x the usual amount to a never-seen recipient.
        r = client.post("/product/payments/send-money", json={"recipient_upi": "stranger@bank", "amount": 2900,
                                                              "purpose": "urgent"})
        body = r.json()
        assert r.status_code in (403, 409), body
        assert body["request"]["status"] in ("BLOCKED", "REVIEW_REQUIRED")
        assert body["risk_level"] in ("MEDIUM", "HIGH") and body["reasons"]
        assert _count(Mandate) == mandates and _count(Payment, Payment.merchant == "UPI stranger@bank") == 0

    def test_authoritative_check_at_authorization_can_still_block(self, fresh):
        _, body = _prepare(BILL)
        real = get_risk_engine()
        set_risk_engine(_Stub(RiskLevel.MEDIUM, RiskAction.REVIEW, 0.031))
        try:
            r = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
        finally:
            set_risk_engine(real)
        assert r.status_code == 409 and r.json()["request"]["status"] == "REVIEW_REQUIRED"
        with _db() as db:
            t = db.get(AgentTask, body["request_id"])
            cap = db.get(Capability, uuid.UUID(t.refs["capability"]))
            assert cap.status == CapabilityStatus.REVOKED          # single-use authority withdrawn
        assert _count(Payment, Payment.merchant == "BESCOM") == 0

    def test_engine_still_flags_anomalies_whatever_the_consumption_basis(self):
        e = RiskEngine(medium_threshold=settings.risk_medium_threshold, high_threshold=settings.risk_high_threshold)
        f = {"transaction_velocity_1h": 8, "amount_z_score": 4.0, "new_merchant_ratio": 1.0,
             "delegation_rate": 0, "time_deviation": 1}
        for c in (0.05, 0.3, 1.0):
            assert e.evaluate({**f, "authority_consumption_rate": c}).risk_level == RiskLevel.HIGH
        r = e.evaluate({**f, "authority_consumption_rate": 0.9}, context={"consumption_basis": "your ₹3,000.00 per-payment limit"})
        assert any("per-payment limit" in x for x in r.reasons)


# ── 10: persistent identity and history ──────────────────────────────────────

class TestPersistentIdentity:
    def test_repeated_payments_use_one_user_one_wallet_and_accumulate_history(self, fresh):
        velocities = []
        for req in (BILL, SEND, RECHARGE):
            _, done = _pay(req)
            d = done.json()
            assert d["status"] == "COMPLETED"
            velocities.append(d["result"]["risk"]["features"]["transaction_velocity_1h"])
        assert velocities == [1.0, 2.0, 3.0]                 # the model sees the growing history
        with _db() as db:
            assert db.scalar(select(func.count(Agent.id)).where(Agent.agent_type == "direct")) == 1
            demo = [u for (u,) in db.execute(select(AgentTask.user_id).distinct()).all()]
            assert len(set(demo)) == 1


# ── 6–9: Drunix enforcement of simple payments ───────────────────────────────

class TestSimplePaymentsOnDrunix:
    def test_prepare_submits_nothing_authorize_enforces_in_order(self, enforce):
        r, body = _prepare(RECHARGE)
        assert body["authorization"]["drunix"]["status"] == "READY"
        assert enforce.functions() == []                      # review only: nothing issued on-chain
        done = client.post(f"/product/payments/requests/{body['request_id']}/authorize").json()
        assert done["status"] == "COMPLETED", done
        assert enforce.functions() == ["RegisterMandate", "RegisterRootCapability", "Reserve", "Commit"]
        dx = done["result"]["drunix"]
        assert dx["reserve"]["tx_id"] and dx["commit"]["tx_id"]
        assert dx["reserve"]["block_number"] < dx["commit"]["block_number"]
        receipt = client.get(f"/product/transactions/{body['request_id']}").json()
        assert receipt["drunix"]["reserve"]["tx_id"] == dx["reserve"]["tx_id"]
        assert receipt["drunix"]["commit"]["tx_id"] == dx["commit"]["tx_id"]
        assert receipt["drunix"]["register_mandate"]["tx_id"]

    def test_failed_reserve_produces_no_payment(self, enforce):
        enforce.script("Reserve", _error("CHAINCODE_REJECTED", "INSUFFICIENT_AUTHORITY"))
        _, body = _prepare(BILL)
        r = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
        assert r.status_code == 409 and r.json()["request"]["status"] == "FAILED"
        assert _count(Payment, Payment.merchant == "BESCOM") == 0
        assert "Commit" not in enforce.functions() and "Revoke" in enforce.functions()
        receipt = client.get(f"/product/transactions/{body['request_id']}").json()
        assert receipt["drunix"]["reserve"] is None and receipt["payment_id"] is None

    def test_rejected_commit_is_not_a_false_success(self, enforce):
        enforce.script("Commit", _error("CHAINCODE_REJECTED", "CAPABILITY_INACTIVE"))
        _, body = _prepare(SEND)
        r = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
        req = r.json()["request"]
        assert r.status_code == 409 and req["status"] == "PAYMENT_FAILED" and req["result"] is None
        assert _count(Payment, Payment.merchant == "UPI friend@okbank") == 0
        with _db() as db:
            t = db.get(AgentTask, body["request_id"])
            assert db.get(Reservation, uuid.UUID(t.refs["reservation"])).status == ReservationStatus.RELEASED

    def test_unreachable_commit_keeps_hold_and_retry_pays_once(self, enforce):
        enforce.script("Commit", _error("DRUNIX_UNAVAILABLE", status=503, stage="bridge"))
        _, body = _prepare(RECHARGE)
        r = client.post(f"/product/payments/requests/{body['request_id']}/authorize")
        assert r.status_code == 503 and r.json()["request"]["status"] == "AWAITING_AUTHORIZATION"
        assert _count(Payment, Payment.merchant == "Jio Recharge") == 0
        again = client.post(f"/product/payments/requests/{body['request_id']}/authorize").json()
        assert again["status"] == "COMPLETED"
        assert enforce.functions().count("Reserve") == 1 and enforce.functions().count("RegisterMandate") == 1
        assert _count(Payment, Payment.merchant == "Jio Recharge") == 1

    def test_direct_payment_reaches_the_drunix_status_and_journal(self, enforce):
        _pay(BILL)
        s = client.get("/drunix/status").json()
        assert s["mode"] == "enforce" and s["connected"] is True
        fns = [t["function"] for t in client.get("/drunix/transactions?limit=20").json()["transactions"]]
        assert {"RegisterMandate", "RegisterRootCapability", "Reserve", "Commit"} <= set(fns)
        rows = client.get("/drunix/reconciliation?limit=5").json()["rows"]
        assert rows and not any(x["state"] == "UNREACHABLE" for x in rows)


# ── 11–13: budgets, Merchant Optimization Agent, authority ──────────────────

def _task(budget):
    r = client.post("/product/agent/tasks", json={"instruction": f"Buy groceries for me under ₹{budget:,}"})
    assert r.status_code == 200, r.json()
    return r.json()


class TestBudgetsAndOptimization:
    @pytest.mark.parametrize("budget", [300, 1000, 3000, 5000, 10000])
    def test_budget_propagates_through_the_agent_chain(self, fresh, budget):
        t = _task(budget)
        tid = t["task_id"]
        assert Decimal(str(t["budget_inr"])) == budget
        expected_limit = min(Decimal(budget), Decimal("3000"))
        assert Decimal(t["policy"]["purchase_limit"]) == expected_limit
        if budget > 3000:
            n = t["budget_notice"]
            assert n["binding"] == "per_purchase_limit" and Decimal(n["requested_budget"]) == budget
            assert Decimal(n["effective_limit"]) == Decimal("3000") and "raise the per-purchase limit" in n["message"]
        else:
            assert t["budget_notice"] is None
        client.post(f"/product/agent/tasks/{tid}/cart/items", json={"product_id": "tata-salt-1kg", "quantity": 3})
        cmp = client.post(f"/product/agent/tasks/{tid}/compare").json()
        assert all(q["budget"] == f"{budget:.2f}" for q in cmp["quotes"])
        assert f"₹{budget:,.2f} budget" in cmp["recommendation"]["reason"]
        client.post(f"/product/agent/tasks/{tid}/merchant", json={"mode": "agent"})
        body = client.post(f"/product/agent/tasks/{tid}/execute").json()
        assert body["status"] == "AWAITING_AUTHORIZATION"
        purchase = next(n for n in body["authority"]["nodes"] if n["role"] == "purchase")
        assert Decimal(purchase["total"]) == expected_limit                    # delegated authority = limit
        assert Decimal(body["authorization"]["total"]) < Decimal(budget)       # the budget is a ceiling, not a target
        done = client.post(f"/product/agent/tasks/{tid}/authorize-payment").json()
        assert Decimal(done["result"]["amount"]) <= Decimal(purchase["total"])

    def test_ten_thousand_needs_a_policy_change_then_works(self, fresh):
        assert client.put("/product/policy", json={"per_transaction_limit": 10000}).status_code == 200
        t = _task(10000)
        assert t["budget_notice"] is None and Decimal(t["policy"]["purchase_limit"]) == Decimal("10000")

    def test_optimization_output_depends_on_cart_and_budget(self, fresh):
        low, high = _task(300), _task(3000)
        for t in (low, high):
            client.post(f"/product/agent/tasks/{t['task_id']}/cart/items", json={"product_id": "amul-ghee-1l", "quantity": 1})
        q_low = client.post(f"/product/agent/tasks/{low['task_id']}/compare").json()
        q_high = client.post(f"/product/agent/tasks/{high['task_id']}/compare").json()
        assert not any(q["eligible"] for q in q_low["quotes"])
        assert all("exceeds your ₹300.00 budget" in " ".join(q["problems"]) for q in q_low["quotes"] if q["available"])
        assert q_low["recommendation"]["merchant_id"] is None and q_high["recommendation"]["merchant_id"]
        client.post(f"/product/agent/tasks/{high['task_id']}/cart/items", json={"product_id": "amul-ghee-1l", "quantity": 3})
        q_more = client.post(f"/product/agent/tasks/{high['task_id']}/compare").json()
        assert Decimal(q_more["recommendation"]["budget_remaining"]) < Decimal(q_high["recommendation"]["budget_remaining"])

    def test_optimization_agent_holds_no_spending_authority(self, fresh):
        p = client.get("/product/policy").json()
        opt = next(a for a in p["agents"] if a["role"] == "Merchant Optimization Agent")
        assert opt["standing_authority"] == "0.00" and opt["agent_identifier"] == "optimization-agent"
        assert p["standing"]["main_unallocated"] == "10000.00"

    def test_legacy_three_hundred_buffer_is_handed_back(self, fresh, monkeypatch):
        monkeypatch.setattr(policy_service, "OPTIMIZATION_AUTHORITY", Decimal("300"))
        assert client.post("/demo/reset").status_code == 200      # issues the old ₹300 shape
        monkeypatch.setattr(policy_service, "OPTIMIZATION_AUTHORITY", Decimal("0"))
        p = client.get("/product/policy").json()                  # ensure_standing_authority runs
        assert p["standing"]["main_unallocated"] == "10000.00"
        opt = next(a for a in p["agents"] if a["role"] == "Merchant Optimization Agent")
        assert opt["standing_authority"] == "0.00"
        assert _count(AuditLog, AuditLog.event_type == "AUTHORITY_RETURNED") >= 1

    def test_catalog_supports_meaningful_budgets(self, fresh):
        products = client.get("/product/catalog?category=groceries").json()["products"]
        assert len(products) >= 45
        assert sum(Decimal(p["from_price"]) for p in products if p["from_price"]) > Decimal("10000")


# ── 14–17: Activity transaction management and unified receipt ─────────────

class TestActivity:
    def _shop(self):
        t = _task(3000)
        client.post(f"/product/agent/tasks/{t['task_id']}/cart/items", json={"product_id": "tata-salt-1kg", "quantity": 2})
        client.post(f"/product/agent/tasks/{t['task_id']}/merchant", json={"mode": "user", "merchant_id": "dailymart"})
        client.post(f"/product/agent/tasks/{t['task_id']}/execute")
        return client.post(f"/product/agent/tasks/{t['task_id']}/authorize-payment").json()

    def test_transactions_list_both_kinds_and_detail_is_unified(self, fresh):
        shop = self._shop()
        body, _ = _pay(BILL)
        rows = client.get("/product/transactions").json()["transactions"]
        kinds = {r["id"]: r["kind"] for r in rows}
        assert kinds[shop["task_id"]] == "agent" and kinds[body["request_id"]] == "direct"
        for tid in (shop["task_id"], body["request_id"]):
            d = client.get(f"/product/transactions/{tid}").json()
            for k in ("type_label", "merchant", "amount", "timestamp", "payment_id", "utr_reference", "rail",
                      "actor", "signature", "risk", "policy", "capability_id", "drunix", "status_label"):
                assert k in d, k
            assert d["payment_id"] and d["utr_reference"] and d["status"] == "COMPLETED"
            assert d["drunix"]["mode"] == "off" and d["drunix"]["reserve"] is None     # never fabricated
        assert client.get(f"/transactions/{body['request_id']}").status_code == 200

    def test_repeat_creates_new_request_and_leaves_original_unchanged(self, fresh):
        shop = self._shop()
        before = client.get(f"/product/transactions/{shop['task_id']}").json()
        rep = client.post(f"/product/transactions/{shop['task_id']}/repeat").json()
        assert rep["kind"] == "agent" and rep["task_id"] != shop["task_id"] and rep["copied_lines"] == 1
        new = client.get(f"/product/agent/tasks/{rep['task_id']}").json()
        assert new["status"] == "CREATED" and new["cart"]["items"][0]["product_id"] == "tata-salt-1kg"
        after = client.get(f"/product/transactions/{shop['task_id']}").json()
        assert {k: after[k] for k in ("status", "payment_id", "amount")} == {k: before[k] for k in ("status", "payment_id", "amount")}

        body, _ = _pay(SEND)
        rep2 = client.post(f"/product/transactions/{body['request_id']}/repeat").json()
        assert rep2["kind"] == "direct" and rep2["tab"] == "send" and rep2["prefill"]["recipient_upi"] == "friend@okbank"
        r2, again = _prepare(("/product/payments/send-money", {**rep2["prefill"], "amount": 450}))
        assert again["request_id"] != body["request_id"]
        assert client.get(f"/product/transactions/{body['request_id']}").json()["amount"] == "500.00"

    def test_edit_and_retry_offered_for_failed(self, fresh):
        r = client.post("/product/payments/recharge", json={**RECHARGE[1], "plan_amount": 3500})
        rid = r.json()["request"]["request_id"]
        row = next(x for x in client.get("/product/transactions").json()["transactions"] if x["id"] == rid)
        assert row["status"] == "FAILED" and row["can_retry"] and row["can_repeat"]
        assert client.get(f"/product/transactions/{rid}").json()["policy"] == "BLOCKED"

    def test_hide_is_presentation_only(self, fresh):
        body, _ = _pay(BILL)
        rid = body["request_id"]
        payments, audits = _count(Payment), _count(AuditLog)
        assert client.post(f"/product/transactions/{rid}/hide").json()["hidden"] is True
        assert rid not in [r["id"] for r in client.get("/product/transactions").json()["transactions"]]
        assert rid in [r["id"] for r in client.get("/product/transactions?include_hidden=true").json()["transactions"]]
        assert _count(Payment) == payments and _count(AuditLog) == audits + 1      # + the ACTIVITY_HIDDEN event
        assert client.get(f"/product/transactions/{rid}").json()["payment_id"]    # receipt still available
        assert client.post(f"/product/transactions/{rid}/unhide").json()["hidden"] is False


# ── 18–20: pages, wiring and schemas ─────────────────────────────────────────

class TestWiring:
    def test_every_script_and_template_reference_resolves(self):
        for tpl in (ROOT / "templates").glob("*.html"):
            text = tpl.read_text()
            for src in re.findall(r'<script src="/static/([^"]+)"', text):
                assert (ROOT / "static" / src).exists(), (tpl.name, src)
            for ref in re.findall(r'{%\s*(?:extends|include|from|import)\s+"([^"]+)"', text):
                assert (ROOT / "templates" / ref).exists(), (tpl.name, ref)

    def test_every_fetch_url_maps_to_a_backend_route(self):
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", p) + "$") for p in routes]
        seen = set()
        for js in (ROOT / "static").glob("*.js"):
            for url in re.findall(r"(?:apiCall|postJSON)\(\s*[`'\"](/[^`'\"?]*)", js.read_text()):
                url = re.sub(r"\$\{[^}]+\}", "X", url)
                if url.endswith("/"):
                    url += "X"
                seen.add(url)
                # a template segment (X) may be a path parameter or one of several fixed routes
                dynamic = re.compile("^" + re.escape(url).replace("X", "[^/]+") + "$")
                assert any(p.match(url) for p in patterns) or any(dynamic.match(r) for r in routes), (js.name, url)
        assert "/product/transactions" in seen and "/drunix/status" in seen

    @pytest.mark.parametrize("path", ["/", "/payments", "/agent", "/activity", "/drunix", "/security"])
    def test_pages_render(self, path):
        assert client.get(path).status_code == 200

    def test_security_page_fields_exist_in_backend_response(self, fresh):
        d = client.get("/product/security/summary").json()
        js = (ROOT / "static" / "app.js").read_text()
        for key in re.findall(r"\bd\.(\w+)", js.split("function renderSummary")[0]):
            if key in ("summary", "policy", "orders", "crypto", "risk", "capabilities", "reservations", "agents",
                       "payments", "events"):
                assert key in d, key
        for key in ("active_agents", "active_capabilities", "active_reservations", "reserved_authority",
                    "committed_authority", "containments", "revoked_capabilities"):
            assert key in d["summary"], key
        for key in ("overall_authority", "per_transaction_limit", "allowed_categories", "allowed_merchants",
                    "enforcement", "standing", "agents"):
            assert key in d["policy"], key
        for key in ("algorithm", "verified_signed_requests_total", "rejected_signed_requests",
                    "grant_signatures_valid", "grant_signatures_checked"):
            assert key in d["crypto"], key

    def test_no_realistic_personal_defaults_in_payment_forms(self):
        html = (ROOT / "templates" / "payments.html").read_text()
        for value in ("9876543210", "1234567890", "john@oksbi"):
            assert value not in html
        assert 'placeholder="Enter mobile number"' in html and 'placeholder="name@bank"' in html

    def test_ui_copy_makes_no_unimplemented_claims(self):
        text = " ".join(p.read_text() for p in list((ROOT / "templates").glob("*.html")) + list((ROOT / "static").glob("*.js")))
        for claim in ("hash-chain", "hash chained", "tamper-evident", "tamper-proof"):
            assert claim not in text.lower(), claim
