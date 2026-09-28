"""AgentGuard ⇄ Drunix integration layer.

AgentGuard's services call the ``on_*`` hooks below *inside* their PostgreSQL
transaction, after their own deterministic checks (and, for reserve, after the
IsolationForest risk decision) have passed. In ``DRUNIX_MODE=enforce`` the hook
submits the matching agentauth chaincode transaction through the Drunix bridge
and waits for the Committing Peer's verdict:

  authority-GRANTING operations (fail closed — the caller's transaction is
  rolled back unless Drunix committed the operation as VALID):
      on_capability_created  -> RegisterMandate / RegisterRootCapability / Delegate
      on_reserve             -> Reserve
      on_commit              -> Commit

  authority-RESTRICTING operations (AgentGuard applies them regardless; if
  Drunix cannot confirm, the journal records SYNC_PENDING and the background
  sweeper retries — the ledger is never silently assumed to be in sync):
      on_release             -> Release
      on_return_unused       -> ReturnUnused
      on_revoke              -> Revoke
      on_mandate_revoked     -> RevokeMandate

Every call is written to ``ledger_transactions`` in its own database
transaction (``_journal``), so rejections remain on record even though the
business transaction that asked for them rolls back.

With ``DRUNIX_MODE=off`` every hook returns immediately and AgentGuard
behaves exactly as it did before the integration.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select

from app.config import settings
from app.exceptions import (
    DrunixConflictError,
    DrunixError,
    DrunixInvalidCommitError,
    DrunixRejectedError,
    DrunixTimeoutError,
    DrunixUnavailableError,
)
from app.services.drunix_client import DrunixTx, get_client

log = logging.getLogger("agentguard.drunix")

OUTCOME_BY_ERROR = [
    (DrunixRejectedError, "REJECTED"),
    (DrunixConflictError, "CONFLICT"),
    (DrunixInvalidCommitError, "INVALID_COMMIT"),
    (DrunixTimeoutError, "TIMEOUT"),
    (DrunixUnavailableError, "UNAVAILABLE"),
    (DrunixError, "UNAVAILABLE"),
]


def enabled() -> bool:
    return settings.drunix_enforced


# ── conversions ──────────────────────────────────────────────────────────────

def paise(amount: Decimal) -> int:
    """Rupees (Decimal, 2 dp) -> integer paise. Refuses sub-paise amounts."""
    value = (Decimal(amount) * 100)
    if value != value.to_integral_value():
        raise ValueError(f"amount {amount} has more than 2 decimal places")
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def rupees(p: Optional[int]) -> Optional[str]:
    return None if p is None else f"{Decimal(p) / 100:.2f}"


def unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _json_list(v: Optional[Iterable[str]]) -> str:
    return json.dumps(sorted(set(v or [])))


def _grant_hash(sig: Optional[str]) -> str:
    return hashlib.sha256(sig.encode()).hexdigest() if sig else ""


def simulated_payment_reference(reservation_id: uuid.UUID) -> str:
    """The simulated rail's UTR (same formula as payment_service), recorded on
    Drunix with the Commit so ledger and payment are linked."""
    return "SIM" + hashlib.sha256(f"agentguard-sim-{reservation_id}".encode()).hexdigest()[:18].upper()


# ── journal ──────────────────────────────────────────────────────────────────

def _journal(function: str, entity_type: str, entity_id: str, args: List[str], outcome: str, *,
             tx: Optional[DrunixTx] = None, err: Optional[DrunixError] = None, latency_ms: Optional[int] = None,
             source: str = "agentguard") -> Optional[uuid.UUID]:
    """Record a Drunix call in its own transaction (survives the caller's rollback)."""
    from app.database import SessionLocal
    from app.models.ledger_transaction import LedgerTransaction

    now = datetime.now(timezone.utc)
    row = LedgerTransaction(
        id=uuid.uuid4(), function=function, entity_type=entity_type, entity_id=str(entity_id),
        args=list(args), outcome=outcome, source=source,
        tx_id=(tx.tx_id if tx else (err.tx_id if err and err.tx_id else None)),
        block_number=(tx.block_number if tx else (err.block_number if err else None)),
        validation_code=(tx.validation_code if tx else None),
        category=(err.category if err else None), code=(err.code if err else None),
        message=(str(err)[:2000] if err else None), latency_ms=latency_ms,
        attempts=1, created_at=now, updated_at=now)
    s = SessionLocal()
    try:
        s.add(row)
        s.commit()
        return row.id
    except Exception:  # the journal must never mask the ledger outcome
        s.rollback()
        log.exception("could not write ledger journal entry for %s %s", function, entity_id)
        return None
    finally:
        s.close()


def _outcome_for(err: DrunixError) -> str:
    for cls, name in OUTCOME_BY_ERROR:
        if isinstance(err, cls):
            return name
    return "UNAVAILABLE"


def _submit(function: str, entity_type: str, entity_id: str, args: List[Any], *,
            source: str = "agentguard") -> DrunixTx:
    """Fail-closed submit: returns a VALID transaction or raises."""
    args = [str(a) for a in args]
    t0 = time.monotonic()
    try:
        tx = get_client().submit(function, args)
    except DrunixError as err:
        _journal(function, entity_type, entity_id, args, _outcome_for(err), err=err,
                 latency_ms=int((time.monotonic() - t0) * 1000), source=source)
        log.warning("Drunix %s for %s %s: %s", _outcome_for(err), function, entity_id, err)
        raise
    _journal(function, entity_type, entity_id, args, "VALID", tx=tx, latency_ms=tx.latency_ms, source=source)
    log.info("Drunix %s VALID tx=%s block=%s (%sms)", function, tx.tx_id, tx.block_number, tx.latency_ms)
    return tx


def _submit_restrictive(function: str, entity_type: str, entity_id: str, args: List[Any]) -> Optional[DrunixTx]:
    """Best-effort submit for operations that only *reduce* authority. Never
    raises: a failure is journalled as SYNC_PENDING (retried later) or, when
    the entity was never on the ledger, as NOT_ON_LEDGER."""
    args = [str(a) for a in args]
    t0 = time.monotonic()
    try:
        tx = get_client().submit(function, args)
    except DrunixError as err:
        latency = int((time.monotonic() - t0) * 1000)
        outcome = "NOT_ON_LEDGER" if (isinstance(err, DrunixRejectedError) and err.code == "NOT_FOUND") \
            else "SYNC_PENDING"
        _journal(function, entity_type, entity_id, args, outcome, err=err, latency_ms=latency)
        log.warning("Drunix %s -> %s for %s: %s", function, outcome, entity_id, err)
        return None
    _journal(function, entity_type, entity_id, args, "VALID", tx=tx, latency_ms=tx.latency_ms)
    return tx


def evaluate(function: str, *args: Any) -> Any:
    return get_client().evaluate(function, [str(a) for a in args])


def _on_ledger(function: str, entity_id: str) -> Optional[Dict[str, Any]]:
    try:
        return evaluate(function, entity_id)
    except DrunixRejectedError as err:
        if err.code == "NOT_FOUND":
            return None
        raise


# ── registration (authority-granting, fail closed) ──────────────────────────

def _controller_id(db) -> str:
    from app.models.agent import Agent
    sys_agent = db.scalar(select(Agent.id).where(Agent.agent_identifier == "system-agent"))
    return str(sys_agent) if sys_agent else ""


def ensure_mandate(db, mandate) -> None:
    if _on_ledger("GetMandate", str(mandate.id)) is not None:
        return
    owner = str(mandate.user_id)
    _submit("RegisterMandate", "mandate", str(mandate.id), [
        mandate.id, owner, _controller_id(db) or owner, mandate.currency or "INR",
        paise(mandate.total_authority), unix(mandate.not_before), unix(mandate.not_after)])


def _register_root(db, cap) -> DrunixTx:
    return _submit("RegisterRootCapability", "capability", str(cap.id), [
        cap.id, cap.root_mandate_id, cap.issued_to_agent_id, paise(cap.total_authority), cap.category,
        _json_list(cap.merchant_allowlist), _json_list(cap.merchant_denylist),
        unix(cap.not_before), unix(cap.not_after), _grant_hash(cap.grant_signature)])


def _delegate(db, parent, cap, amount: Decimal) -> DrunixTx:
    return _submit("Delegate", "capability", str(cap.id), [
        parent.id, cap.id, cap.issued_by_agent_id or parent.issued_to_agent_id, cap.issued_to_agent_id,
        paise(amount), cap.category, _json_list(cap.merchant_allowlist), _json_list(cap.merchant_denylist),
        unix(cap.not_before), unix(cap.not_after), _grant_hash(cap.grant_signature)])


def ensure_capability(db, cap) -> None:
    """Adopt a capability created before Drunix enforcement was switched on
    (registered at its current size). Normal operation never needs this."""
    from app.models.capability import Capability
    from app.models.mandate import Mandate

    if _on_ledger("GetCapability", str(cap.id)) is not None:
        return
    ensure_mandate(db, db.get(Mandate, cap.root_mandate_id))
    if cap.parent_capability_id is None:
        _register_root(db, cap)
        return
    parent = db.get(Capability, cap.parent_capability_id)
    ensure_capability(db, parent)
    _delegate(db, parent, cap, cap.total_authority)


def on_capability_created(db, cap) -> Optional[DrunixTx]:
    """Mirror a new root capability / delegation on Drunix (fail closed)."""
    if not enabled():
        return None
    from app.models.capability import Capability
    from app.models.mandate import Mandate

    ensure_mandate(db, db.get(Mandate, cap.root_mandate_id))
    if cap.parent_capability_id is None:
        tx = _register_root(db, cap)
    else:
        parent = db.get(Capability, cap.parent_capability_id)
        ensure_capability(db, parent)
        tx = _delegate(db, parent, cap, cap.total_authority)
    cap.ledger_tx = tx  # transient: read by the product timeline
    return tx


# ── reserve / commit (authority-granting, fail closed) ──────────────────────

def on_reserve(db, cap, reservation) -> Optional[DrunixTx]:
    """Drunix must independently accept the hold before AgentGuard records it."""
    if not enabled():
        return None
    ensure_capability(db, cap)
    tx = _submit("Reserve", "reservation", str(reservation.id), [
        cap.id, reservation.id, cap.issued_to_agent_id, paise(reservation.amount),
        reservation.currency or "INR", reservation.merchant, reservation.category,
        reservation.idempotency_key or "", settings.reservation_ttl_seconds])
    reservation.ledger_tx = tx
    return tx


def on_commit(db, cap, reservation) -> Optional[DrunixTx]:
    """Drunix must commit the hold (VALID) before any payment is recorded."""
    if not enabled():
        return None
    ref = simulated_payment_reference(reservation.id)
    args = [reservation.id, cap.issued_to_agent_id, ref]
    try:
        tx = _submit("Commit", "reservation", str(reservation.id), args)
    except DrunixRejectedError as err:
        # A previous attempt may have committed on Drunix although AgentGuard
        # never saw the answer (timeout). Accept it only if the ledger shows
        # exactly this commit.
        if err.code != "ALREADY_COMMITTED":
            raise
        onchain = _on_ledger("GetReservation", str(reservation.id)) or {}
        if onchain.get("status") != "COMMITTED" or onchain.get("paymentRef") != ref:
            raise
        tx = DrunixTx(function="Commit", tx_id=onchain.get("commitTx", ""), block_number=None,
                      validation_code="VALID", result=onchain, latency_ms=0)
        _journal("Commit", "reservation", str(reservation.id), [str(a) for a in args], "RECOVERED", tx=tx)
    reservation.ledger_tx = tx
    return tx


# ── restrictive operations (never block AgentGuard's safety actions) ────────

def _is_simulation_hold(reservation) -> bool:
    from app.services.risk_simulation import SIMULATION_KEY_PREFIX
    return bool(reservation.idempotency_key and reservation.idempotency_key.startswith(SIMULATION_KEY_PREFIX))


def on_release(db, reservation, actor: str, reason: str) -> Optional[DrunixTx]:
    if not enabled() or _is_simulation_hold(reservation):
        return None
    return _submit_restrictive("Release", "reservation", str(reservation.id),
                               [reservation.id, actor or "agentguard", reason or "released"])


def on_return_unused(db, child, issuer_id) -> Optional[DrunixTx]:
    if not enabled():
        return None
    return _submit_restrictive("ReturnUnused", "capability", str(child.id), [child.id, issuer_id])


def on_revoke(db, capability_id, actor_id, subtree_ids: List[Any], reservation_ids: List[Any],
              reason: str = "revoked by AgentGuard") -> Optional[DrunixTx]:
    if not enabled():
        return None
    descendants = [str(i) for i in subtree_ids if str(i) != str(capability_id)]
    return _submit_restrictive("Revoke", "capability", str(capability_id), [
        capability_id, actor_id, json.dumps(sorted(descendants)), json.dumps(sorted(str(r) for r in reservation_ids)),
        reason])


def on_mandate_revoked(db, mandate, actor_id) -> Optional[DrunixTx]:
    if not enabled():
        return None
    return _submit_restrictive("RevokeMandate", "mandate", str(mandate.id), [mandate.id, actor_id])


# ── sync-pending retry ───────────────────────────────────────────────────────

def retry_pending(limit: int = 25) -> Dict[str, int]:
    """Re-submit SYNC_PENDING restrictive operations in the order they happened."""
    from app.database import SessionLocal
    from app.models.ledger_transaction import LedgerTransaction

    out = {"attempted": 0, "synced": 0, "still_pending": 0}
    if not enabled():
        return out
    s = SessionLocal()
    try:
        rows = s.scalars(select(LedgerTransaction).where(LedgerTransaction.outcome == "SYNC_PENDING")
                         .order_by(LedgerTransaction.created_at).limit(limit)).all()
        for row in rows:
            out["attempted"] += 1
            t0 = time.monotonic()
            unreachable = False
            try:
                tx = get_client().submit(row.function, list(row.args))
                row.outcome, row.tx_id, row.block_number = "SYNCED", tx.tx_id, tx.block_number
                row.validation_code, row.category, row.code = "VALID", None, None
                row.message = f"applied on retry {row.attempts + 1}"
                out["synced"] += 1
            except DrunixError as err:
                if isinstance(err, DrunixRejectedError) and err.code == "NOT_FOUND":
                    row.outcome = "NOT_ON_LEDGER"
                else:
                    out["still_pending"] += 1
                    unreachable = isinstance(err, (DrunixUnavailableError, DrunixTimeoutError))
                row.category, row.code, row.message = err.category, err.code, str(err)[:2000]
            row.attempts += 1
            row.latency_ms = int((time.monotonic() - t0) * 1000)
            row.updated_at = datetime.now(timezone.utc)
            s.commit()
            if unreachable:
                break  # ledger unreachable: stop here and keep the original order
    finally:
        s.close()
    return out


# ── reconciliation (PostgreSQL vs Drunix) ────────────────────────────────────

def reconcile(db, limit: int = 12) -> Dict[str, Any]:
    """Compare recent AgentGuard capabilities and reservations with the state
    the agentauth chaincode holds for them on Drunix."""
    from app.models.capability import Capability
    from app.models.reservation import Reservation

    rows: List[Dict[str, Any]] = []
    caps = db.scalars(select(Capability).order_by(Capability.created_at.desc()).limit(limit)).all()
    for cap in caps:
        pg = {"total": paise(cap.total_authority), "unallocated": paise(cap.unallocated_authority),
              "reserved": paise(cap.reserved_authority), "committed": paise(cap.committed_authority),
              "status": cap.status.value.upper()}
        rows.append(_compare("capability", str(cap.id), cap.purpose, pg,
                             lambda: _on_ledger("GetCapability", str(cap.id)),
                             ("total", "unallocated", "reserved", "committed", "status")))
    res = db.scalars(select(Reservation).order_by(Reservation.created_at.desc()).limit(limit)).all()
    for r in res:
        if _is_simulation_hold(r):
            continue
        pg = {"amount": paise(r.amount), "status": r.status.value.upper(), "merchant": r.merchant}
        rows.append(_compare("reservation", str(r.id), r.merchant or "", pg,
                             lambda: _on_ledger("GetReservation", str(r.id)), ("amount", "status", "merchant")))
    summary = {k: sum(1 for x in rows if x["state"] == k) for k in ("MATCH", "MISMATCH", "NOT_ON_LEDGER", "UNREACHABLE")}
    return {"rows": rows, "summary": summary}


def _compare(kind: str, entity_id: str, label: str, pg: Dict[str, Any], fetch, fields) -> Dict[str, Any]:
    try:
        chain = fetch()
    except DrunixError as err:
        return {"kind": kind, "id": entity_id, "label": label, "state": "UNREACHABLE", "postgres": pg,
                "drunix": None, "diff": [], "error": str(err)}
    if chain is None:
        return {"kind": kind, "id": entity_id, "label": label, "state": "NOT_ON_LEDGER", "postgres": pg,
                "drunix": None, "diff": []}
    onchain = {f: chain.get(f) for f in fields}
    diff = [f for f in fields if onchain.get(f) != pg.get(f)]
    return {"kind": kind, "id": entity_id, "label": label, "state": "MISMATCH" if diff else "MATCH",
            "postgres": pg, "drunix": onchain, "diff": diff}
