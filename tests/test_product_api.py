"""Consumer product: marketplace, cart, merchant choice, agent policy, the
authorization state machine, orders/receipts and containment — against
PostgreSQL with the real ledger, real Ed25519 signing and the real
IsolationForest.
"""
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal, engine
from app.main import app
from app.models.agent import Agent
from app.models.agent_policy import AgentPolicy
from app.models.capability import Capability, CapabilityStatus
from app.models.commerce import Listing, Order
from app.models.nonce import RequestNonce
from app.models.payment import Payment
from app.models.reservation import Reservation, ReservationStatus

client = TestClient(app)
pytestmark = pytest.mark.usefixtures("real_risk_engine", "fresh_demo")
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def fresh_demo():
    assert client.post("/demo/reset").status_code == 200
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

BASKET = [("amul-toned-milk-1l", 4), ("aashirvaad-atta-5kg", 1), ("tata-salt-1kg", 2),
          ("fortune-sunflower-oil-1l", 1), ("potato-1kg", 2)]


def _task(instruction="Buy groceries for me under ₹3,000"):
    r = client.post("/product/agent/tasks", json={"instruction": instruction})
    assert r.status_code == 200, r.json()
    return r.json()["task_id"]


def _add(tid, items=BASKET):
    body = None
    for pid, q in items:
        r = client.post(f"/product/agent/tasks/{tid}/cart/items", json={"product_id": pid, "quantity": q})
        assert r.status_code == 200, r.json()
        body = r.json()
    return body


def _choose(tid, merchant=None):
    body = {"mode": "user", "merchant_id": merchant} if merchant else {"mode": "agent"}
    r = client.post(f"/product/agent/tasks/{tid}/merchant", json=body)
    assert r.status_code == 200, r.json()
    return r.json()


def _prepare(items=BASKET, merchant="dailymart"):
    tid = _task()
    _add(tid, items)
    _choose(tid, merchant)
    r = client.post(f"/product/agent/tasks/{tid}/execute")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["status"] == "AWAITING_AUTHORIZATION", body
    return tid, body


def _db():
    return SessionLocal()


def _res(body):
    with _db() as db:
        return db.get(Reservation, uuid.UUID(body["authorization"]["reservation_id"]))


def _payments(reservation_id):
    with _db() as db:
        return db.scalar(select(func.count(Payment.id)).where(Payment.reservation_id == reservation_id))


def _node(body, role):
    return next(n for n in body["authority"]["nodes"] if n["role"] == role)


def _policy_row():
    with _db() as db:
        return db.scalar(select(AgentPolicy))


def _assert_ledger_conserved():
    with _db() as db:
        for cap in db.scalars(select(Capability)).all():
            pool = cap.unallocated_authority + cap.reserved_authority + cap.committed_authority
            assert pool <= cap.total_authority
            children = db.scalar(select(func.coalesce(func.sum(Capability.total_authority), 0))
                                 .where(Capability.parent_capability_id == cap.id))
            assert cap.total_authority - pool == Decimal(children), cap.id
            for status, field in [(ReservationStatus.RESERVED, cap.reserved_authority),
                                  (ReservationStatus.COMMITTED, cap.committed_authority)]:
                s = db.scalar(select(func.coalesce(func.sum(Reservation.amount), 0)).where(
                    Reservation.capability_id == cap.id, Reservation.status == status))
                assert Decimal(s) == field, (cap.id, status)


# ── infrastructure ───────────────────────────────────────────────────────────

class TestInfrastructure:
    def test_postgresql_is_really_used(self):
        assert engine.dialect.name == "postgresql"
        h = client.get("/health")
        assert h.status_code == 200
        body = h.json()
        assert body["status"] == "ok" and body["database"] == "ok"
        assert body["database_details"]["dialect"] == "postgresql"
        assert body["risk_model_loaded"] is True

    def test_database_url_is_required_and_postgres_only(self):
        from app.config import normalized_database_url
        with pytest.raises(RuntimeError):
            normalized_database_url("")
        with pytest.raises(RuntimeError):
            normalized_database_url("sqlite:///x.db")
        assert normalized_database_url("postgres://u:p@h:5432/d") == "postgresql+psycopg2://u:p@h:5432/d"
        assert normalized_database_url("postgresql://u:p@h/d") == "postgresql+psycopg2://u:p@h/d"

    @pytest.mark.parametrize("path", ["/", "/payments", "/agent", "/activity", "/security", "/docs"])
    def test_pages_render(self, path):
        r = client.get(path)
        assert r.status_code == 200
        assert "trust completely" not in r.text
        assert not re.search(r"(?<!near-)real-time", r.text, re.IGNORECASE)

    def test_no_machine_specific_urls_or_paths_in_shipped_code(self):
        bad = re.compile(r"localhost|127\.0\.0\.1|:800[01]\b|/Users/")
        for folder in ("static", "templates", "app"):
            for f in (ROOT / folder).rglob("*"):
                if f.suffix in {".js", ".html", ".css", ".py"}:
                    assert not bad.search(f.read_text()), f

    def test_home_is_consumer_page_not_security_center(self):
        html = client.get("/").text
        assert "Security Center" not in html.split("<nav")[0]
        for label in ["Recharge", "Bills", "Send Money", "Shopping", "AI Agent"]:
            assert label in html


# ── marketplace & cart ───────────────────────────────────────────────────────

class TestCatalogAndCart:
    def test_catalog_is_backend_driven(self):
        d = client.get("/product/catalog").json()
        assert len(d["products"]) >= 30
        assert {m["id"] for m in d["merchants"]} == {"quickkart", "freshbasket", "dailymart"}
        assert all(m["simulated"] for m in d["merchants"])
        p = next(p for p in d["products"] if p["product_id"] == "amul-toned-milk-1l")
        assert {"merchant_id", "price", "stock"} <= set(p["offers"][0])

    def test_search_and_filter(self):
        milk = client.get("/product/catalog?q=milk").json()["products"]
        assert milk and all("milk" in (p["name"] + p["subcategory"]).lower() for p in milk)
        hh = client.get("/product/catalog?category=household").json()["products"]
        assert hh and {p["category"] for p in hh} == {"household"}

    def test_task_creates_cart_and_search_agent_suggests(self):
        r = client.post("/product/agent/tasks", json={"instruction": "Buy groceries for me under ₹3,000"})
        body = r.json()
        assert body["status"] == "CREATED" and body["cart"]["items"] == []
        assert body["suggestions"] and all(s["category"] == "groceries" for s in body["suggestions"])
        assert any(s["key"] == "search" for s in body["steps"])

    def test_cart_add_change_remove_clear(self):
        tid = _task()
        body = _add(tid, [("amul-toned-milk-1l", 2), ("tata-salt-1kg", 1)])
        assert {i["product_id"]: i["quantity"] for i in body["cart"]["items"]} == {"amul-toned-milk-1l": 2, "tata-salt-1kg": 1}
        r = client.post(f"/product/agent/tasks/{tid}/cart/items",
                        json={"product_id": "amul-toned-milk-1l", "quantity": 3, "mode": "add"})
        assert next(i for i in r.json()["cart"]["items"] if i["product_id"] == "amul-toned-milk-1l")["quantity"] == 5
        r = client.post(f"/product/agent/tasks/{tid}/cart/items", json={"product_id": "tata-salt-1kg", "quantity": 4})
        assert next(i for i in r.json()["cart"]["items"] if i["product_id"] == "tata-salt-1kg")["quantity"] == 4
        r = client.delete(f"/product/agent/tasks/{tid}/cart/items/tata-salt-1kg")
        assert [i["product_id"] for i in r.json()["cart"]["items"]] == ["amul-toned-milk-1l"]
        r = client.delete(f"/product/agent/tasks/{tid}/cart/items")
        assert r.json()["cart"]["items"] == []

    def test_cart_totals_are_backend_computed(self):
        tid = _task()
        body = _add(tid, [("amul-toned-milk-1l", 3)])
        with _db() as db:
            price = db.scalar(select(Listing.price).where(Listing.merchant_id == "dailymart",
                                                          Listing.product_id == "amul-toned-milk-1l"))
        _choose(tid, "dailymart")
        q = client.get(f"/product/agent/tasks/{tid}").json()["cart"]["selected_quote"]
        assert Decimal(q["subtotal"]) == price * 3
        assert Decimal(q["total"]) == Decimal(q["subtotal"]) + Decimal(q["delivery_fee"])

    def test_invalid_product_and_quantity_rejected(self):
        tid = _task()
        assert client.post(f"/product/agent/tasks/{tid}/cart/items",
                           json={"product_id": "nope", "quantity": 1}).status_code == 422
        assert client.post(f"/product/agent/tasks/{tid}/cart/items",
                           json={"product_id": "tata-salt-1kg", "quantity": 99}).status_code == 422

    def test_cart_locked_after_checkout(self):
        tid, _ = _prepare()
        r = client.post(f"/product/agent/tasks/{tid}/cart/items", json={"product_id": "tata-salt-1kg", "quantity": 9})
        assert r.status_code == 409


# ── merchant comparison & selection ─────────────────────────────────────────

class TestMerchants:
    def test_compare_prices_every_merchant(self):
        tid = _task()
        _add(tid)
        d = client.post(f"/product/agent/tasks/{tid}/compare").json()
        assert len(d["quotes"]) == 3
        for q in d["quotes"]:
            assert {"subtotal", "delivery_fee", "total", "delivery_minutes", "eligible", "problems"} <= set(q)
        assert d["recommendation"]["merchant_id"] in {q["merchant_id"] for q in d["quotes"] if q["eligible"]}
        assert "selected because" in d["recommendation"]["reason"]

    def test_manual_choice_becomes_transaction_merchant(self):
        tid, body = _prepare(merchant="freshbasket")
        assert body["authorization"]["merchant_id"] == "freshbasket"
        assert _res(body).merchant == "freshbasket"

    def test_agent_decision_follows_rule_and_explains(self):
        from app.services.commerce_service import agent_decide
        tid = _task()
        _add(tid)
        quotes = client.post(f"/product/agent/tasks/{tid}/compare").json()["quotes"]
        body = _choose(tid)
        assert body["cart"]["selection_mode"] == "agent"
        assert body["cart"]["selected_merchant"] == agent_decide(quotes)["merchant_id"]
        assert body["cart"]["selection_reason"]

    def test_agent_rule_prefers_faster_within_tolerance(self):
        from app.services.commerce_service import agent_decide
        q = lambda m, t, mins: {"merchant_id": m, "merchant": m, "total": t, "delivery_minutes": mins, "eligible": True}
        d = agent_decide([q("slow", "1000.00", 55), q("fast", "1020.00", 20)])
        assert d["merchant_id"] == "fast" and "faster" in d["reason"]
        d = agent_decide([q("slow", "1000.00", 55), q("fast", "1200.00", 20)])
        assert d["merchant_id"] == "slow" and "lowest total" in d["reason"]

    def test_ineligible_merchant_cannot_be_chosen(self):
        tid = _task()
        _add(tid, [("apple-shimla-1kg", 1)])  # DailyMart doesn't carry apples
        r = client.post(f"/product/agent/tasks/{tid}/merchant", json={"mode": "user", "merchant_id": "dailymart"})
        assert r.status_code == 409 and "Not sold here" in r.json()["detail"]

    def test_checkout_requires_merchant_choice(self):
        tid = _task()
        _add(tid)
        r = client.post(f"/product/agent/tasks/{tid}/execute")
        assert r.status_code == 409 and r.json()["task"]["status"] == "CREATED"


# ── agent policy ─────────────────────────────────────────────────────────────

class TestPolicy:
    def test_policy_defaults_and_enforcement_map(self):
        p = client.get("/product/policy").json()
        assert p["overall_authority"] == "10000.00" and p["per_transaction_limit"] == "3000.00"
        assert p["approval_mode"] == "always"
        assert "ledger" in p["enforcement"]["overall_authority"]
        assert "ledger" in p["enforcement"]["allowed_merchants"]

    def test_disallowed_category_blocked(self):
        r = client.post("/product/agent/tasks", json={"instruction": "Buy electronics under ₹2,000"})
        assert r.status_code == 403
        tid = _task()
        _add(tid, [("usb-c-charger-20w", 1)])
        d = client.post(f"/product/agent/tasks/{tid}/compare").json()
        assert not any(q["eligible"] for q in d["quotes"])

    def test_per_transaction_limit_caps_purchase_authority(self):
        client.put("/product/policy", json={"per_transaction_limit": 1000})
        tid, body = _prepare(items=[("tata-salt-1kg", 2)])
        assert _node(body, "purchase")["total"] == "1000.00"
        tid2 = _task()
        _add(tid2, [("india-gate-basmati-5kg", 2)])
        r = client.post(f"/product/agent/tasks/{tid2}/merchant", json={"mode": "user", "merchant_id": "dailymart"})
        assert r.status_code == 409 and "exceeds" in r.json()["detail"]

    def test_merchant_allowlist_is_ledger_enforced(self):
        client.put("/product/policy", json={"allowed_merchants": ["freshbasket"]})
        tid, body = _prepare(items=[("tata-salt-1kg", 1)], merchant="freshbasket")
        cap_id = body["authorization"]["capability_id"]
        with _db() as db:
            cap = db.get(Capability, uuid.UUID(cap_id))
            assert cap.merchant_allowlist == ["freshbasket"]
            agent = db.get(Agent, cap.issued_to_agent_id)
            from app.exceptions import MerchantDeniedError
            from app.services import signed_ops
            with pytest.raises(MerchantDeniedError):
                signed_ops.signed_reserve(db, agent=agent, capability=cap, amount=Decimal("10"),
                                          merchant="quickkart", category=cap.category)
            db.rollback()

    def test_overall_authority_reissue(self):
        p = client.put("/product/policy", json={"overall_authority": 6000}).json()
        assert p["standing"]["main_total"] == "6000.00"
        assert p["overall_authority"] == "6000.00"
        _assert_ledger_conserved()

    def test_policy_change_blocked_while_payment_pending(self):
        _prepare()
        assert client.put("/product/policy", json={"approval_mode": "autonomous"}).status_code == 409

    def test_invalid_policy_rejected(self):
        assert client.put("/product/policy", json={"per_transaction_limit": 50000}).status_code == 422
        assert client.put("/product/policy", json={"allowed_merchants": ["amazon"]}).status_code == 422

    def test_autonomous_mode_settles_without_prompt(self):
        client.put("/product/policy", json={"approval_mode": "autonomous"})
        tid = _task()
        _add(tid, [("tata-salt-1kg", 2)])
        _choose(tid, "dailymart")
        body = client.post(f"/product/agent/tasks/{tid}/execute").json()
        assert body["status"] == "COMPLETED" and body["order"]["order_number"].startswith("AG-")
        assert body["result"]["approval"].startswith("policy:")

    def test_threshold_mode(self):
        client.put("/product/policy", json={"approval_mode": "above_threshold", "approval_threshold": 500})
        tid = _task()
        _add(tid, [("tata-salt-1kg", 1)])
        _choose(tid, "dailymart")
        assert client.post(f"/product/agent/tasks/{tid}/execute").json()["status"] == "COMPLETED"
        tid2, body = _prepare(items=[("aashirvaad-atta-5kg", 2)])
        assert body["status"] == "AWAITING_AUTHORIZATION"

    def test_budget_enforced(self):
        tid = client.post("/product/agent/tasks", json={"instruction": "Buy groceries under ₹500"}).json()["task_id"]
        _add(tid, [("india-gate-basmati-5kg", 1)])
        r = client.post(f"/product/agent/tasks/{tid}/merchant", json={"mode": "agent"})
        assert r.status_code == 409


# ── preparation, signing, risk, reservation ──────────────────────────────────

class TestPreparation:
    def test_review_screen_data(self):
        tid, body = _prepare()
        a = body["authorization"]
        assert a["checks"] == {"identity": "VERIFIED", "signature": "VERIFIED", "policy": "WITHIN LIMIT",
                               "risk": "ALLOW", "reservation": "RESERVED"}
        assert Decimal(a["total"]) == Decimal(a["subtotal"]) + Decimal(a["delivery_fee"])
        assert Decimal(a["total"]) == _res(body).amount
        assert a["lines"] and all("quantity" in l for l in a["lines"])
        assert Decimal(a["remaining_after_payment"]) == Decimal(a["agent_authority"]) - Decimal(a["total"])
        assert a["risk"]["engine"] == "IsolationForest"
        assert _payments(_res(body).id) == 0                          # nothing paid yet

    def test_authority_moves_down_the_hierarchy(self):
        tid, body = _prepare()
        main, search, nego, purchase = (_node(body, r) for r in ("main", "search", "negotiation", "purchase"))
        assert main["total"] == "10000.00"
        # Search and Merchant Optimization agents look and compare; neither can spend.
        assert search["total"] == "0.00" and nego["total"] == "0.00"
        assert purchase["total"] == "3000.00"
        assert Decimal(main["delegated"]) == Decimal("3000.00")
        assert Decimal(purchase["reserved"]) == Decimal(body["authorization"]["total"])
        assert body["authority"]["conservation_ok"]
        _assert_ledger_conserved()

    def test_reserve_signed_by_purchase_agent(self):
        tid, body = _prepare()
        with _db() as db:
            ops = [n.operation for n in db.scalars(select(RequestNonce).where(
                RequestNonce.agent_id == uuid.UUID(_node(body, "purchase")["agent_id"]))).all()]
        assert ops == ["reserve"]

    def test_real_model_invoked(self, monkeypatch):
        from app.services import risk_engine as re_mod
        calls = {"n": 0}
        engine_ = re_mod.get_risk_engine()
        orig = engine_.evaluate

        def spy(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)

        monkeypatch.setattr(engine_, "evaluate", spy)
        _prepare()
        assert calls["n"] == 1


# ── authorization, payment, commit, order, receipt ───────────────────────────

class TestAuthorization:
    def test_end_to_end_shopping(self):
        """User → task → products → cart → merchant → agents → signed request →
        authority → IsolationForest → reserve → authorize → pay → commit →
        order → receipt → activity."""
        tid = _task()
        _add(tid)
        client.post(f"/product/agent/tasks/{tid}/compare")
        _choose(tid)                                                   # let the agent decide
        body = client.post(f"/product/agent/tasks/{tid}/execute").json()
        assert body["status"] == "AWAITING_AUTHORIZATION"
        total = Decimal(body["authorization"]["total"])
        done = client.post(f"/product/agent/tasks/{tid}/authorize-payment").json()
        assert done["status"] == "COMPLETED"
        order = done["order"]
        assert Decimal(order["total"]) == total and order["status"] == "CONFIRMED"
        res = _res(body)
        assert res.status == ReservationStatus.COMMITTED and _payments(res.id) == 1
        # unused authority handed back to the Main Agent
        r = done["result"]
        assert Decimal(r["authority_consumed"]) == total
        assert Decimal(r["authority_returned"]) == Decimal("3000.00") - total
        assert _node(done, "purchase")["status"] == "EXHAUSTED"
        assert Decimal(_node(done, "main")["unallocated"]) == Decimal("10000") - total
        # receipt
        rec = client.get(f"/product/orders/{order['order_number']}").json()
        assert rec["utr_reference"].startswith("SIM") and rec["rail"] == "SIMULATED"
        assert rec["authority"]["consumed"] == f"{total:.2f}"
        assert client.get(f"/orders/{order['order_number']}").status_code == 200
        # stock decremented
        # activity
        raw = [e["raw_event"] for e in client.get("/product/activity?limit=200").json()]
        for ev in ["AGENT_CREATED", "CAPABILITY_DELEGATED", "SIGNED_RESERVE_REQUEST", "RISK_EVALUATED",
                   "AUTHORITY_RESERVED", "PAYMENT_AUTHORIZED", "RESERVATION_COMMITTED", "ORDER_CREATED",
                   "AUTHORITY_RETURNED", "MERCHANT_COMPARISON", "MERCHANT_SELECTED"]:
            assert ev in raw, ev
        with _db() as db:
            ops = sorted(n.operation for n in db.scalars(select(RequestNonce)).all())
        assert {"reserve", "pay", "attenuate"} <= set(ops)
        _assert_ledger_conserved()

    def test_stock_is_decremented(self):
        with _db() as db:
            before = db.scalar(select(Listing.stock).where(Listing.merchant_id == "dailymart",
                                                           Listing.product_id == "tata-salt-1kg"))
        tid, _ = _prepare(items=[("tata-salt-1kg", 3)])
        client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        with _db() as db:
            after = db.scalar(select(Listing.stock).where(Listing.merchant_id == "dailymart",
                                                          Listing.product_id == "tata-salt-1kg"))
        assert after == before - 3

    def test_double_authorization_rejected(self):
        tid, body = _prepare()
        assert client.post(f"/product/agent/tasks/{tid}/authorize-payment").status_code == 200
        r = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert r.status_code == 409 and r.json()["error"] == "InvalidTaskState"
        assert _payments(_res(body).id) == 1
        with _db() as db:
            assert db.scalar(select(func.count(Order.id)).where(Order.task_id == tid)) == 1

    def test_concurrent_double_authorization_pays_once(self):
        tid, body = _prepare()
        codes, barrier = [], threading.Barrier(2)

        def go():
            barrier.wait()
            codes.append(client.post(f"/product/agent/tasks/{tid}/authorize-payment").status_code)

        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert sorted(codes) == [200, 409]
        assert _payments(_res(body).id) == 1

    def test_expired_authorization_rejected(self):
        tid, body = _prepare()
        with _db() as db:
            db.get(Reservation, _res(body).id).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        r = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert r.status_code == 409 and r.json()["task"]["status"] == "EXPIRED"
        assert _res(body).status == ReservationStatus.RELEASED and _payments(_res(body).id) == 0
        assert _node(r.json()["task"], "purchase")["reserved"] == "0.00"
        _assert_ledger_conserved()

    def test_sweeper_released_hold_reports_expired(self):
        from app.services import reservation_service
        tid, body = _prepare()
        with _db() as db:
            db.get(Reservation, _res(body).id).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
            assert reservation_service.release_expired_reservations(db) >= 1
            db.commit()
        r = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert r.status_code == 409 and r.json()["task"]["status"] == "EXPIRED"

    def test_revoked_capability_rejected(self):
        from app.security.keys import load_dev_private_key
        from tests.conftest import build_signed_envelope
        tid, body = _prepare()
        main, purchase = _node(body, "main"), _node(body, "purchase")
        env = build_signed_envelope(private_key=load_dev_private_key(main["agent_identifier"]),
                                    agent_id=uuid.UUID(main["agent_id"]), operation="revoke",
                                    resource_id=purchase["capability_id"])
        rv = client.post(f"/capabilities/{purchase['capability_id']}/revoke", json={"envelope": env})
        assert rv.status_code == 200 and rv.json()["released_reservations"] == 1
        r = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert r.status_code == 409
        assert _payments(_res(body).id) == 0

    def test_tampered_pay_rejected_and_hold_kept(self, monkeypatch):
        from app.services import signed_ops
        tid, body = _prepare()
        real = signed_ops.sign_and_encode
        monkeypatch.setattr(signed_ops, "sign_and_encode", lambda k, d: real(k, d + b"tampered"))
        r = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert r.status_code == 401 and r.json()["error"] == "InvalidSignatureError"
        assert r.json()["task"]["authorization"]["checks"]["signature"] == "FAILED"
        assert _res(body).status == ReservationStatus.RESERVED and _payments(_res(body).id) == 0
        monkeypatch.setattr(signed_ops, "sign_and_encode", real)
        assert client.post(f"/product/agent/tasks/{tid}/authorize-payment").json()["status"] == "COMPLETED"

    def test_tampered_reserve_payload_rejected(self):
        from app.exceptions import PayloadIntegrityError
        from app.services import signed_ops
        tid, body = _prepare()
        with _db() as db:
            cap = db.get(Capability, uuid.UUID(body["authorization"]["capability_id"]))
            agent = db.get(Agent, cap.issued_to_agent_id)
            with pytest.raises(PayloadIntegrityError):
                signed_ops.signed_reserve(db, agent=agent, capability=cap, amount=Decimal("50"),
                                          merchant="dailymart", category=cap.category,
                                          tamper=lambda env, p: p.update(amount="1.00"))
            db.rollback()

    def test_payment_failure_releases_hold_no_order(self, monkeypatch):
        from app.services import payment_service
        tid, body = _prepare()
        monkeypatch.setattr(payment_service, "execute_payment",
                            lambda db, rid: (_ for _ in ()).throw(RuntimeError("rail down")))
        r = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert r.status_code == 502 and r.json()["task"]["status"] == "PAYMENT_FAILED"
        assert r.json()["task"]["order"] is None
        assert _res(body).status == ReservationStatus.RELEASED and _payments(_res(body).id) == 0
        _assert_ledger_conserved()

    def test_out_of_stock_at_settlement_rolls_back_payment(self):
        tid, body = _prepare(items=[("tata-salt-1kg", 2)])
        with _db() as db:
            l = db.scalar(select(Listing).where(Listing.merchant_id == "dailymart",
                                                Listing.product_id == "tata-salt-1kg"))
            l.stock = 1
            db.commit()
        r = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert r.status_code == 502 and r.json()["task"]["status"] == "PAYMENT_FAILED"
        assert _payments(_res(body).id) == 0 and _res(body).status == ReservationStatus.RELEASED


class TestCancel:
    def test_cancel_releases_and_returns_authority(self):
        tid, body = _prepare()
        out = client.post(f"/product/agent/tasks/{tid}/cancel").json()
        assert out["status"] == "CANCELLED"
        assert _res(body).status == ReservationStatus.RELEASED and _payments(_res(body).id) == 0
        p = _node(out, "purchase")
        assert p["total"] == "0.00" and p["status"] == "EXHAUSTED"       # all authority handed back
        assert _node(out, "main")["unallocated"] == "10000.00"            # the full overall authority is back
        assert client.post(f"/product/agent/tasks/{tid}/authorize-payment").status_code == 409
        assert client.post(f"/product/agent/tasks/{tid}/cancel").status_code == 409
        _assert_ledger_conserved()


# ── containment ──────────────────────────────────────────────────────────────

class TestContainment:
    def test_containment_scenario(self):
        """Valid agent → valid authority → anomalous behaviour → HIGH → CONTAIN →
        subtree revocation → reservation release → next payment rejected."""
        tid, body = _prepare(items=[("tata-salt-1kg", 2), ("amul-toned-milk-1l", 2)])
        out = client.post(f"/product/agent/tasks/{tid}/trigger-anomaly").json()
        sim = out["simulation"]
        assert out["simulation_status"] == "contained" and out["task_status"] == "CONTAINED"
        assert sim["label"] == "Controlled Security Simulation"
        assert sim["checks"] == {"signature": "verified", "authority": "within limits", "behaviour": "anomalous"}
        assert sim["risk"]["level"] == "HIGH" and sim["risk"]["anomaly_score"] >= 0.05
        assert sim["containment"]["capability_status"] == "REVOKED"
        assert sim["containment"]["reserved_after"] == "0.00"
        assert sim["containment"]["agent"]["agent_status"] == "suspended"
        assert sim["follow_up_request"]["blocked"] is True
        assert _res(body).status == ReservationStatus.RELEASED
        assert client.post(f"/product/agent/tasks/{tid}/authorize-payment").status_code == 409
        assert _payments(_res(body).id) == 0
        # a new shopping task cannot use the contained agent
        tid2 = _task()
        _add(tid2, [("tata-salt-1kg", 1)])
        _choose(tid2, "dailymart")
        r = client.post(f"/product/agent/tasks/{tid2}/execute")
        assert r.status_code == 409 and "suspended" in r.json()["detail"]
        # the user replaces the agent explicitly → new identity, shopping works again
        rep = client.post("/product/agents/purchase/replace").json()
        assert rep["replaced_with"].startswith("purchase-agent") and rep["replaced_with"] != "purchase-agent"
        assert client.post(f"/product/agent/tasks/{tid2}/execute").json()["status"] == "AWAITING_AUTHORIZATION"
        _assert_ledger_conserved()

    def test_simulation_labelled_in_activity(self):
        tid, _ = _prepare()
        client.post(f"/product/agent/tasks/{tid}/trigger-anomaly")
        seeded = [e for e in client.get("/product/activity?limit=200").json()
                  if e["raw_event"] == "BEHAVIOUR_SIMULATION_SEEDED"]
        assert seeded and seeded[0]["simulation"] and "not customer history" in seeded[0]["payload"]["label"]

    def test_simulation_requires_pending_payment(self):
        tid = _task()
        assert client.post(f"/product/agent/tasks/{tid}/trigger-anomaly").status_code == 409


# ── direct payments & read models ────────────────────────────────────────────

class TestDirectAndReadModels:
    @pytest.mark.parametrize("path,body", [
        ("/product/payments/recharge", {"mobile_number": "9876543210", "operator": "Jio",
                                        "plan_amount": 299, "plan_description": "2GB/day"}),
        ("/product/payments/bill", {"consumer_number": "1234567890", "provider": "BESCOM", "amount": 850}),
        ("/product/payments/send-money", {"recipient_upi": "john@oksbi", "amount": 500, "purpose": "Dinner"}),
    ])
    def test_direct_payment(self, path, body):
        # Submitting prepares a request; nothing is paid until the user authorizes.
        req = client.post(path, json=body).json()
        assert req["status"] == "AWAITING_AUTHORIZATION" and req["kind"] == "direct"
        assert req["authorization"]["risk"]["level"] == "LOW" and req["result"] is None
        d = client.post(f"/product/payments/requests/{req['request_id']}/authorize").json()
        assert d["status"] == "COMPLETED" and d["result"]["risk"]["level"] == "LOW"
        with _db() as db:
            p = db.get(Payment, uuid.UUID(d["result"]["payment_id"]))
            assert db.get(Reservation, p.reservation_id).status == ReservationStatus.COMMITTED

    def test_direct_payments_reuse_one_wallet_identity(self):
        for _ in range(3):   # (the old version used "a@ok", an invalid UPI ID, so it never paid)
            req = client.post("/product/payments/send-money", json={"recipient_upi": "ab@ok", "amount": 100}).json()
            done = client.post(f"/product/payments/requests/{req['request_id']}/authorize").json()
            assert done["status"] == "COMPLETED", done
        with _db() as db:
            assert db.scalar(select(func.count(Agent.id)).where(Agent.agent_type == "direct")) == 1

    def test_security_summary(self):
        tid, _ = _prepare()
        client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        d = client.get("/product/security/summary").json()
        for k in ["summary", "policy", "agents", "capabilities", "reservations", "payments", "orders",
                  "risk", "crypto", "events"]:
            assert k in d
        assert d["crypto"]["grant_signatures_invalid"] == 0
        assert d["orders"] and d["orders"][0]["order_number"].startswith("AG-")

    def test_clean_demo_state_after_reset(self):
        from app.models.mandate import Mandate
        from app.models.user import User
        with _db() as db:
            demo_active = db.scalar(select(func.count(Reservation.id)).join(
                Capability, Capability.id == Reservation.capability_id).join(
                Mandate, Mandate.id == Capability.root_mandate_id).join(User, User.id == Mandate.user_id).where(
                User.is_demo.is_(True), Reservation.status == ReservationStatus.RESERVED))
            assert demo_active == 0
        d = client.get("/product/security/summary").json()
        assert d["orders"] == []
        mine = [c for c in d["capabilities"] if not c["agent_identifier"].startswith(("lab-",))]
        assert {c["agent_identifier"] for c in mine} >= {"main-agent", "search-agent", "optimization-agent"}
