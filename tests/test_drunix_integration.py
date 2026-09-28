"""AgentGuard ⇄ Drunix integration (DRUNIX_MODE=off / enforce) against a fake bridge.

The fake bridge speaks the real bridge's HTTP contract (drunix/bridge) and
records every call. It deliberately does NOT re-implement the chaincode's
authority rules — those are tested in drunix/chaincode-agentauth (Go unit
tests) and against the real Drunix network (tests/test_drunix_live.py).
What is tested here is AgentGuard's side of the contract: which operations
are sent, in which order, what fails closed, what becomes SYNC_PENDING, and
that no payment is ever recorded without a VALID Drunix commit.
"""
import json
import uuid
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import settings
from app.database import SessionLocal
from app.exceptions import (
    DrunixConflictError,
    DrunixInvalidCommitError,
    DrunixRejectedError,
    DrunixTimeoutError,
    DrunixUnavailableError,
)
from app.main import app
from app.models.agent_task import AgentTask
from app.models.commerce import Order
from app.models.ledger_transaction import LedgerTransaction
from app.models.payment import Payment
from app.models.reservation import Reservation, ReservationStatus
from app.services import drunix_client, ledger_sync
from app.services.drunix_client import DrunixClient

client = TestClient(app)

BASKET = [("amul-toned-milk-1l", 4), ("aashirvaad-atta-5kg", 1), ("tata-salt-1kg", 2)]


class FakeBridge:
    """In-memory stand-in for drunix/bridge. Tracks which entities were
    written (so existence queries answer correctly) and lets a test script
    the answer to specific functions."""

    def __init__(self):
        self.calls = []            # (endpoint, function, args)
        self.scripted = {}         # function -> list of (status, body) consumed in order
        self.existing = set()
        self.state = {}            # id -> document returned by Get*
        self.block = 100

    def script(self, function, *responses):
        self.scripted.setdefault(function, []).extend(responses)

    def functions(self, endpoint="submit"):
        return [f for e, f, _ in self.calls if e == endpoint]

    def args_of(self, function):
        return [a for e, f, a in self.calls if e == "submit" and f == function]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "bridge": "ok", "drunix": "reachable",
                                             "chaincode_status": "ready", "channel": "mychannel",
                                             "chaincode": "agentauth", "contract": {"version": "test"}})
        body = json.loads(request.content)
        fn, args = body["function"], body["args"]
        endpoint = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, fn, args))
        if self.scripted.get(fn):
            status, payload = self.scripted[fn].pop(0)
            return httpx.Response(status, json=payload)
        if endpoint == "evaluate":
            if args and args[0] in self.existing:
                return httpx.Response(200, json={"ok": True, "result": self.state.get(args[0], {"id": args[0]})})
            return httpx.Response(409, json={"ok": False, "error": {
                "category": "CHAINCODE_REJECTED", "code": "NOT_FOUND", "stage": "evaluate",
                "message": f"{args[0] if args else '?'} not found"}})
        written = {"RegisterMandate": 0, "RegisterRootCapability": 0, "Delegate": 1, "Reserve": 1}.get(fn)
        if written is not None:
            self.existing.add(args[written])
        self.block += 1
        return httpx.Response(200, json={"ok": True, "function": fn, "tx_id": uuid.uuid4().hex * 2,
                                         "block_number": self.block, "status": "VALID", "validation_code": "VALID",
                                         "result": {}, "latency_ms": 2000, "channel": "mychannel",
                                         "chaincode": "agentauth"})


def _error(category, code="", status=409, **extra):
    return status, {"ok": False, "error": {"category": category, "code": code, "stage": extra.pop("stage", "endorse"),
                                           "message": extra.pop("message", f"{category} {code}"), **extra}}


@pytest.fixture()
def bridge(monkeypatch):
    fake = FakeBridge()
    drunix_client.set_client(DrunixClient("http://drunix-bridge.test", transport=httpx.MockTransport(fake.handler)))
    monkeypatch.setattr(settings, "drunix_bridge_url", "http://drunix-bridge.test")
    yield fake
    drunix_client.set_client(None)


@pytest.fixture()
def enforce(monkeypatch, bridge):
    monkeypatch.setattr(settings, "drunix_mode", "enforce")
    assert client.post("/demo/reset").status_code == 200
    bridge.calls.clear()
    return bridge


def _prepare(merchant="dailymart"):
    r = client.post("/product/agent/tasks", json={"instruction": "Buy groceries for me under ₹3,000"})
    tid = r.json()["task_id"]
    for pid, q in BASKET:
        assert client.post(f"/product/agent/tasks/{tid}/cart/items", json={"product_id": pid, "quantity": q}).status_code == 200
    assert client.post(f"/product/agent/tasks/{tid}/merchant", json={"mode": "user", "merchant_id": merchant}).status_code == 200
    return tid, client.post(f"/product/agent/tasks/{tid}/execute")


def _task(resp):
    """Task body of a product response (error responses wrap it under "task")."""
    b = resp.json()
    return b.get("task", b)


def _journal(**where):
    with SessionLocal() as db:
        q = select(LedgerTransaction).order_by(LedgerTransaction.created_at)
        for k, v in where.items():
            q = q.where(getattr(LedgerTransaction, k) == v)
        return db.scalars(q).all()


def _payments(reservation_id):
    with SessionLocal() as db:
        return db.scalar(select(func.count(Payment.id)).where(Payment.reservation_id == uuid.UUID(reservation_id)))


def _reservation(reservation_id):
    with SessionLocal() as db:
        return db.get(Reservation, uuid.UUID(reservation_id))


# ── DRUNIX_MODE=off ──────────────────────────────────────────────────────────

class TestModeOff:
    def test_off_makes_no_ledger_calls_and_flow_is_unchanged(self, bridge, monkeypatch):
        monkeypatch.setattr(settings, "drunix_mode", "off")
        assert client.post("/demo/reset").status_code == 200
        tid, r = _prepare()
        assert r.status_code == 200 and r.json()["status"] == "AWAITING_AUTHORIZATION"
        assert "drunix" not in r.json()["authorization"]["checks"]
        done = client.post(f"/product/agent/tasks/{tid}/authorize-payment").json()
        assert done["status"] == "COMPLETED"
        assert bridge.calls == []
        assert any(s["key"] == "drunix_off" for s in done["steps"])
        receipt = client.get(f"/product/orders/{done['result']['order_number']}").json()
        assert receipt["drunix"]["enforced"] is False

    def test_status_endpoint_reports_off_without_bridge(self, monkeypatch):
        monkeypatch.setattr(settings, "drunix_mode", "off")
        monkeypatch.setattr(settings, "drunix_bridge_url", "")
        s = client.get("/drunix/status").json()
        assert s["mode"] == "off" and s["connected"] is False and s["bridge"] == "not_configured"
        assert client.get("/drunix").status_code == 200
        assert client.get("/health").json()["drunix"] == {"mode": "off"}


# ── DRUNIX_MODE=enforce: happy path ─────────────────────────────────────────

class TestEnforceHappyPath:
    def test_standing_authority_is_registered_on_drunix(self, bridge, monkeypatch):
        monkeypatch.setattr(settings, "drunix_mode", "enforce")
        assert client.post("/demo/reset").status_code == 200
        fns = bridge.functions()
        assert fns[:2] == ["RegisterMandate", "RegisterRootCapability"]
        assert fns.count("Delegate") == 2          # search (₹0) + negotiation grants
        mandate = bridge.args_of("RegisterMandate")[0]
        assert mandate[3] == "INR" and mandate[4] == str(1_000_000)   # ₹10,000 in paise

    def test_full_payment_flow_is_enforced_in_order(self, enforce):
        tid, r = _prepare()
        body = r.json()
        assert r.status_code == 200 and body["status"] == "AWAITING_AUTHORIZATION"
        a = body["authorization"]
        assert enforce.functions() == ["Delegate", "Reserve"]
        assert a["checks"]["drunix"].startswith("VALID · block ")
        assert a["drunix"]["reserve"]["tx_id"] and a["drunix"]["delegate"]["tx_id"]
        reserve_args = enforce.args_of("Reserve")[0]
        assert reserve_args[1] == a["reservation_id"]
        assert int(reserve_args[3]) == int(Decimal(a["total"]) * 100)         # paise, never floats
        keys = [s["key"] for s in body["steps"]]
        assert keys.index("risk") < keys.index("drunix_reserve_submitted") < keys.index("drunix_reserve") < keys.index("reserved")
        assert _payments(a["reservation_id"]) == 0

        done = client.post(f"/product/agent/tasks/{tid}/authorize-payment").json()
        assert done["status"] == "COMPLETED"
        assert enforce.functions() == ["Delegate", "Reserve", "Commit", "ReturnUnused"]
        commit_args = enforce.args_of("Commit")[0]
        assert commit_args[0] == a["reservation_id"] and commit_args[2] == done["result"]["utr_reference"]
        keys = [s["key"] for s in done["steps"]]
        assert keys.index("pay") < keys.index("drunix_commit_submitted") < keys.index("drunix_commit") < keys.index("paid")
        assert keys[-1] == "receipt"
        assert done["result"]["drunix"]["commit"]["tx_id"]

        receipt = client.get(f"/product/orders/{done['result']['order_number']}").json()
        d = receipt["drunix"]
        assert d["enforced"] is True
        assert d["reserve"]["tx_id"] == a["drunix"]["reserve"]["tx_id"]
        assert d["commit"]["tx_id"] == done["result"]["drunix"]["commit"]["tx_id"]
        assert d["commit"]["block_number"] > d["reserve"]["block_number"]

    def test_transaction_metadata_is_journalled(self, enforce):
        tid, r = _prepare()
        client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        rid = r.json()["authorization"]["reservation_id"]
        rows = _journal(entity_id=rid)
        assert [x.function for x in rows] == ["Reserve", "Commit"]
        assert all(x.outcome == "VALID" and x.tx_id and x.block_number and x.latency_ms == 2000 for x in rows)
        txs = client.get("/drunix/transactions?limit=10").json()["transactions"]
        assert {"Reserve", "Commit"} <= {t["function"] for t in txs}


# ── fail closed: authority-granting operations ──────────────────────────────

class TestFailClosed:
    def test_reserve_rejected_by_drunix_leaves_no_hold(self, enforce):
        enforce.script("Reserve", _error("CHAINCODE_REJECTED", "INSUFFICIENT_AUTHORITY",
                                         message="capability has 100 paise unallocated; reserve asks for 900"))
        tid, r = _prepare()
        assert r.status_code == 409 and r.json()["error"] == "DrunixRejectedError"
        body = _task(r)
        assert body["status"] == "FAILED"
        step = next(s for s in body["steps"] if s["key"] == "drunix_reserve_failed")
        assert step["drunix_code"] == "INSUFFICIENT_AUTHORITY"
        with SessionLocal() as db:
            task = db.get(AgentTask, tid)
            assert "reservation" not in (task.refs or {})
        # the rejection is on record although the business transaction rolled back
        rej = _journal(function="Reserve", outcome="REJECTED")
        assert rej and rej[-1].code == "INSUFFICIENT_AUTHORITY"

    def test_delegation_rejected_by_drunix_stops_checkout(self, enforce):
        enforce.script("Delegate", _error("CHAINCODE_REJECTED", "INSUFFICIENT_AUTHORITY"))
        tid, r = _prepare()
        assert r.status_code == 409 and _task(r)["status"] == "FAILED"
        assert "Reserve" not in enforce.functions()

    def test_unavailable_drunix_fails_closed(self, enforce):
        enforce.script("Reserve", _error("DRUNIX_UNAVAILABLE", "Unavailable", status=503, stage="submit"))
        tid, r = _prepare()
        assert r.status_code == 503 and _task(r)["status"] == "FAILED"
        assert _journal(function="Reserve", outcome="UNAVAILABLE")

    def test_commit_rejected_never_records_a_payment(self, enforce):
        enforce.script("Commit", _error("CHAINCODE_REJECTED", "ANCESTOR_INACTIVE",
                                        message="ancestor capability is REVOKED"))
        tid, r = _prepare()
        rid = r.json()["authorization"]["reservation_id"]
        out = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert out.status_code == 409
        assert _task(out)["status"] == "PAYMENT_FAILED"
        assert _payments(rid) == 0
        with SessionLocal() as db:
            assert db.scalar(select(func.count(Order.id)).where(Order.reservation_id == uuid.UUID(rid))) == 0
        assert _reservation(rid).status == ReservationStatus.RELEASED
        assert "Release" in enforce.functions()

    def test_success_without_valid_commit_is_not_trusted(self, enforce):
        # A bridge that says ok but not VALID must never let the payment through.
        enforce.script("Commit", (200, {"ok": True, "function": "Commit", "tx_id": "t", "block_number": 1,
                                        "status": "MVCC_READ_CONFLICT", "validation_code": "MVCC_READ_CONFLICT"}))
        tid, r = _prepare()
        rid = r.json()["authorization"]["reservation_id"]
        out = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert out.status_code == 502 and _task(out)["status"] == "PAYMENT_FAILED"
        assert _payments(rid) == 0

    def test_mvcc_conflict_on_commit_keeps_hold_for_retry(self, enforce):
        enforce.script("Commit", _error("MVCC_READ_CONFLICT", "MVCC_READ_CONFLICT", stage="commit",
                                        tx_id="abc", block_number=7))
        tid, r = _prepare()
        rid = r.json()["authorization"]["reservation_id"]
        out = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert out.status_code == 409 and _task(out)["status"] == "AWAITING_AUTHORIZATION"
        assert _payments(rid) == 0 and _reservation(rid).status == ReservationStatus.RESERVED
        again = client.post(f"/product/agent/tasks/{tid}/authorize-payment").json()
        assert again["status"] == "COMPLETED" and _payments(rid) == 1

    def test_commit_timeout_then_recovered_from_ledger(self, enforce):
        tid, r = _prepare()
        rid = r.json()["authorization"]["reservation_id"]
        enforce.script("Commit", _error("DRUNIX_TIMEOUT", "DeadlineExceeded", status=504, stage="commit_status",
                                        tx_id="t-lost"))
        out = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert out.status_code == 504 and _task(out)["status"] == "AWAITING_AUTHORIZATION"
        assert _payments(rid) == 0
        # The first commit did land on Drunix: the retry is told ALREADY_COMMITTED
        # and AgentGuard verifies that on-chain before accepting it.
        ref = ledger_sync.simulated_payment_reference(uuid.UUID(rid))
        enforce.script("Commit", _error("CHAINCODE_REJECTED", "ALREADY_COMMITTED"))
        enforce.state[rid] = {"id": rid, "status": "COMMITTED", "paymentRef": ref, "commitTx": "t-lost"}
        done = client.post(f"/product/agent/tasks/{tid}/authorize-payment").json()
        assert done["status"] == "COMPLETED" and _payments(rid) == 1
        assert done["result"]["drunix"]["commit"]["tx_id"] == "t-lost"
        assert _journal(entity_id=rid, outcome="RECOVERED")

    def test_already_committed_with_other_reference_is_rejected(self, enforce):
        tid, r = _prepare()
        rid = r.json()["authorization"]["reservation_id"]
        enforce.script("Commit", _error("CHAINCODE_REJECTED", "ALREADY_COMMITTED"))
        enforce.state[rid] = {"id": rid, "status": "COMMITTED", "paymentRef": "SOMEONE-ELSE", "commitTx": "x"}
        out = client.post(f"/product/agent/tasks/{tid}/authorize-payment")
        assert out.status_code == 409 and _payments(rid) == 0


# ── restrictive operations: SYNC_PENDING ─────────────────────────────────────

class TestSyncPending:
    def test_release_failure_is_sync_pending_then_synced(self, enforce):
        tid, r = _prepare()
        rid = r.json()["authorization"]["reservation_id"]
        enforce.script("Release", _error("DRUNIX_UNAVAILABLE", "Unavailable", status=503, stage="submit"))
        out = client.post(f"/product/agent/tasks/{tid}/cancel").json()
        assert out["status"] == "CANCELLED"                              # AgentGuard's release still stands
        assert _reservation(rid).status == ReservationStatus.RELEASED
        pending = _journal(entity_id=rid, outcome="SYNC_PENDING")
        assert len(pending) == 1 and pending[0].function == "Release"
        assert client.get("/drunix/status").json()["sync_pending"] >= 1

        result = client.post("/drunix/sync/retry").json()
        assert result["synced"] >= 1
        synced = _journal(entity_id=rid, function="Release")
        assert synced[0].outcome == "SYNCED" and synced[0].attempts == 2 and synced[0].tx_id

    def test_policy_reissue_mirrors_revocation_and_mandate(self, enforce):
        r = client.put("/product/policy", json={"overall_authority": 8000})
        assert r.status_code == 200, r.json()
        fns = enforce.functions()
        assert fns.index("Revoke") < fns.index("RevokeMandate") < fns.index("RegisterMandate")
        revoke = enforce.args_of("Revoke")[0]
        assert len(json.loads(revoke[2])) == 2                           # search + negotiation beneath the root


# ── bridge client contract ──────────────────────────────────────────────────

class TestClientContract:
    def _client(self, handler):
        return DrunixClient("http://b.test", transport=httpx.MockTransport(handler), timeout=1)

    def test_error_categories_map_to_typed_exceptions(self):
        cases = [(_error("CHAINCODE_REJECTED", "HOLDER_MISMATCH"), DrunixRejectedError),
                 (_error("MVCC_READ_CONFLICT", "MVCC_READ_CONFLICT", stage="commit"), DrunixConflictError),
                 (_error("DRUNIX_INVALID_COMMIT", "ENDORSEMENT_POLICY_FAILURE", status=502), DrunixInvalidCommitError),
                 (_error("DRUNIX_UNAVAILABLE", "Unavailable", status=503), DrunixUnavailableError),
                 (_error("DRUNIX_TIMEOUT", "DeadlineExceeded", status=504), DrunixTimeoutError)]
        for (status, body), exc in cases:
            c = self._client(lambda req, s=status, b=body: httpx.Response(s, json=b))
            with pytest.raises(exc) as info:
                c.submit("Reserve", ["x"])
            assert info.value.code == body["error"]["code"]

    def test_transport_failures(self):
        def timeout(req):
            raise httpx.ReadTimeout("slow", request=req)

        def refused(req):
            raise httpx.ConnectError("refused", request=req)

        with pytest.raises(DrunixTimeoutError):
            self._client(timeout).submit("Reserve", [])
        with pytest.raises(DrunixUnavailableError):
            self._client(refused).submit("Reserve", [])
        with pytest.raises(DrunixUnavailableError):
            self._client(lambda req: httpx.Response(200, text="<html>proxy</html>")).submit("Reserve", [])
        with pytest.raises(DrunixUnavailableError):
            DrunixClient("", timeout=1).submit("Reserve", [])
        assert self._client(refused).health()["bridge"] == "unreachable"

    def test_ok_without_tx_id_is_invalid(self):
        c = self._client(lambda req: httpx.Response(200, json={"ok": True, "status": "VALID", "validation_code": "VALID"}))
        with pytest.raises(DrunixInvalidCommitError):
            c.submit("Commit", ["r"])

    def test_paise_conversion_is_exact(self):
        assert ledger_sync.paise(Decimal("2450.00")) == 245000
        assert ledger_sync.paise(Decimal("0.01")) == 1
        with pytest.raises(ValueError):
            ledger_sync.paise(Decimal("1.005"))
