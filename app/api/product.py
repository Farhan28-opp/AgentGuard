"""
AgentGuard Product API — the consumer surface over the authority ledger.

Agentic shopping (flagship) — persistent task + cart, backend-owned totals:
  POST /product/agent/tasks                            instruction → task + cart (Search Agent suggests)
  GET  /product/agent/tasks/{id}                       task, cart, quotes, live authority
  POST /product/agent/tasks/{id}/cart/items            add / set quantity
  DELETE /product/agent/tasks/{id}/cart/items[/{pid}]  remove item / clear cart
  POST /product/agent/tasks/{id}/compare               Merchant Optimization Agent prices the cart at every simulated merchant
  POST /product/agent/tasks/{id}/merchant              {"mode":"user","merchant_id":…} | {"mode":"agent"}
  POST /product/agent/tasks/{id}/execute               policy → delegate → sign → verify → risk → RESERVE
  POST /product/agent/tasks/{id}/authorize-payment     signed pay → commit → order → return unused authority
  POST /product/agent/tasks/{id}/cancel                signed release + signed return of authority
  POST /product/agent/tasks/{id}/trigger-anomaly       Behavioural Risk Simulation (controlled)

Direct (user-authorized) payments — recharge, bill, send money — share the
same authorization state machine; see app/api/payment_requests.py.
Read models: GET /product/activity, GET /product/security/summary

The agents are a controlled, DETERMINISTIC orchestration runtime (not an
LLM). The ledger (capabilities / reservations in PostgreSQL) is sovereign:
the orchestration layer only requests authority moves through the same
services and signed-request verification as the public API.

Task state machine (persisted in agent_tasks):
  CREATED (shopping; cart editable) → RUNNING → AWAITING_AUTHORIZATION
      → AUTHORIZED → COMPLETED
  policy rejection at checkout → back to CREATED (nothing delegated)
  RUNNING → FAILED | REVIEW_REQUIRED | CONTAINED
  AWAITING_AUTHORIZATION → CANCELLED | EXPIRED | CONTAINED | FAILED
  AUTHORIZED → PAYMENT_FAILED
Whenever a task ends without spending its Purchase capability, the hold is
released (signed ``release``) and the unused authority is handed back to the
Main Agent (signed ``attenuate``). Authority of a CONTAINED agent is not
returned — it stays frozen in the revoked capability.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.exceptions import (
    SIGNATURE_ERRORS, AgentGuardError, CapabilityNotActiveError, HighRiskContainmentError,
    InvalidReservationTransitionError, ReservationExpiredError, RiskReviewError, http_status_for,
    DrunixError, DrunixInvalidCommitError, DrunixRejectedError,
)
from app.models.agent import Agent
from app.models.agent_task import AgentTask
from app.models.audit_log import AuditLog
from app.models.capability import Capability, CapabilityStatus
from app.models.commerce import Merchant, Order
from app.models.mandate import Mandate, MandateStatus
from app.models.reservation import Reservation, ReservationStatus
from app.schemas.capability import CapabilityCreate
from app.services import audit_service, capability_service, commerce_service, ledger_sync, policy_service, signed_ops
from app.services.catalog_seed import CATEGORY_LABELS
from app.services.commerce_service import CartStateError, CatalogError
from app.services.containment_service import contain_agent
from app.services.risk_simulation import run_behavioural_simulation

router = APIRouter(prefix="/product", tags=["product"])

MIN_BUDGET = Decimal("100")
MAX_BUDGET = Decimal("50000")
PAYABLE = "AWAITING_AUTHORIZATION"


class PolicyRejection(AgentGuardError):
    """Deterministic policy rejection before any authority is delegated."""


# ── Schemas ──────────────────────────────────────────────────────────────────

class AgentTaskRequest(BaseModel):
    instruction: str = Field(..., min_length=3, max_length=200)
    budget_inr: Optional[float] = None  # optional; must match the instruction if both given


class CartItemRequest(BaseModel):
    product_id: str = Field(..., min_length=1, max_length=64)
    quantity: int = Field(..., ge=0, le=50)
    mode: str = Field(default="set", pattern="^(set|add)$")


class MerchantChoice(BaseModel):
    mode: str = Field(..., pattern="^(user|agent)$")
    merchant_id: Optional[str] = None


# ── Instruction parsing (deterministic, no LLM) ──────────────────────────────

_CATEGORY_KEYWORDS = {
    "groceries": ("grocery", "groceries", "food", "vegetable", "milk", "ration", "kitchen"),
    "household": ("household", "cleaning", "home supplies", "detergent"),
    "personal_care": ("personal care", "toiletries", "soap", "toothpaste", "hygiene"),
    "electronics": ("electronics", "charger", "earphones", "gadget"),
}

_AMOUNT_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)\s*(k)?\b"
    r"|\b(?:under|below|within|max(?:imum)?|up\s*to|upto|budget(?:\s+of)?)\s+"
    r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d{1,2})?)\s*(k)?\b"
    r"|\b([\d,]+(?:\.\d{1,2})?)\s*(k)?\s*(?:₹|rs\.?|rupees|inr)\b",
    re.IGNORECASE,
)


def parse_instruction(instruction: str) -> Tuple[Optional[Decimal], Optional[str]]:
    """(budget, category) from e.g. "Buy groceries for me under ₹3,000"."""
    text = instruction.strip()
    budget = None
    m = _AMOUNT_RE.search(text)
    if m:
        number = m.group(1) or m.group(3) or m.group(5)
        k_suffix = m.group(2) or m.group(4) or m.group(6)
        try:
            budget = Decimal(number.replace(",", ""))
            if k_suffix:
                budget *= 1000
        except Exception:
            budget = None
    lowered = text.lower()
    category = next((c for c, words in _CATEGORY_KEYWORDS.items() if any(w in lowered for w in words)), None)
    return budget, category


# ── Helpers ──────────────────────────────────────────────────────────────────

def _q(amount) -> Decimal:
    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _m(v) -> str:
    return f"{Decimal(v):.2f}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(agent: Optional[Agent]) -> Optional[str]:
    import hashlib
    return hashlib.sha256((agent.public_key or "").encode()).hexdigest()[:12] if agent else None


def _uuid(v) -> Optional[uuid.UUID]:
    return uuid.UUID(str(v)) if v else None


def _risk_dict(risk) -> Dict[str, Any]:
    if risk is None:
        return {}
    from app.services.risk_engine import get_risk_engine
    engine = get_risk_engine()
    return {
        "level": risk.risk_level.value, "action": risk.action.value,
        "anomaly_score": risk.anomaly_score,
        "reason_codes": [rc.value for rc in risk.reason_codes], "reasons": risk.reasons,
        "engine": "IsolationForest" if engine.is_loaded else "cold-start fallback (model missing)",
    }


def _load_task(db: Session, task_id: str, lock: bool = False) -> AgentTask:
    row = db.get(AgentTask, task_id, with_for_update=lock, populate_existing=lock)
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def _agent_task(db: Session, task_id: str) -> AgentTask:
    """An agent shopping task (not a direct payment request, which has its own
    endpoints under /product/payments/requests)."""
    row = _load_task(db, task_id)
    if (row.refs or {}).get("kind") == "direct":
        raise HTTPException(status_code=404, detail="Task not found (this is a direct payment request)")
    return row


def _step(row: AgentTask, key: str, name: str, detail: str, status: str = "completed", **extra) -> None:
    s = {"key": key, "name": name, "detail": detail, "status": status, "ts": _now().isoformat()}
    s.update({k: v for k, v in extra.items() if v is not None})
    row.steps = list(row.steps or []) + [s]


def _set_step(row: AgentTask, key: str, status: str, detail: Optional[str] = None) -> None:
    steps = []
    for s in row.steps or []:
        if s.get("key") == key:
            s = {**s, "status": status}
            if detail:
                s["detail"] = detail
        steps.append(s)
    row.steps = steps


def _ref(row: AgentTask, **kv) -> None:
    row.refs = {**(row.refs or {}), **{k: (str(v) if isinstance(v, (uuid.UUID, Decimal)) else v)
                                       for k, v in kv.items()}}


# ── Drunix (on-chain enforcement) helpers ───────────────────────────────────

def _dtx(obj) -> Optional[Dict[str, Any]]:
    """The VALID Drunix transaction that confirmed an operation (transient
    attribute set by ledger_sync), or None when enforcement is off."""
    tx = getattr(obj, "ledger_tx", None)
    return tx.as_dict() if tx is not None else None


def _short(tx_id: Optional[str]) -> str:
    return (tx_id or "")[:16] + ("…" if tx_id and len(tx_id) > 16 else "")


def _block(tx: Dict[str, Any]) -> str:
    return f"block {tx['block_number']}" if tx.get("block_number") is not None else "already committed (recovered)"


def _drunix_steps(row: AgentTask, kind: str, tx: Optional[Dict[str, Any]], what: str) -> None:
    """Timeline entries for one Drunix-enforced operation: submitted -> VALID."""
    if tx is None:
        if not ledger_sync.enabled() and kind == "reserve":
            _step(row, "drunix_off", "Drunix enforcement is off",
                  "DRUNIX_MODE=off: this hold exists only in AgentGuard's PostgreSQL ledger.", status="skipped")
        return
    label = {"reserve": "Reserve", "commit": "Commit", "delegate": "Delegate"}[kind]
    _step(row, f"drunix_{kind}_submitted", f"Drunix {label} submitted",
          f"Submitted agentauth.{label} through the Drunix Gateway (Lite Peer). {what}",
          drunix_function=label)
    _step(row, f"drunix_{kind}", f"Drunix {label} VALID",
          f"Transaction {_short(tx['tx_id'])} endorsed by the Lite Peers, ordered, validated and committed "
          f"VALID by the Committing Peers ({_block(tx)}, {tx['latency_ms']} ms).",
          drunix_tx=tx["tx_id"], drunix_block=tx.get("block_number"), drunix_status="VALID")


def _drunix_failure(db: Session, row: AgentTask, exc: DrunixError, user, stage: str) -> None:
    audit_service.record(db, "DRUNIX_REJECTED" if isinstance(exc, DrunixRejectedError) else "DRUNIX_UNAVAILABLE",
                         "drunix", {"task": row.id, "stage": stage, **exc.as_dict()}, user_id=user.id)
    if isinstance(exc, DrunixRejectedError):
        title = f"Drunix rejected the {exc.function or stage}"
        detail = (f"The agentauth chaincode refused it on-chain: {exc.code} — {exc} "
                  "Nothing was recorded as authorized.")
    elif isinstance(exc, DrunixInvalidCommitError):
        title = f"Drunix did not commit the {exc.function or stage}"
        detail = f"Committed as {exc.code or 'invalid'}, not VALID: {exc} Nothing was recorded as authorized."
    else:
        title = f"Drunix could not confirm the {exc.function or stage}"
        detail = f"{exc.category}: {exc} AgentGuard fails closed: nothing was recorded as authorized."
    _step(row, f"drunix_{stage}_failed", title, detail, status="failed", drunix_code=exc.code or exc.category,
          drunix_tx=exc.tx_id or None)


def _merchant_names(db: Session) -> Dict[str, str]:
    return {m.id: m.name for m in db.scalars(select(Merchant)).all()}


def _context(db: Session):
    """Demo user, policy (with ACTIVE standing authority) and agents."""
    user = policy_service.demo_user(db)
    policy = policy_service.ensure_standing_authority(db, user)
    return user, policy


def _authority_snapshot(db: Session, policy, row: Optional[AgentTask]) -> Dict[str, Any]:
    """Live authority figures read from the ledger (never computed in the UI)."""
    db.expire_all()
    roles = [("main", "Main Agent", policy.main_agent_id, policy.root_capability_id),
             ("search", "Search Agent", policy.search_agent_id, policy.search_capability_id),
             ("negotiation", "Merchant Optimization Agent", policy.negotiation_agent_id, policy.negotiation_capability_id)]
    refs = (row.refs or {}) if row is not None else {}
    purchase_agent_id = refs.get("purchase_agent") or policy.purchase_agent_id
    roles.append(("purchase", "Purchase Agent", purchase_agent_id, refs.get("purchase_cap")))
    nodes = []
    for role, label, agent_id, cap_id in roles:
        agent = db.get(Agent, _uuid(agent_id)) if agent_id else None
        cap = db.get(Capability, _uuid(cap_id)) if cap_id else None
        node = {"role": role, "label": label,
                "agent_identifier": agent.agent_identifier if agent else None,
                "agent_id": str(agent.id) if agent else None,
                "agent_status": agent.status if agent else "missing",
                "key_fingerprint": _fingerprint(agent), "capability_id": None}
        if cap is not None:
            pool = cap.unallocated_authority + cap.reserved_authority + cap.committed_authority
            delegated = cap.total_authority - pool
            children = db.scalar(select(func.coalesce(func.sum(Capability.total_authority), 0))
                                 .where(Capability.parent_capability_id == cap.id))
            node.update({
                "capability_id": str(cap.id), "status": cap.status.value.upper(),
                "total": _m(cap.total_authority), "unallocated": _m(cap.unallocated_authority),
                "reserved": _m(cap.reserved_authority), "committed": _m(cap.committed_authority),
                "delegated": _m(delegated), "merchant_allowlist": cap.merchant_allowlist,
                "conservation_ok": bool(pool <= cap.total_authority and Decimal(children) == delegated),
                "grant_signed": bool(cap.grant_signature),
            })
        nodes.append(node)
    mandate = db.get(Mandate, policy.mandate_id) if policy.mandate_id else None
    return {
        "mandate_total": _m(mandate.total_authority) if mandate else None,
        "mandate_valid_until": mandate.not_after.isoformat() if mandate else None,
        "nodes": nodes,
        "conservation_ok": all(n.get("conservation_ok", True) for n in nodes),
    }


def _limits(db: Session, policy, row: AgentTask) -> Decimal:
    root = db.get(Capability, policy.root_capability_id)
    return policy_service.purchase_limit(policy, root, Decimal(row.budget))


def _limit_breakdown(db: Session, policy, row: AgentTask) -> Dict[str, Decimal]:
    """The three ceilings behind the Purchase Agent's limit, so every surface
    can say which one binds (the requested budget is never silently lowered)."""
    root = db.get(Capability, policy.root_capability_id)
    return {"budget": _q(row.budget), "per_purchase_limit": _q(policy.per_transaction_limit),
            "remaining_authority": _q(root.unallocated_authority) if root else Decimal("0.00")}


def _budget_notice(db: Session, policy, row: AgentTask) -> Optional[Dict[str, Any]]:
    lim = _limit_breakdown(db, policy, row)
    effective = _limits(db, policy, row)
    if lim["budget"] <= effective:
        return None
    if lim["per_purchase_limit"] < lim["budget"] and lim["per_purchase_limit"] <= lim["remaining_authority"]:
        binding, fix = "per_purchase_limit", (
            f"To spend up to ₹{lim['budget']:,.2f} in one purchase, raise the per-purchase limit in "
            f"Agent policy (it can be at most your overall authority ₹{_q(policy.overall_authority):,.2f}).")
    else:
        binding, fix = "remaining_authority", (
            f"The Main Agent has ₹{lim['remaining_authority']:,.2f} of its ₹{_q(policy.overall_authority):,.2f} "
            "overall authority left. Raise the overall authority in Agent policy to spend more.")
    label = "per-purchase limit" if binding == "per_purchase_limit" else "Main Agent's remaining authority"
    return {"requested_budget": _m(lim["budget"]), "effective_limit": _m(effective), "binding": binding,
            "action": "edit_policy",
            "message": (f"You asked for up to ₹{lim['budget']:,.2f}, but your {label} is "
                        f"₹{effective:,.2f}, so the Purchase Agent can be given at most ₹{effective:,.2f} for "
                        f"this purchase. Your budget was not changed. {fix}")}


def _cart_block(db: Session, policy, row: AgentTask) -> Optional[Dict[str, Any]]:
    if not row.cart_id:
        return None
    cart = commerce_service.get_cart(db, row.cart_id)
    return commerce_service.cart_view(db, cart, allowed_merchants=list(policy.allowed_merchants),
                                      allowed_categories=list(policy.allowed_categories),
                                      limit=_limits(db, policy, row), limits=_limit_breakdown(db, policy, row))


def _response(db: Session, row: AgentTask, policy=None) -> Dict[str, Any]:
    if policy is None:
        _, policy = _context(db)
    order = None
    if (row.result or {}).get("order_id"):
        o = db.get(Order, _uuid(row.result["order_id"]))
        order = commerce_service.order_dict(o) if o else None
    return {
        "task_id": row.id, "status": row.status, "instruction": row.instruction,
        "budget_inr": float(row.budget), "category": row.category,
        "category_label": CATEGORY_LABELS.get(row.category, row.category),
        "steps": row.steps or [], "cart": _cart_block(db, policy, row),
        "authority": _authority_snapshot(db, policy, row),
        "authorization": row.authorization, "result": row.result, "order": order,
        "simulation": row.simulation, "error": row.error,
        "kind": (row.refs or {}).get("kind", "agent"),
        "budget_notice": _budget_notice(db, policy, row) if row.cart_id else None,
        "policy": {"approval_mode": policy.approval_mode,
                   "approval_threshold": _m(policy.approval_threshold),
                   "per_transaction_limit": _m(policy.per_transaction_limit),
                   "allowed_categories": list(policy.allowed_categories),
                   "allowed_merchants": list(policy.allowed_merchants),
                   "purchase_limit": _m(_limits(db, policy, row))},
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _error(db: Session, row: AgentTask, code: int, error: str, detail: str, policy=None) -> JSONResponse:
    row.error = {"error": error, "detail": detail}
    db.commit()
    return JSONResponse(status_code=code, content={"error": error, "detail": detail,
                                                   "task": _response(db, row, policy)})


def _claim(db: Session, task_id: str, allowed: Tuple[str, ...], new_status: str) -> Tuple[AgentTask, Optional[str]]:
    """Atomically move a task to ``new_status`` if it is in ``allowed``
    (row lock). Returns (row, previous_status) or (row, None) if refused."""
    row = _load_task(db, task_id, lock=True)
    if row.status not in allowed:
        db.rollback()
        return _load_task(db, task_id), None
    prev = row.status
    row.status = new_status
    row.error = None
    db.commit()
    return row, prev


def _wind_down(db: Session, row: AgentTask, reason: str) -> Dict[str, Optional[str]]:
    """Release the task's hold (signed by the Purchase Agent, if it still
    holds one) and hand unused authority back to the Main Agent (signed
    ``attenuate``). Each step commits on its own; failures are recorded."""
    user, policy = _context(db)
    refs = row.refs or {}
    out: Dict[str, Optional[str]] = {"released": None, "returned": None}
    res = db.get(Reservation, _uuid(refs.get("reservation"))) if refs.get("reservation") else None
    agent = db.get(Agent, _uuid(refs.get("purchase_agent"))) if refs.get("purchase_agent") else None
    if res is not None and res.status == ReservationStatus.RESERVED and agent is not None:
        try:
            signed_ops.signed_release(db, agent=agent, reservation_id=res.id)
            audit_service.record(db, "RESERVATION_RELEASED", agent.agent_identifier,
                                 {"reason": reason, "task": row.id, "reservation_id": str(res.id),
                                  "signed": "Ed25519 release"},
                                 capability_id=res.capability_id, amount=res.amount, user_id=user.id)
            db.commit()
            out["released"] = _m(res.amount)
        except Exception as exc:
            db.rollback()
            out["release_error"] = f"{type(exc).__name__}: {exc}"
    cap = db.get(Capability, _uuid(refs.get("purchase_cap"))) if refs.get("purchase_cap") else None
    main = db.get(Agent, policy.main_agent_id)
    if cap is not None and cap.status == CapabilityStatus.ACTIVE and main is not None:
        try:
            amount = signed_ops.signed_return_unused(db, agent=main, capability_id=cap.id)
            audit_service.record(db, "AUTHORITY_RETURNED", main.agent_identifier,
                                 {"reason": reason, "task": row.id, "returned": _m(amount),
                                  "signed": "Ed25519 attenuate"},
                                 capability_id=cap.id, amount=amount, user_id=user.id)
            db.commit()
            out["returned"] = _m(amount)
        except Exception as exc:
            db.rollback()
            out["return_error"] = f"{type(exc).__name__}: {exc}"
    if row.cart_id:
        cart = commerce_service.get_cart(db, row.cart_id)
        if cart.status == "CHECKOUT":
            cart.status = "CANCELLED"
            db.commit()
    return out


def _expire_if_needed(db: Session, row: AgentTask) -> bool:
    """Lazy expiry of a pending authorization (the background sweeper may
    already have released the hold)."""
    if row.status != PAYABLE:
        return False
    res = db.get(Reservation, _uuid((row.refs or {}).get("reservation")))
    if res is None or res.expires_at > _now():
        return False
    row, prev = _claim(db, row.id, (PAYABLE,), "EXPIRED")
    if prev is None:
        return False
    wound = _wind_down(db, row, "authorization window expired")
    user = policy_service.demo_user(db)
    audit_service.record(db, "AUTHORIZATION_EXPIRED", "agentguard",
                         {"task": row.id, "reservation_id": str(res.id), **wound},
                         capability_id=res.capability_id, amount=res.amount, user_id=user.id)
    _set_step(row, "awaiting", "failed", "Not authorized before the hold expired.")
    _step(row, "expired", "Authorization window expired",
          f"The hold expired at {res.expires_at.isoformat()}; it was released and the unused "
          "authority returned to the Main Agent. No payment was made.", status="failed")
    row.error = {"error": "ReservationExpiredError",
                 "detail": "The payment authorization window expired. No money moved."}
    db.commit()
    return True


# ── Task creation & shopping ─────────────────────────────────────────────────

@router.post("/agent/tasks")
def create_agent_task(req: AgentTaskRequest, db: Session = Depends(get_db)):
    """Create a shopping task + empty cart. The Search Agent runs a catalogue
    search for the requested category; the user then picks products."""
    user, policy = _context(db)
    parsed, category = parse_instruction(req.instruction)
    if category is None:
        raise HTTPException(status_code=422, detail=(
            "AgentGuard's shopping agents understand grocery, household, personal-care and "
            "electronics requests with a rupee budget, e.g. \"Buy groceries for me under ₹3,000\"."))
    explicit = Decimal(str(req.budget_inr)) if req.budget_inr is not None else None
    if parsed is not None and explicit is not None and _q(parsed) != _q(explicit):
        raise HTTPException(status_code=422, detail=(
            f"The instruction says ₹{parsed:,.0f} but the budget field says ₹{explicit:,.0f}."))
    budget = parsed if parsed is not None else explicit
    if budget is None:
        raise HTTPException(status_code=422, detail="Please include a budget, e.g. \"under ₹3,000\".")
    if not (MIN_BUDGET <= budget <= MAX_BUDGET):
        raise HTTPException(status_code=422,
                            detail=f"Budget must be between ₹{MIN_BUDGET:,.0f} and ₹{MAX_BUDGET:,.0f}.")
    if category not in policy.allowed_categories:
        raise HTTPException(status_code=403, detail=(
            f"Your agent policy does not allow the '{CATEGORY_LABELS.get(category, category)}' category. "
            "Change it under Agent policy if you want agents to shop for it."))

    task_id = uuid.uuid4().hex[:10]
    cart = commerce_service.create_cart(db, user.id, task_id)
    row = AgentTask(id=task_id, user_id=user.id, status="CREATED", instruction=req.instruction.strip(),
                    budget=_q(budget), category=category, cart_id=cart.id, steps=[], refs={})
    db.add(row)
    search = db.get(Agent, policy.search_agent_id)
    found = commerce_service.search_catalog(db, category=category)
    merchants_carrying = sorted({o["merchant"] for p in found for o in p["offers"]})
    _step(row, "instruction", "Instruction understood",
          f"Deterministic parser: category '{CATEGORY_LABELS.get(category, category)}', budget ₹{budget:,.2f}.",
          parser="deterministic pattern match (no LLM)")
    _step(row, "search", "Search Agent searched the marketplace",
          f"Found {len(found)} {CATEGORY_LABELS.get(category, category).lower()} products across "
          f"{len(merchants_carrying)} simulated merchants. Search Agent holds ₹0 — it can look, not pay.",
          agent=search.agent_identifier if search else None, authority="0.00")
    audit_service.record(db, "TASK_CREATED", user.name,
                         {"task": task_id, "instruction": row.instruction, "budget": _m(budget)},
                         user_id=user.id, amount=_q(budget))
    audit_service.record(db, "SEARCH_PERFORMED", search.agent_identifier if search else "search-agent",
                         {"task": task_id, "category": category, "results": len(found),
                          "note": "simulated marketplace"}, user_id=user.id)
    db.commit()
    body = _response(db, row, policy)
    body["suggestions"] = found
    return body


@router.get("/agent/tasks/{task_id}")
def get_agent_task(task_id: str, db: Session = Depends(get_db)):
    row = _agent_task(db, task_id)
    _expire_if_needed(db, row)
    return _response(db, _load_task(db, task_id))


def _editable_cart(db: Session, row: AgentTask):
    if row.status != "CREATED":
        raise HTTPException(status_code=409, detail=f"Task is {row.status}; the cart can no longer change.")
    return commerce_service.get_cart(db, row.cart_id, lock=True)


@router.post("/agent/tasks/{task_id}/cart/items")
def set_cart_item(task_id: str, req: CartItemRequest, db: Session = Depends(get_db)):
    row = _load_task(db, task_id)
    cart = _editable_cart(db, row)
    try:
        commerce_service.set_quantity(db, cart, req.product_id, req.quantity, add=(req.mode == "add"))
    except (CatalogError, CartStateError) as exc:
        db.rollback()
        raise HTTPException(status_code=422 if isinstance(exc, CatalogError) else 409, detail=str(exc))
    db.commit()
    return _response(db, row)


@router.delete("/agent/tasks/{task_id}/cart/items/{product_id}")
def remove_cart_item(task_id: str, product_id: str, db: Session = Depends(get_db)):
    return set_cart_item(task_id, CartItemRequest(product_id=product_id, quantity=0), db)


@router.delete("/agent/tasks/{task_id}/cart/items")
def clear_cart(task_id: str, db: Session = Depends(get_db)):
    row = _load_task(db, task_id)
    cart = _editable_cart(db, row)
    commerce_service.clear_cart(db, cart)
    db.commit()
    return _response(db, row)


@router.post("/agent/tasks/{task_id}/compare")
def compare_merchants(task_id: str, db: Session = Depends(get_db)):
    """Merchant Optimization Agent: price the cart at every simulated
    merchant and recommend one (it does not select it — the user decides, or
    asks the agent to decide via /merchant {"mode":"agent"}). Deterministic
    optimization over listed prices — not a negotiation with any merchant."""
    user, policy = _context(db)
    row = _load_task(db, task_id)
    if row.status != "CREATED":
        raise HTTPException(status_code=409, detail=f"Task is {row.status}.")
    cart = commerce_service.get_cart(db, row.cart_id)
    if not cart.items:
        raise HTTPException(status_code=409, detail="Add products to the cart first.")
    quotes = commerce_service.compare_merchants(
        db, cart, allowed_merchants=list(policy.allowed_merchants),
        allowed_categories=list(policy.allowed_categories), limit=_limits(db, policy, row),
        limits=_limit_breakdown(db, policy, row))
    decision = commerce_service.agent_decide(quotes, budget=_q(row.budget))
    nego = db.get(Agent, policy.negotiation_agent_id)
    _step(row, "compare", "Merchant Optimization Agent compared simulated merchants",
          f"Deterministic comparison of {len(quotes)} simulated merchants' listed prices, delivery fee, delivery "
          f"time, stock, policy and your ₹{_q(row.budget):,.2f} budget (no merchant is contacted or negotiated with). "
          f"Recommendation: {decision['reason']}",
          agent=nego.agent_identifier if nego else None,
          quotes=[{"merchant": q["merchant"], "total": q["total"], "eligible": q["eligible"]} for q in quotes])
    audit_service.record(db, "MERCHANT_COMPARISON", nego.agent_identifier if nego else "negotiation-agent",
                         {"task": task_id, "recommendation": decision["merchant_id"], "reason": decision["reason"],
                          "quotes": {q["merchant_id"]: q["total"] for q in quotes},
                          "note": "comparison of simulated marketplace listings; no external merchant contacted"},
                         user_id=user.id)
    db.commit()
    return {"task_id": task_id, "quotes": quotes, "recommendation": decision}


@router.post("/agent/tasks/{task_id}/merchant")
def choose_merchant(task_id: str, req: MerchantChoice, db: Session = Depends(get_db)):
    user, policy = _context(db)
    row = _load_task(db, task_id)
    cart = _editable_cart(db, row)
    if not cart.items:
        raise HTTPException(status_code=409, detail="Add products to the cart first.")
    quotes = commerce_service.compare_merchants(
        db, cart, allowed_merchants=list(policy.allowed_merchants),
        allowed_categories=list(policy.allowed_categories), limit=_limits(db, policy, row),
        limits=_limit_breakdown(db, policy, row))
    if req.mode == "agent":
        decision = commerce_service.agent_decide(quotes, budget=_q(row.budget))
        if decision["merchant_id"] is None:
            raise HTTPException(status_code=409, detail=decision["reason"])
        merchant_id, reason = decision["merchant_id"], decision["reason"]
    else:
        q = next((q for q in quotes if q["merchant_id"] == req.merchant_id), None)
        if q is None:
            raise HTTPException(status_code=422, detail="Unknown merchant.")
        if not q["eligible"]:
            raise HTTPException(status_code=409, detail=f"{q['merchant']} can't take this order: " + "; ".join(q["problems"]))
        merchant_id, reason = q["merchant_id"], f"{q['merchant']} chosen by you."
    cart.merchant_id, cart.selection_mode, cart.selection_reason = merchant_id, req.mode, reason
    nego = db.get(Agent, policy.negotiation_agent_id)
    actor = (nego.agent_identifier if nego else "optimization-agent") if req.mode == "agent" else user.name
    _step(row, "merchant", "Platform selected" + (" by the agent" if req.mode == "agent" else " by you"),
          reason, merchant=merchant_id)
    audit_service.record(db, "MERCHANT_SELECTED", actor,
                         {"task": task_id, "merchant": merchant_id, "mode": req.mode, "reason": reason},
                         user_id=user.id)
    db.commit()
    return _response(db, row, policy)


# ── Checkout: policy → delegate → sign → verify → risk → reserve ────────────

def _delegate_purchase(db: Session, user, policy, row: AgentTask, merchant_id: str, amount: Decimal):
    root = db.get(Capability, policy.root_capability_id)
    main = db.get(Agent, policy.main_agent_id)
    purchase = db.get(Agent, policy.purchase_agent_id)
    not_after = min(root.not_after, _now() + timedelta(days=1))
    cap = capability_service.create_capability(db, CapabilityCreate(
        parent_capability_id=root.id, root_mandate_id=root.root_mandate_id,
        issued_to_agent_id=purchase.id, issued_by_agent_id=main.id, total_authority=amount,
        purpose=f"Purchase for task {row.id}", category=root.category,
        merchant_allowlist=list(policy.allowed_merchants), max_delegation_depth=2, max_fanout=0,
        not_before=max(root.not_before, _now() - timedelta(minutes=1)), not_after=not_after))
    audit_service.record(db, "CAPABILITY_DELEGATED", main.agent_identifier,
                         {"to": purchase.agent_identifier, "amount": _m(amount), "task": row.id,
                          "merchant_allowlist": list(policy.allowed_merchants), "for_merchant": merchant_id,
                          "grant_signed_by": main.agent_identifier},
                         capability_id=cap.id, mandate_id=root.root_mandate_id, amount=amount, user_id=user.id)
    return cap, main, purchase


def _policy_check(db: Session, policy, row: AgentTask, cart, q: Dict[str, Any]) -> None:
    """Deterministic pre-delegation checks (nothing to do with the ML model)."""
    total = Decimal(q["total"])
    bad_cats = sorted({CATEGORY_LABELS.get(i.product.category, i.product.category)
                       for i in cart.items if i.product.category not in policy.allowed_categories})
    if bad_cats:
        raise PolicyRejection(f"Category not allowed by your agent policy: {', '.join(bad_cats)}.")
    if q["merchant_id"] not in policy.allowed_merchants:
        raise PolicyRejection(f"{q['merchant']} is not an allowed merchant in your agent policy.")
    if not q["available"]:
        raise PolicyRejection("; ".join(q["problems"]) or "Cart can't be fulfilled.")
    if total > Decimal(policy.per_transaction_limit):
        raise PolicyRejection(f"Total ₹{total:,.2f} exceeds your per-transaction limit "
                              f"₹{Decimal(policy.per_transaction_limit):,.2f}.")
    if total > Decimal(row.budget):
        raise PolicyRejection(f"Total ₹{total:,.2f} exceeds this task's budget ₹{Decimal(row.budget):,.2f}.")
    root = db.get(Capability, policy.root_capability_id)
    if total > root.unallocated_authority:
        raise PolicyRejection(f"Total ₹{total:,.2f} exceeds the Main Agent's remaining authority "
                              f"₹{root.unallocated_authority:,.2f}.")


def _auto_approval(policy, total: Decimal) -> Optional[str]:
    if policy.approval_mode == "autonomous":
        return "policy: autonomous within policy"
    if policy.approval_mode == "above_threshold" and total <= Decimal(policy.approval_threshold):
        return f"policy: auto ≤ ₹{Decimal(policy.approval_threshold):,.0f}"
    return None


@router.post("/agent/tasks/{task_id}/execute")
def execute_agent_task(task_id: str, db: Session = Depends(get_db)):
    """Prepare the payment. Stops at AWAITING_AUTHORIZATION unless the
    agent policy's approval mode allows the Purchase Agent to settle."""
    user, policy = _context(db)
    row, prev = _claim(db, task_id, ("CREATED",), "RUNNING")
    if prev is None:
        return _error(db, row, 409, "InvalidTaskState", f"Task is already {row.status}.", policy)
    cart = commerce_service.get_cart(db, row.cart_id, lock=True)
    purchase = db.get(Agent, policy.purchase_agent_id)
    try:
        if not cart.items:
            raise PolicyRejection("The cart is empty.")
        if not cart.merchant_id:
            raise PolicyRejection("Choose a platform, or let the agent decide, before checkout.")
        if purchase is None or purchase.status != "active":
            raise PolicyRejection("Your Purchase Agent is suspended after a containment. "
                                  "Replace it under Agent policy to shop again.")
        limit = _limits(db, policy, row)
        merchant = db.get(Merchant, cart.merchant_id)
        q = commerce_service.quote(db, cart, merchant, allowed_merchants=list(policy.allowed_merchants),
                                   allowed_categories=list(policy.allowed_categories), limit=limit,
                                   limits=_limit_breakdown(db, policy, row))
        _policy_check(db, policy, row, cart, q)
    except PolicyRejection as exc:
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = "CREATED"
        audit_service.record(db, "POLICY_REJECTED", "agentguard",
                             {"task": task_id, "detail": str(exc), "layer": "policy check (deterministic)"},
                             user_id=user.id)
        return _error(db, row, 409, "PolicyRejection", str(exc), policy)

    total = Decimal(q["total"])
    basket = {k: q[k] for k in ("merchant_id", "merchant", "lines", "subtotal", "delivery_fee", "total",
                                "delivery_minutes")}
    cart.status = "CHECKOUT"
    _ref(row, basket=basket, purchase_agent=purchase.id)
    _step(row, "policy", "Policy check passed",
          f"Categories and merchant allowed; ₹{total:,.2f} within the per-transaction limit "
          f"₹{Decimal(policy.per_transaction_limit):,.2f}, the task budget and the Main Agent's remaining authority.")
    db.commit()

    try:
        cap, main, purchase = _delegate_purchase(db, user, policy, row, basket["merchant_id"], limit)
        _ref(row, purchase_cap=cap.id, purchase_authority=_m(limit))
        db.commit()
        _step(row, "purchase", "Main Agent delegated bounded authority to the Purchase Agent",
              f"₹{limit:,.2f} transferred (not minted) from the Main Agent's pool — capped by your "
              f"per-transaction limit and scoped to your allowed merchants. Grant signed by {main.agent_identifier}.",
              agent=purchase.agent_identifier, authority=_m(limit))
        delegate_tx = _dtx(cap)
        _drunix_steps(row, "delegate", delegate_tx,
                      f"The chaincode checked that the Main Agent holds the parent capability and moved ₹{limit:,.2f} "
                      "from its on-chain pool into the Purchase capability.")

        reservation = signed_ops.signed_reserve(db, agent=purchase, capability=cap, amount=total,
                                                merchant=basket["merchant_id"], category=cap.category)
        risk = getattr(reservation, "risk_result", None)
        _ref(row, reservation=reservation.id)
        for ev, payload in [
            ("SIGNED_RESERVE_REQUEST", {"operation": "reserve", "verified": True,
                                        "signing": "Ed25519 signature verified (timestamp, nonce, payload hash)",
                                        "merchant": basket["merchant_id"]}),
            ("RISK_EVALUATED", _risk_dict(risk)),
            ("AUTHORITY_RESERVED", {"reservation_id": str(reservation.id), "merchant": basket["merchant_id"],
                                    "expires_at": reservation.expires_at.isoformat()}),
        ]:
            audit_service.record(db, ev, purchase.agent_identifier if ev.startswith("SIGNED") else "agentguard",
                                 {"task": task_id, **payload}, capability_id=cap.id, amount=total, user_id=user.id)
        _step(row, "signed", "Payment request signed and verified",
              "Purchase Agent signed the reserve request with its Ed25519 key; AgentGuard verified the "
              "registered key, signature, timestamp window, one-time nonce, payload hash and operation binding.",
              agent=purchase.agent_identifier, signing_verified=True)
        _step(row, "authority", "Authority checks passed",
              "Capability chain active, agent holds the capability, amount ≤ unallocated authority, "
              "merchant in the capability allowlist, time window valid (deterministic; ML cannot override).")
        _step(row, "risk", "Behavioural risk evaluated",
              f"IsolationForest over the Purchase Agent's history: {risk.risk_level.value} → {risk.action.value} "
              f"(anomaly score {risk.anomaly_score:.4f}).",
              risk_level=risk.risk_level.value, anomaly_score=risk.anomaly_score)
        reserve_tx = _dtx(reservation)
        _drunix_steps(row, "reserve", reserve_tx,
                      "The agentauth chaincode re-validated the hold against the on-chain authority state "
                      "(mandate and every ancestor active, holder, amount ≤ unallocated, merchant, category, "
                      "time window, idempotency key).")
        _step(row, "reserved", "Authority reserved",
              f"₹{total:,.2f} moved unallocated → reserved on the Purchase capability"
              f"{' — in AgentGuard and on the Drunix ledger' if reserve_tx else ''}. "
              f"Hold expires at {reservation.expires_at.isoformat()}.", reservation_id=str(reservation.id))
        root = db.get(Capability, policy.root_capability_id)
        row.authorization = {
            "reservation_id": str(reservation.id), "reservation_status": "RESERVED",
            "merchant_id": basket["merchant_id"], "merchant": basket["merchant"],
            "delivery_minutes": basket["delivery_minutes"],
            "lines": basket["lines"], "subtotal": basket["subtotal"], "delivery_fee": basket["delivery_fee"],
            "total": basket["total"], "currency": "INR", "expires_at": reservation.expires_at.isoformat(),
            "agent": purchase.agent_identifier, "agent_key_fingerprint": _fingerprint(purchase),
            "capability_id": str(cap.id), "agent_authority": _m(limit),
            "remaining_after_payment": _m(limit - total),
            "main_agent_remaining": _m(root.unallocated_authority),
            "checks": {"identity": "VERIFIED", "signature": "VERIFIED", "policy": "WITHIN LIMIT",
                       "risk": risk.action.value, "reservation": "RESERVED",
                       **({"drunix": f"VALID · block {reserve_tx['block_number']}"} if reserve_tx else {})},
            "risk": _risk_dict(risk),
            "drunix": {"mode": "enforce" if ledger_sync.enabled() else "off",
                       "delegate": delegate_tx, "reserve": reserve_tx},
        }
        row.status = PAYABLE
        auto = _auto_approval(policy, total)
        _step(row, "awaiting", "Payment authorization required" if not auto else "Approved by your agent policy",
              "Nothing has been paid. Authorize to let the Purchase Agent settle the hold."
              if not auto else f"Approval mode '{policy.approval_mode}' allows the Purchase Agent to settle ₹{total:,.2f}.",
              status="pending" if not auto else "completed")
        audit_service.record(db, "PAYMENT_AUTHORIZATION_REQUIRED" if not auto else "PAYMENT_AUTO_APPROVED",
                             "agentguard", {"task": task_id, "approval_mode": policy.approval_mode},
                             capability_id=cap.id, amount=total, user_id=user.id)
        db.commit()
    except HighRiskContainmentError as exc:
        db.commit()  # persist the core containment (task capability revoked)
        row = _load_task(db, task_id, lock=True)
        audit_service.record(db, "HIGH_RISK_CONTAINMENT", "risk-engine",
                             {"task": task_id, **_risk_dict(exc.risk_result)},
                             capability_id=_uuid(row.refs.get("purchase_cap")), user_id=user.id)
        contained = contain_agent(db, purchase, reason="HIGH behavioural risk at checkout", user_id=user.id)
        row.status = "CONTAINED"
        commerce_service.get_cart(db, row.cart_id).status = "CANCELLED"
        _step(row, "risk", "Behavioural risk HIGH — agent contained",
              f"Score {exc.risk_result.anomaly_score:.4f}. Purchase capability revoked, agent "
              f"{contained['agent']} suspended; nothing reserved. Reasons: {'; '.join(exc.risk_result.reasons) or 'model score'}.",
              status="failed", risk_level="HIGH", anomaly_score=exc.risk_result.anomaly_score)
        return _error(db, row, 403, "HighRiskContainmentError", str(exc), policy)
    except RiskReviewError as exc:
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = "REVIEW_REQUIRED"
        audit_service.record(db, "RISK_REVIEW", "risk-engine", {"task": task_id, **_risk_dict(exc.risk_result)},
                             user_id=user.id)
        _step(row, "risk", "Behavioural risk MEDIUM — held for review",
              f"Score {exc.risk_result.anomaly_score:.4f}. Nothing reserved; unused authority returned.",
              status="failed", risk_level="MEDIUM", anomaly_score=exc.risk_result.anomaly_score)
        db.commit()
        _wind_down(db, row, "risk review required")
        return _error(db, row, 409, "RiskReviewError", str(exc), policy)
    except DrunixError as exc:
        # Fail closed: the PostgreSQL delegation / hold is rolled back because
        # Drunix did not commit it as VALID.
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = "FAILED"
        _drunix_failure(db, row, exc, user, "reserve" if exc.function == "Reserve" else "delegate")
        db.commit()
        _wind_down(db, row, "Drunix did not confirm the checkout")
        return _error(db, row, http_status_for(exc), type(exc).__name__, str(exc), policy)
    except Exception as exc:
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = "FAILED"
        name = type(exc).__name__
        if isinstance(exc, SIGNATURE_ERRORS):
            audit_service.record(db, "SIGNATURE_INVALID", "agentguard",
                                 {"task": task_id, "error": name, "detail": str(exc)}, user_id=user.id)
        _step(row, "error", "Checkout failed", f"{name}: {exc}", status="failed")
        db.commit()
        _wind_down(db, row, "checkout failed")
        code = http_status_for(exc) if isinstance(exc, AgentGuardError) else 500
        return _error(db, row, code, name, str(exc), policy)

    if auto:
        return _authorize(db, task_id, approval=auto)
    return _response(db, row, policy)


# ── Authorization → signed pay → commit → order ─────────────────────────────

def _assert_still_authorized(db: Session, row: AgentTask) -> None:
    refs = row.refs
    cap = db.get(Capability, _uuid(refs["purchase_cap"]))
    agent = db.get(Agent, _uuid(refs["purchase_agent"]))
    if agent is None or agent.status != "active":
        raise CapabilityNotActiveError("The Purchase Agent is suspended; its authority was withdrawn.")
    node = cap
    while node is not None:
        if node.status != CapabilityStatus.ACTIVE:
            raise CapabilityNotActiveError(
                f"{'Purchase' if node.id == cap.id else 'An ancestor'} capability is {node.status.value}; "
                "authority has been withdrawn.")
        node = db.get(Capability, node.parent_capability_id) if node.parent_capability_id else None
    if db.get(Mandate, cap.root_mandate_id).status != MandateStatus.ACTIVE:
        raise CapabilityNotActiveError("The spending mandate is no longer active.")


def _authorize(db: Session, task_id: str, approval: str):
    user, policy = _context(db)
    row = _agent_task(db, task_id)
    if _expire_if_needed(db, row):
        return _error(db, _load_task(db, task_id), 409, "ReservationExpiredError",
                      "The authorization window expired. No payment was made.", policy)
    row, prev = _claim(db, task_id, (PAYABLE,), "AUTHORIZED")
    if prev is None:
        reason = {
            "COMPLETED": "This payment has already been authorized and completed.",
            "AUTHORIZED": "This payment is already being authorized.",
            "CANCELLED": "This task was cancelled.",
            "CONTAINED": "This task's authority was contained by AgentGuard.",
            "EXPIRED": "The authorization window expired.",
        }.get(row.status, f"Task is {row.status}; there is no payment awaiting authorization.")
        return _error(db, row, 409, "InvalidTaskState", reason, policy)

    refs = row.refs
    reservation_id = _uuid(refs["reservation"])
    purchase = db.get(Agent, _uuid(refs["purchase_agent"]))
    main = db.get(Agent, policy.main_agent_id)
    try:
        res = db.get(Reservation, reservation_id)
        if res.expires_at <= _now():
            raise ReservationExpiredError("Reservation has expired.")
        if res.status != ReservationStatus.RESERVED:
            raise InvalidReservationTransitionError(
                f"The payment hold is {res.status.value} (authority was withdrawn).")
        _assert_still_authorized(db, row)
        audit_service.record(db, "PAYMENT_AUTHORIZED", user.name if approval == "user" else "agent-policy",
                             {"task": task_id, "reservation_id": str(res.id), "approval": approval},
                             capability_id=res.capability_id, amount=res.amount, user_id=user.id)
        payment = signed_ops.signed_pay(db, agent=purchase, reservation_id=reservation_id)
        cart = commerce_service.get_cart(db, row.cart_id, lock=True)
        order = commerce_service.create_order(
            db, cart=cart, basket=refs["basket"], payment=payment, reservation=res,
            capability_id=res.capability_id, agent_id=purchase.id, task_id=task_id,
            approval="user" if approval == "user" else approval)
        for ev, actor, payload in [
            ("SIGNED_PAY_REQUEST", purchase.agent_identifier,
             {"operation": "pay", "verified": True, "signing": "Ed25519 signature verified"}),
            ("RESERVATION_COMMITTED", "agentguard", {"reservation_id": str(res.id)}),
            ("PAYMENT_SUCCESS", "simulated-rail", {"payment_id": str(payment.id), "utr": payment.utr_reference,
                                                   "merchant": payment.merchant, "rail": "SIMULATED"}),
            ("ORDER_CREATED", "agentguard", {"order": order.order_number, "merchant": order.merchant_id,
                                             "items": sum(i.quantity for i in order.items)}),
        ]:
            audit_service.record(db, ev, actor, {"task": task_id, **payload},
                                 capability_id=res.capability_id, amount=res.amount, user_id=user.id)
        db.commit()   # commit + payment + order + stock + nonce + audit: one transaction
    except SIGNATURE_ERRORS as exc:
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = PAYABLE  # hold untouched; still releasable / expirable
        audit_service.record(db, "SIGNATURE_INVALID", "agentguard",
                             {"task": task_id, "operation": "pay", "error": type(exc).__name__,
                              "detail": str(exc)}, user_id=user.id)
        row.authorization = {**row.authorization, "checks": {**row.authorization["checks"], "signature": "FAILED"}}
        _step(row, "pay_rejected", "Signed pay request rejected",
              f"{type(exc).__name__}: {exc}. No money moved; the hold is unchanged.", status="failed")
        return _error(db, row, http_status_for(exc), type(exc).__name__, str(exc), policy)
    except ReservationExpiredError:
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = "EXPIRED"
        db.commit()
        wound = _wind_down(db, row, "authorization window expired")
        audit_service.record(db, "AUTHORIZATION_EXPIRED", "agentguard", {"task": task_id, **wound}, user_id=user.id)
        _set_step(row, "awaiting", "failed", "Not authorized before the hold expired.")
        _step(row, "expired", "Authorization window expired",
              "Hold released, reservation not committed, unused authority returned. No payment made.", status="failed")
        return _error(db, row, 409, "ReservationExpiredError",
                      "The authorization window expired. No payment was made.", policy)
    except DrunixError as exc:
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        _drunix_failure(db, row, exc, user, "commit")
        if isinstance(exc, (DrunixRejectedError, DrunixInvalidCommitError)):
            # The ledger refused to settle this hold: never pay, release it.
            row.status = "PAYMENT_FAILED"
            db.commit()
            wound = _wind_down(db, row, "Drunix refused the commit")
            audit_service.record(db, "PAYMENT_REJECTED", "drunix", {"task": task_id, **exc.as_dict(), **wound},
                                 user_id=user.id)
            db.commit()
        else:
            # Ledger unreachable / timed out / lost a race: nothing was paid and
            # the hold is untouched, so the user can simply authorize again.
            row.status = PAYABLE
            row.authorization = {**row.authorization,
                                 "checks": {**row.authorization["checks"], "drunix": exc.category}}
            db.commit()
        return _error(db, row, http_status_for(exc), type(exc).__name__, str(exc), policy)
    except (CapabilityNotActiveError, InvalidReservationTransitionError) as exc:
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = "CONTAINED" if row.simulation else "FAILED"
        db.commit()
        _wind_down(db, row, "authority withdrawn before authorization")
        audit_service.record(db, "PAYMENT_REJECTED", "agentguard",
                             {"task": task_id, "error": type(exc).__name__, "detail": str(exc)}, user_id=user.id)
        _step(row, "rejected", "Payment rejected — authority withdrawn", f"{exc} No payment made.", status="failed")
        return _error(db, row, 409, type(exc).__name__, str(exc), policy)
    except Exception as exc:
        # Rail / commit / order failure: never report success; release the hold.
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = "PAYMENT_FAILED"
        db.commit()
        wound = _wind_down(db, row, "payment failed")
        audit_service.record(db, "PAYMENT_FAILED", "simulated-rail",
                             {"task": task_id, "error": type(exc).__name__, "detail": str(exc), **wound},
                             user_id=user.id)
        _step(row, "payment_failed", "Payment failed",
              f"{type(exc).__name__}: {exc}. Reservation released; nothing committed; no order created.",
              status="failed")
        code = http_status_for(exc) if isinstance(exc, AgentGuardError) and not isinstance(exc, CatalogError) else 502
        return _error(db, row, code, "PaymentFailed", str(exc), policy)

    # Hand the unused part of the Purchase capability back to the Main Agent.
    returned = Decimal("0")
    try:
        returned = signed_ops.signed_return_unused(db, agent=main, capability_id=res.capability_id)
        audit_service.record(db, "AUTHORITY_RETURNED", main.agent_identifier,
                             {"task": task_id, "returned": _m(returned), "signed": "Ed25519 attenuate"},
                             capability_id=res.capability_id, amount=returned, user_id=user.id)
        db.commit()
    except Exception as exc:  # the payment already succeeded; surface, don't hide
        db.rollback()
        row = _load_task(db, task_id)
        _step(row, "return_failed", "Unused authority not returned", f"{type(exc).__name__}: {exc}",
              status="failed")

    row = _load_task(db, task_id, lock=True)
    root = db.get(Capability, policy.root_capability_id)
    row.status = "COMPLETED"
    row.error = None
    _set_step(row, "awaiting", "completed",
              "Authorized by you." if approval == "user" else f"Approved by agent policy ({approval}).")
    _step(row, "pay", "Purchase Agent signed the pay operation",
          "Ed25519 signature bound to this reservation verified.", signing_verified=True)
    commit_tx = _dtx(res)
    _drunix_steps(row, "commit", commit_tx,
                  "The chaincode re-checked that the hold is still RESERVED, unexpired and that the capability, "
                  "every ancestor and the mandate are still active before settling it on-chain.")
    _step(row, "paid", "Simulated payment completed",
          f"₹{payment.amount:,.2f} to {refs['basket']['merchant']} · UTR {payment.utr_reference} (simulated rail).",
          utr=payment.utr_reference)
    _step(row, "committed", "Reservation committed",
          f"₹{payment.amount:,.2f} moved reserved → committed in the same transaction as the payment and order.")
    _step(row, "order", f"Order {order.order_number} confirmed",
          f"{sum(i.quantity for i in order.items)} items from {refs['basket']['merchant']}; stock updated.")
    _step(row, "returned", "Unused authority returned to the Main Agent",
          f"₹{returned:,.2f} handed back (signed attenuate); the Purchase capability now equals what it spent.")
    drunix_receipt = {"mode": "enforce" if ledger_sync.enabled() else "off",
                      **((row.authorization or {}).get("drunix") or {}), "commit": commit_tx}
    _step(row, "receipt", "Receipt generated",
          f"Receipt {order.order_number}: AgentGuard authorization ({approval}), payment reference "
          f"{payment.utr_reference}"
          + (f", Drunix Reserve {_short((drunix_receipt.get('reserve') or {}).get('tx_id'))} and Drunix Commit "
             f"{_short(commit_tx['tx_id'])}." if commit_tx else "."))
    row.result = {
        "order_id": str(order.id), "order_number": order.order_number,
        "payment_id": str(payment.id), "reservation_id": str(reservation_id),
        "amount": _m(payment.amount), "merchant": refs["basket"]["merchant"],
        "utr_reference": payment.utr_reference, "rail": "SIMULATED",
        "approval": approval, "authorized_at": _now().isoformat(),
        "agent": purchase.agent_identifier,
        "agent_authority": refs.get("purchase_authority"), "authority_consumed": _m(payment.amount),
        "authority_returned": _m(returned), "main_agent_remaining": _m(root.unallocated_authority),
        "signing": {"algorithm": "Ed25519", "operations_verified": ["reserve", "pay", "attenuate"],
                    "agent_id": str(purchase.id), "capability_id": refs["purchase_cap"]},
        "risk": (row.authorization or {}).get("risk"),
        "drunix": drunix_receipt,
    }
    db.commit()
    return _response(db, row, policy)


@router.post("/agent/tasks/{task_id}/authorize-payment")
def authorize_payment(task_id: str, db: Session = Depends(get_db)):
    """The user authorizes the prepared payment (see _authorize)."""
    return _authorize(db, task_id, approval="user")


@router.post("/agent/tasks/{task_id}/cancel")
def cancel_task(task_id: str, db: Session = Depends(get_db)):
    """Cancel: blocks future authorization, releases the hold (signed by the
    Purchase Agent) and returns unused authority (signed by the Main Agent)."""
    user, policy = _context(db)
    _agent_task(db, task_id)
    row, prev = _claim(db, task_id, ("CREATED", PAYABLE), "CANCELLED")
    if prev is None:
        return _error(db, row, 409, "InvalidTaskState",
                      f"Task is {row.status} and can no longer be cancelled.", policy)
    wound = _wind_down(db, row, "cancelled by user")
    if row.cart_id:
        cart = commerce_service.get_cart(db, row.cart_id)
        if cart.status in ("OPEN", "CHECKOUT"):
            cart.status = "CANCELLED"
    audit_service.record(db, "TASK_CANCELLED", user.name, {"task": task_id, **wound}, user_id=user.id)
    _set_step(row, "awaiting", "failed", "Cancelled before authorization.")
    _step(row, "cancelled", "Task cancelled",
          (f"Hold of ₹{wound['released']} released. " if wound.get("released") else "") +
          (f"₹{wound['returned']} returned to the Main Agent. " if wound.get("returned") else "") +
          "No payment was made.", status="failed")
    row.error = None
    db.commit()
    return _response(db, row, policy)


# ── Behavioural Risk Simulation (controlled) ─────────────────────────────────

@router.post("/agent/tasks/{task_id}/trigger-anomaly")
def trigger_anomaly(task_id: str, db: Session = Depends(get_db)):
    """CONTROLLED SECURITY SIMULATION — not real customer history.

    Runs against a pending payment request: the Purchase Agent keeps valid
    identity and valid authority, a labelled burst of synthetic holds is
    injected on its task capability, then the agent sends one genuine signed
    request through the real pipeline (signature → authority → IsolationForest).
    HIGH → core containment (subtree revoked, holds released) → the agent is
    suspended → the pending payment can no longer be authorized.
    """
    user, policy = _context(db)
    _agent_task(db, task_id)
    row, prev = _claim(db, task_id, (PAYABLE,), "RUNNING")
    if prev is None:
        return _error(db, row, 409, "InvalidTaskState",
                      "The simulation runs against a payment request awaiting authorization.", policy)
    try:
        agent = db.get(Agent, _uuid(row.refs["purchase_agent"]))
        cap = db.get(Capability, _uuid(row.refs["purchase_cap"]))
        outcome = run_behavioural_simulation(db, agent=agent, capability=cap,
                                             context={"task": task_id}, user_id=user.id)
    except Exception:
        db.rollback()
        row = _load_task(db, task_id, lock=True)
        row.status = prev
        db.commit()
        raise
    row = _load_task(db, task_id, lock=True)
    row.simulation = outcome
    if outcome["status"] == "contained":
        row.status = "CONTAINED"
        commerce_service.get_cart(db, row.cart_id).status = "CANCELLED"
        row.authorization = {**(row.authorization or {}), "reservation_status": "RELEASED",
                             "checks": {**(row.authorization or {}).get("checks", {}), "risk": "CONTAIN",
                                        "reservation": "RELEASED"}}
        _set_step(row, "awaiting", "failed",
                  "Pending hold released by containment — this payment can no longer be authorized.")
        _step(row, "contained", "Behavioural Risk Simulation: agent contained",
              f"Valid signature and authority, abnormal behaviour → IsolationForest HIGH "
              f"({outcome['risk']['anomaly_score']:.4f}) → capability revoked, "
              f"{outcome['containment']['released_reservations']} holds released, agent suspended, "
              "next request blocked.", status="failed", risk_level="HIGH",
              anomaly_score=outcome["risk"]["anomaly_score"])
    else:
        row.status = prev
        _step(row, "simulation", "Behavioural Risk Simulation: not contained",
              f"The model scored the simulated behaviour {outcome['risk'].get('level')}; "
              "the synthetic holds were removed.", status="failed")
    db.commit()
    body = _response(db, row, policy)
    body.update({"simulation_status": outcome["status"], "task_status": row.status,
                 "risk_level": outcome["risk"].get("level"),
                 "anomaly_score": outcome["risk"].get("anomaly_score"),
                 "reasons": outcome["risk"].get("reasons", [])})
    return body


# ── Activity feed ────────────────────────────────────────────────────────────

ACTIVITY_LABELS = {
    "MANDATE_CREATED": ("Spending mandate issued", "info"),
    "AGENT_CREATED": ("Agent created", "info"),
    "AGENT_KEY_ROTATED": ("Agent key rotated", "warning"),
    "CAPABILITY_ISSUED": ("Authority issued", "info"),
    "PAYMENT_REQUESTED": ("Payment requested", "pending"),
    "TRANSACTION_REPEATED": ("Repeated as a new request", "info"),
    "ACTIVITY_HIDDEN": ("Hidden from Activity", "info"),
    "ACTIVITY_UNHIDDEN": ("Restored to Activity", "info"),
    "CAPABILITY_DELEGATED": ("Authority delegated", "info"),
    "TASK_CREATED": ("Shopping task started", "info"),
    "DRUNIX_REJECTED": ("Drunix refused the operation", "danger"),
    "DRUNIX_UNAVAILABLE": ("Drunix could not confirm", "danger"),
    "SEARCH_PERFORMED": ("Search Agent searched", "info"),
    "MERCHANT_COMPARISON": ("Agent compared merchants", "info"),
    "MERCHANT_SELECTED": ("Platform selected", "info"),
    "POLICY_REJECTED": ("Blocked by your agent policy", "warning"),
    "SIGNED_RESERVE_REQUEST": ("Signed request verified", "info"),
    "SIGNED_PAY_REQUEST": ("Signed payment verified", "info"),
    "RISK_EVALUATED": ("Risk evaluated", "info"),
    "AUTHORITY_RESERVED": ("Amount on hold", "pending"),
    "PAYMENT_AUTHORIZATION_REQUIRED": ("Waiting for your approval", "pending"),
    "PAYMENT_AUTO_APPROVED": ("Approved by your agent policy", "info"),
    "PAYMENT_AUTHORIZED": ("Payment authorized", "success"),
    "RESERVATION_COMMITTED": ("Payment committed", "success"),
    "PAYMENT_SUCCESS": ("Payment completed", "success"),
    "ORDER_CREATED": ("Order created", "success"),
    "AUTHORITY_RETURNED": ("Unused authority returned", "info"),
    "RESERVATION_RELEASED": ("Hold released", "warning"),
    "RESERVATIONS_EXPIRED": ("Expired holds released", "warning"),
    "TASK_CANCELLED": ("Task cancelled", "warning"),
    "AUTHORIZATION_EXPIRED": ("Approval window expired", "warning"),
    "PAYMENT_FAILED": ("Payment failed", "danger"),
    "PAYMENT_REJECTED": ("Payment rejected", "danger"),
    "SIGNATURE_INVALID": ("Request rejected — invalid signature", "danger"),
    "RISK_REVIEW": ("Held for risk review", "warning"),
    "HIGH_RISK_CONTAINMENT": ("Containment triggered", "danger"),
    "CAPABILITY_REVOKED": ("Capability revoked", "danger"),
    "AGENT_SUSPENDED": ("Agent suspended", "danger"),
    "AGENT_REPLACED": ("Purchase Agent replaced", "info"),
    "POST_CONTAINMENT_BLOCKED": ("Blocked: agent contained", "danger"),
    "BEHAVIOUR_SIMULATION_SEEDED": ("Security simulation started", "warning"),
    "POLICY_VIOLATION_REJECTED": ("Blocked: over authority limit", "danger"),
    "POLICY_UPDATED": ("Agent policy updated", "info"),
    "DEMO_RESET": ("Demo reset", "info"),
    "RACE_WINNER": ("Concurrent request accepted", "success"),
    "RACE_LOSER": ("Concurrent request rejected", "warning"),
}


@router.get("/activity")
def get_activity(db: Session = Depends(get_db), limit: int = 60):
    """Consumer-labelled view of backend audit events (newest first)."""
    limit = max(1, min(limit, 200))
    events = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    out = []
    for e in events:
        label, kind = ACTIVITY_LABELS.get(e.event_type, (e.event_type.replace("_", " ").title(), "info"))
        payload = e.payload or {}
        out.append({"id": str(e.id), "timestamp": e.created_at.isoformat(), "label": label, "kind": kind,
                    "actor": e.actor, "amount": _m(e.amount) if e.amount is not None else None,
                    "raw_event": e.event_type, "simulation": bool(payload.get("simulation")),
                    "payload": payload})
    return out


# ── Security Center ──────────────────────────────────────────────────────────

@router.get("/security/summary")
def security_summary(db: Session = Depends(get_db)):
    """Everything the technical Security Center shows, from the database."""
    from app.api.dashboard import (
        get_agents, get_capability_tree, get_crypto_status, get_dashboard_summary,
        get_global_events, get_payments, get_reservations, get_risk_events,
    )
    user, policy = _context(db)
    orders = db.scalars(select(Order).order_by(Order.created_at.desc()).limit(15)).all()
    return {
        "summary": get_dashboard_summary(db),
        "policy": policy_service.policy_dict(db, policy),
        "agents": get_agents(db),
        "capabilities": get_capability_tree(db),
        "reservations": get_reservations(db),
        "payments": get_payments(db),
        "orders": [commerce_service.order_dict(o) for o in orders],
        "risk": get_risk_events(db),
        "crypto": get_crypto_status(db),
        "events": get_global_events(db, limit=40),
    }
