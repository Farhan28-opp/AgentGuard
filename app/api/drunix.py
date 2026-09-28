"""Drunix ledger monitoring + Security Lab API.

  GET  /drunix/status                      mode, bridge / network / chaincode health, journal summary
  GET  /drunix/transactions                journal of Drunix transactions AgentGuard submitted
  GET  /drunix/reconciliation              PostgreSQL vs on-chain state for recent entities
  GET  /drunix/ledger/capabilities/{id}    on-chain capability + ancestors + mandate
  GET  /drunix/ledger/reservations/{id}    on-chain reservation
  POST /drunix/sync/retry                  re-submit SYNC_PENDING restrictive operations
  GET  /drunix/lab/scenarios               Security Lab catalogue
  POST /drunix/lab/setup                   (re)create the on-chain lab authority tree
  POST /drunix/lab/{scenario}              run one ledger-bypass attack directly against Drunix

Status fields report what was actually observed: "connected" is only shown
when the bridge answered a real query against the deployed chaincode.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.exceptions import DrunixError
from app.models.ledger_transaction import LedgerTransaction
from app.services import drunix_lab, ledger_sync
from app.services.drunix_client import get_client

router = APIRouter(prefix="/drunix", tags=["drunix"])

LAB_INFO = [
    {"id": "over_limit", "title": "Over-limit reserve",
     "attack": "Reserve ₹1 more than the capability's on-chain unallocated authority, then reserve as an agent that does not hold it.",
     "expected": "INSUFFICIENT_AUTHORITY and HOLDER_MISMATCH; pool unchanged."},
    {"id": "double_commit", "title": "Double commit",
     "attack": "Reserve and commit a hold, then commit the same hold again with a different payment reference.",
     "expected": "Second commit: ALREADY_COMMITTED; committed authority counted once."},
    {"id": "idempotency_reuse", "title": "Idempotency-key reuse",
     "attack": "Reuse an idempotency key with a different amount and at a different merchant.",
     "expected": "Identical retry returns the original hold; changed parameters: IDEMPOTENCY_CONFLICT."},
    {"id": "commit_after_revoke", "title": "Commit after revocation",
     "attack": "Revoke a capability without naming its open hold, then try to commit that hold and reserve again.",
     "expected": "CAPABILITY_INACTIVE for both — revocation cannot be bypassed."},
    {"id": "concurrent_race", "title": "Concurrent reserve race",
     "attack": "Three simultaneous reserves of 60% of the remaining authority each.",
     "expected": "At most one commits VALID; the rest MVCC_READ_CONFLICT / INSUFFICIENT_AUTHORITY; no overspend."},
    {"id": "live_capability", "title": "Attack the live Main Agent capability",
     "attack": "Directly reserve more than the real Main Agent capability holds on-chain, skipping every AgentGuard check.",
     "expected": "INSUFFICIENT_AUTHORITY from the chaincode."},
]


def _row(r: LedgerTransaction) -> dict:
    return {"id": str(r.id), "function": r.function, "entity_type": r.entity_type, "entity_id": r.entity_id,
            "outcome": r.outcome, "source": r.source, "tx_id": r.tx_id, "block_number": r.block_number,
            "validation_code": r.validation_code, "category": r.category, "code": r.code, "message": r.message,
            "latency_ms": r.latency_ms, "attempts": r.attempts, "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat()}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    mode = "enforce" if ledger_sync.enabled() else "off"
    configured = bool(settings.drunix_bridge_url)
    health = get_client().health() if configured else {"status": "not_configured", "bridge": "not_configured"}
    counts = dict(db.execute(select(LedgerTransaction.outcome, func.count()).group_by(LedgerTransaction.outcome)).all())
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    lat = db.execute(select(func.avg(LedgerTransaction.latency_ms), func.max(LedgerTransaction.latency_ms),
                            func.count()).where(LedgerTransaction.outcome == "VALID",
                                                LedgerTransaction.created_at >= since)).one()
    last = db.scalar(select(LedgerTransaction).where(LedgerTransaction.outcome == "VALID")
                     .order_by(LedgerTransaction.created_at.desc()).limit(1))
    connected = health.get("status") == "ok" and health.get("chaincode_status") == "ready"
    return {
        "mode": mode,
        "bridge_configured": configured,
        "connected": connected,
        "bridge": health.get("bridge", "unknown"),
        "network": health.get("drunix", "unknown"),
        "chaincode_status": health.get("chaincode_status", "unknown"),
        "channel": health.get("channel"), "chaincode": health.get("chaincode"),
        "peer_endpoint": health.get("peer_endpoint"), "msp_id": health.get("msp_id"),
        "contract": health.get("contract"), "health_error": health.get("error"),
        "journal": {k: int(v) for k, v in counts.items()},
        "sync_pending": int(counts.get("SYNC_PENDING", 0)),
        "latency_24h": {"avg_ms": round(float(lat[0]), 1) if lat[0] is not None else None,
                        "max_ms": lat[1], "valid_transactions": lat[2]},
        "last_valid": _row(last) if last else None,
    }


@router.get("/transactions")
def transactions(limit: int = 50, source: str = "", outcome: str = "", db: Session = Depends(get_db)):
    q = select(LedgerTransaction).order_by(LedgerTransaction.created_at.desc()).limit(max(1, min(limit, 500)))
    if source:
        q = q.where(LedgerTransaction.source == source)
    if outcome:
        q = q.where(LedgerTransaction.outcome == outcome.upper())
    return {"transactions": [_row(r) for r in db.scalars(q).all()]}


def _require_bridge():
    if not settings.drunix_bridge_url:
        raise HTTPException(status_code=503, detail="DRUNIX_BRIDGE_URL is not configured.")


@router.get("/reconciliation")
def reconciliation(limit: int = 12, db: Session = Depends(get_db)):
    _require_bridge()
    return ledger_sync.reconcile(db, limit=max(1, min(limit, 50)))


@router.get("/ledger/capabilities/{capability_id}")
def ledger_capability(capability_id: str):
    _require_bridge()
    return get_client().evaluate("GetAuthorityChain", [capability_id])


@router.get("/ledger/reservations/{reservation_id}")
def ledger_reservation(reservation_id: str):
    _require_bridge()
    return get_client().evaluate("GetReservation", [reservation_id])


@router.post("/sync/retry")
def sync_retry():
    return ledger_sync.retry_pending()


def _lab_guard():
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Security Lab is only available in DEMO_MODE.")
    _require_bridge()


@router.get("/lab/scenarios")
def lab_scenarios():
    return {"scenarios": LAB_INFO}


@router.post("/lab/setup")
def lab_setup():
    _lab_guard()
    try:
        return drunix_lab.setup(force=True)
    except DrunixError as exc:
        raise HTTPException(status_code=503, detail=f"Drunix unavailable: {exc}")


@router.post("/lab/{scenario}")
def lab_run(scenario: str, db: Session = Depends(get_db)):
    _lab_guard()
    try:
        if scenario == "live_capability":
            return drunix_lab.live_capability_attack(db)
        fn = drunix_lab.SCENARIOS.get(scenario)
        if fn is None:
            raise HTTPException(status_code=404, detail=f"Unknown scenario {scenario}")
        return fn()
    except DrunixError as exc:
        raise HTTPException(status_code=503, detail=f"Drunix unavailable: {exc}")
