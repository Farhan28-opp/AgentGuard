"""End-to-end tests against a REAL Drunix network (bridge + agentauth chaincode).

Skipped unless AGENTGUARD_DRUNIX_LIVE_BRIDGE points at a running bridge, e.g.

    drunix/scripts/up.sh && drunix/scripts/deploy.sh && drunix/scripts/bridge.sh --background
    AGENTGUARD_DRUNIX_LIVE_BRIDGE=http://127.0.0.1:8090 pytest tests/test_drunix_live.py -v

Every transaction here is endorsed, ordered and validated by Drunix (≈2 s each).
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services import drunix_client, drunix_lab, ledger_sync
from app.services.drunix_client import DrunixClient

LIVE = os.environ.get("AGENTGUARD_DRUNIX_LIVE_BRIDGE", "")
pytestmark = [pytest.mark.skipif(not LIVE, reason="set AGENTGUARD_DRUNIX_LIVE_BRIDGE to run against a real Drunix network"),
              pytest.mark.usefixtures("real_risk_engine")]
client = TestClient(app)


@pytest.fixture(autouse=True)
def live(monkeypatch):
    monkeypatch.setattr(settings, "drunix_mode", "enforce")
    monkeypatch.setattr(settings, "drunix_bridge_url", LIVE)
    drunix_client.set_client(DrunixClient(LIVE, timeout=60))
    yield
    drunix_client.set_client(None)


def test_bridge_reports_ready_chaincode():
    s = client.get("/drunix/status").json()
    assert s["connected"] is True and s["chaincode_status"] == "ready" and s["contract"]["contract"] == "agentauth"


@pytest.mark.parametrize("scenario", list(drunix_lab.SCENARIOS))
def test_security_lab_attack_is_blocked_by_drunix(scenario):
    result = drunix_lab.SCENARIOS[scenario]()
    assert result["verdict"] == "PASS", result


def test_enforced_purchase_end_to_end_and_reconciled():
    assert client.post("/demo/reset").status_code == 200
    r = client.post("/product/agent/tasks", json={"instruction": "Buy groceries for me under ₹3,000"})
    tid = r.json()["task_id"]
    for pid, q in [("amul-toned-milk-1l", 4), ("tata-salt-1kg", 2)]:
        client.post(f"/product/agent/tasks/{tid}/cart/items", json={"product_id": pid, "quantity": q})
    client.post(f"/product/agent/tasks/{tid}/merchant", json={"mode": "user", "merchant_id": "dailymart"})
    prepared = client.post(f"/product/agent/tasks/{tid}/execute").json()
    assert prepared["status"] == "AWAITING_AUTHORIZATION", prepared
    rid = prepared["authorization"]["reservation_id"]
    onchain = client.get(f"/drunix/ledger/reservations/{rid}").json()
    assert onchain["status"] == "RESERVED"

    done = client.post(f"/product/agent/tasks/{tid}/authorize-payment").json()
    assert done["status"] == "COMPLETED", done
    onchain = client.get(f"/drunix/ledger/reservations/{rid}").json()
    assert onchain["status"] == "COMMITTED" and onchain["paymentRef"] == done["result"]["utr_reference"]
    receipt = client.get(f"/product/orders/{done['result']['order_number']}").json()
    assert receipt["drunix"]["enforced"] is True

    # A second commit of the same hold, sent straight to Drunix, is refused.
    holder = onchain["holder"]
    with pytest.raises(Exception) as info:
        drunix_client.get_client().submit("Commit", [rid, holder, "REPLAY"])
    assert getattr(info.value, "code", "") == "ALREADY_COMMITTED"

    rec = client.get("/drunix/reconciliation?limit=6").json()
    mine = [row for row in rec["rows"] if row["id"] in (rid, prepared["authorization"]["capability_id"])]
    assert mine and all(row["state"] == "MATCH" for row in mine), mine


def test_unknown_reservation_cannot_be_committed():
    with pytest.raises(Exception) as info:
        drunix_client.get_client().submit("Commit", [f"ghost-{uuid.uuid4()}", "agent", "X"])
    assert getattr(info.value, "code", "") == "NOT_FOUND"
    assert ledger_sync.enabled()


def test_enforced_direct_payment_end_to_end_and_reconciled():
    """Recharge: prepare (nothing on-chain) → user authorizes → single-use
    mandate + capability registered → Reserve → Commit, all VALID on the real
    network; the receipt's transaction ids are the ones Drunix holds."""
    assert client.post("/demo/reset").status_code == 200
    req = client.post("/product/payments/recharge", json={"mobile_number": "9000000001", "operator": "Jio",
                                                          "plan_amount": 299, "plan_description": "live test"}).json()
    assert req["status"] == "AWAITING_AUTHORIZATION", req
    assert req["authorization"]["drunix"]["status"] == "READY"
    done = client.post(f"/product/payments/requests/{req['request_id']}/authorize").json()
    assert done["status"] == "COMPLETED", done
    rid = done["result"]["reservation_id"]
    onchain = client.get(f"/drunix/ledger/reservations/{rid}").json()
    assert onchain["status"] == "COMMITTED" and onchain["paymentRef"] == done["result"]["utr_reference"]
    receipt = client.get(f"/product/transactions/{req['request_id']}").json()
    assert receipt["drunix"]["reserve"]["tx_id"] == onchain["reserveTx"]
    assert receipt["drunix"]["commit"]["tx_id"] == onchain["commitTx"]
    assert receipt["drunix"]["register_mandate"]["block_number"] is not None
    rec = client.get("/drunix/reconciliation?limit=6").json()
    mine = [row for row in rec["rows"] if row["id"] in (rid, done["result"]["capability_id"])]
    assert len(mine) == 2 and all(row["state"] == "MATCH" for row in mine), mine
