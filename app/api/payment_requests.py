"""User-authorized direct payments (recharge, bill, send money) and the
transaction history over every payment request.

Direct payments use the SAME authorization state machine and persistence as
agent shopping — an ``agent_tasks`` row with ``refs.kind = "direct"`` and the
statuses AWAITING_AUTHORIZATION → AUTHORIZED → COMPLETED — but they are not
agent payments: nothing is delegated, the user's own wallet key signs, and the
authority is a single-use mandate for exactly the amount the user approves.

  POST /product/payments/recharge | /bill | /send-money
        Prepare only. No authority is issued and no money moves:
        validate → policy (per-payment limit) → wallet identity → behavioural
        risk pre-check (the real IsolationForest, read-only) → Drunix readiness
        → AWAITING_AUTHORIZATION with everything the review screen shows.
  GET  /product/payments/requests/{id}
  POST /product/payments/requests/{id}/authorize
        The user's explicit authorization, then, in order:
        single-use mandate + capability for exactly this amount (Drunix
        RegisterMandate / RegisterRootCapability) → wallet key signs reserve →
        deterministic authority checks + IsolationForest (authoritative) →
        Drunix Reserve VALID → wallet key signs pay → Drunix Commit VALID →
        simulated rail → receipt. Fails closed at every step.
  POST /product/payments/requests/{id}/cancel

Transaction history (agent purchases and direct payments):
  GET  /product/transactions[?include_hidden=true&kind=direct|agent]
  GET  /product/transactions/{id}             unified receipt
  POST /product/transactions/{id}/repeat      a NEW request pre-filled from this one
  POST /product/transactions/{id}/hide|unhide presentation only — payments,
                                              orders, ledger and audit records
                                              are never deleted or changed
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.product import (
    PAYABLE, _claim, _context, _drunix_failure, _drunix_steps, _dtx, _fingerprint, _load_task, _m, _now,
    _q, _ref, _risk_dict, _set_step, _short, _step, _uuid,
)
from app.database import get_db
from app.exceptions import (
    SIGNATURE_ERRORS, AgentGuardError, DrunixError, DrunixInvalidCommitError, DrunixRejectedError,
    HighRiskContainmentError, ReservationExpiredError, RiskReviewError, http_status_for,
)
from app.models.agent import Agent
from app.models.agent_task import AgentTask
from app.models.capability import Capability, CapabilityStatus
from app.models.commerce import Order
from app.models.ledger_transaction import LedgerTransaction
from app.models.mandate import Mandate, MandateStatus
from app.models.payment import Payment
from app.models.reservation import Reservation, ReservationStatus
from app.schemas.capability import CapabilityCreate
from app.schemas.reservation import ReserveRequest
from app.services import audit_service, capability_service, commerce_service, ledger_sync, signed_ops
from app.services.catalog_seed import CATEGORY_LABELS

router = APIRouter(prefix="/product", tags=["payments"])

REQUEST_TTL = timedelta(minutes=10)      # how long a prepared request can be authorized
MANDATE_TTL = timedelta(hours=1)         # validity of the single-use mandate issued on authorization

TYPES = {
    "recharge": {"label": "Mobile recharge", "category": "recharge", "tab": "recharge"},
    "bill": {"label": "Bill payment", "category": "utilities", "tab": "bills"},
    "send-money": {"label": "Send money", "category": "transfer", "tab": "send"},
}
FINAL = ("COMPLETED", "CANCELLED", "EXPIRED", "FAILED", "PAYMENT_FAILED", "REVIEW_REQUIRED", "BLOCKED", "CONTAINED")


class RechargeRequest(BaseModel):
    mobile_number: str = Field(..., pattern=r"^\d{10}$")
    operator: str = Field(..., min_length=2, max_length=40)
    plan_amount: float = Field(..., gt=0, le=50000)
    plan_description: str = Field(default="", max_length=120)


class BillPaymentRequest(BaseModel):
    consumer_number: str = Field(..., min_length=4, max_length=40)
    provider: str = Field(..., min_length=2, max_length=60)
    amount: float = Field(..., gt=0, le=50000)


class SendMoneyRequest(BaseModel):
    recipient_upi: str = Field(..., pattern=r"^[A-Za-z0-9._-]{2,64}@[A-Za-z]{2,32}$")
    amount: float = Field(..., gt=0, le=50000)
    purpose: str = Field(default="Transfer", max_length=80)


def _mask(value: str, keep: int = 4) -> str:
    v = str(value or "")
    return ("•" * max(0, len(v) - keep) + v[-keep:]) if len(v) > keep else v


def _is_direct(row: AgentTask) -> bool:
    return (row.refs or {}).get("kind") == "direct"


def _limit(policy) -> Decimal:
    return _q(policy.per_transaction_limit)


def _risk_context(policy) -> Dict[str, Any]:
    limit = _limit(policy)
    return {"authority_reference": limit, "consumption_basis": f"your ₹{limit:,.2f} per-payment limit"}


def _drunix_readiness() -> Dict[str, Any]:
    if not ledger_sync.enabled():
        return {"mode": "off", "connected": False, "status": "OFF",
                "detail": "DRUNIX_MODE=off: this payment would be recorded in AgentGuard only."}
    from app.services.drunix_client import get_client
    h = get_client().health()
    ok = h.get("status") == "ok" and h.get("chaincode_status") == "ready"
    return {"mode": "enforce", "connected": ok, "status": "READY" if ok else "NOT CONNECTED",
            "detail": ("Bridge connected, agentauth chaincode answering. Reserve and Commit must both be "
                       "committed VALID on Drunix before the payment is recorded.") if ok else
                      ("Drunix is not reachable right now. Authorizing will fail closed — nothing is paid "
                       "unless Drunix commits the Reserve and the Commit VALID.")}


def _risk_precheck(db: Session, wallet: Agent, policy, amount: Decimal, merchant: str, category: str):
    """The real feature extraction + IsolationForest on a transient (unsaved)
    single-use capability of exactly this amount. Read-only: it records no
    reservation and moves no authority. The authoritative evaluation runs
    again inside the signed reserve when the user authorizes."""
    from app.services.feature_extraction import extract_features
    from app.services.risk_engine import get_risk_engine

    ctx = _risk_context(policy)
    cap = Capability(id=uuid.uuid4(), issued_to_agent_id=wallet.id, total_authority=amount,
                     unallocated_authority=amount, reserved_authority=Decimal("0"), committed_authority=Decimal("0"))
    req = ReserveRequest(agent_id=wallet.id, amount=amount, currency="INR", merchant=merchant, category=category,
                         transaction_time=_now(), idempotency_key="precheck")
    features = extract_features(db, cap, req, authority_reference=ctx["authority_reference"])
    result = get_risk_engine().evaluate(features, context={"consumption_basis": ctx["consumption_basis"]})
    return result, features


def _risk_view(result, features, stage: str) -> Dict[str, Any]:
    return {**_risk_dict(result), "stage": stage,
            "features": {k: round(float(v), 4) for k, v in (features or {}).items()}}


def _response(db: Session, row: AgentTask) -> Dict[str, Any]:
    refs = row.refs or {}
    t = TYPES.get(refs.get("payment_type"), {})
    return {
        "task_id": row.id, "request_id": row.id, "kind": "direct", "payment_type": refs.get("payment_type"),
        "type_label": t.get("label"), "status": row.status, "merchant": refs.get("merchant"),
        "amount": _m(row.budget), "currency": "INR", "description": row.instruction,
        "steps": row.steps or [], "authorization": row.authorization, "result": row.result, "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _fail(db: Session, row: AgentTask, code: int, error: str, detail: str, **extra) -> JSONResponse:
    row.error = {"error": error, "detail": detail}
    db.commit()
    return JSONResponse(status_code=code, content={"error": error, "detail": detail, **extra,
                                                   "request": _response(db, row), "task": _response(db, row)})


# ── Prepare ──────────────────────────────────────────────────────────────────

def _prepare(db: Session, *, kind: str, amount: float, merchant: str, payee: str, description: str,
             request_fields: Dict[str, Any]):
    user, policy = _context(db)
    t = TYPES[kind]
    wallet = db.get(Agent, policy.wallet_agent_id)
    amount_dec = _q(Decimal(str(amount)))
    row = AgentTask(id=uuid.uuid4().hex[:10], user_id=user.id, status="RUNNING", instruction=description[:200],
                    budget=amount_dec, category=t["category"], cart_id=None, steps=[],
                    refs={"kind": "direct", "payment_type": kind, "merchant": merchant, "payee": payee,
                          "request": request_fields, "wallet_agent": str(wallet.id) if wallet else None})
    db.add(row)
    _step(row, "request", f"{t['label']} requested",
          f"{merchant} · {payee} · ₹{amount_dec:,.2f}. Nothing is issued, held or paid until you authorize.")
    audit_service.record(db, "PAYMENT_REQUESTED", user.name,
                         {"request": row.id, "type": kind, "merchant": merchant, "flow": "direct"},
                         amount=amount_dec, user_id=user.id)

    # 1. Policy (deterministic; the model cannot override it)
    limit = _limit(policy)
    if amount_dec > limit:
        row.status = "FAILED"
        detail = (f"₹{amount_dec:,.2f} exceeds your per-payment limit ₹{limit:,.2f}. Raise it in Agent policy "
                  "(AI Agent → Edit policy) if you want to pay more in one payment. Nothing was issued or paid.")
        _step(row, "policy", "Blocked by your policy", detail, status="failed")
        audit_service.record(db, "POLICY_REJECTED", "agentguard",
                             {"request": row.id, "type": kind, "detail": detail, "layer": "policy check (deterministic)",
                              "per_payment_limit": _m(limit)}, amount=amount_dec, user_id=user.id)
        return _fail(db, row, 409, "PolicyRejection", detail)
    _step(row, "policy", "Policy check passed",
          f"₹{amount_dec:,.2f} is within your ₹{limit:,.2f} per-payment limit. A direct payment is authorized "
          "by you and does not draw on the authority delegated to your agents.")

    # 2. Identity: the wallet key that will sign reserve and pay
    if wallet is None or wallet.status != "active" or not wallet.public_key:
        row.status = "FAILED"
        _step(row, "identity", "Wallet key unavailable", "No active wallet key is registered.", status="failed")
        return _fail(db, row, 409, "WalletUnavailable", "No active wallet key is registered for direct payments.")
    _step(row, "identity", "Wallet key verified",
          f"Your wallet key {wallet.agent_identifier} (Ed25519, fingerprint {_fingerprint(wallet)}) is registered "
          "and active. It signs the reserve and pay operations when you authorize.", signing_verified=None)

    # 3. Behavioural risk pre-check (read-only)
    risk, features = _risk_precheck(db, wallet, policy, amount_dec, merchant, t["category"])
    rv = _risk_view(risk, features, "pre-check")
    if risk.action.value != "ALLOW":
        blocked = risk.action.value == "CONTAIN"
        row.status = "BLOCKED" if blocked else "REVIEW_REQUIRED"
        _step(row, "risk", f"Behavioural risk {risk.risk_level.value} — {'blocked' if blocked else 'held for review'}",
              f"IsolationForest score {risk.anomaly_score:.4f}. Nothing was issued, held or paid. "
              f"Reasons: {'; '.join(risk.reasons) or 'model score'}.", status="failed",
              risk_level=risk.risk_level.value, anomaly_score=risk.anomaly_score)
        audit_service.record(db, "RISK_REVIEW", "risk-engine",
                             {"request": row.id, "type": kind, "stage": "pre-check", **_risk_dict(risk)},
                             amount=amount_dec, user_id=user.id)
        row.authorization = {"risk": rv}
        err = "HighRiskContainmentError" if blocked else "RiskReviewError"
        return _fail(db, row, 403 if blocked else 409, err,
                     f"{risk.risk_level.value} behavioural risk — the payment {'is blocked' if blocked else 'requires review'}.",
                     risk_level=risk.risk_level.value, anomaly_score=risk.anomaly_score, reasons=risk.reasons)
    _step(row, "risk", "Behavioural risk pre-check",
          f"IsolationForest over your wallet's payment history: {risk.risk_level.value} → {risk.action.value} "
          f"(score {risk.anomaly_score:.4f}). The same model runs again, authoritatively, when you authorize.",
          risk_level=risk.risk_level.value, anomaly_score=risk.anomaly_score)

    # 4. Drunix readiness (observed, not assumed)
    dx = _drunix_readiness()
    _step(row, "drunix_ready", "Drunix " + ("ready" if dx["connected"] else dx["status"].lower()), dx["detail"],
          status="completed" if (dx["connected"] or dx["mode"] == "off") else "pending")

    expires = _now() + REQUEST_TTL
    after = (["A single-use mandate and capability for exactly this amount are issued to your wallet key"
              + (" and registered on Drunix (RegisterMandate, RegisterRootCapability)" if dx["mode"] == "enforce" else ""),
              "Your wallet key signs the reserve; AgentGuard re-checks the authority and the behavioural risk",
              "Drunix Reserve must be committed VALID" if dx["mode"] == "enforce" else "The amount is held in AgentGuard",
              "Simulated payment on the payment rail (no real money moves)",
              "Drunix Commit must be committed VALID before the payment is recorded" if dx["mode"] == "enforce"
              else "The hold is committed in AgentGuard",
              "Receipt with the payment reference" + (" and the Drunix transaction IDs" if dx["mode"] == "enforce" else "")])
    row.authorization = {
        "flow": "direct", "flow_label": "Direct payment — authorized by you",
        "payment_type": kind, "type_label": t["label"], "merchant": merchant, "payee": payee,
        "description": description, "amount": _m(amount_dec), "currency": "INR",
        "actor": {"kind": "direct", "label": "You, with your wallet key", "agent": wallet.agent_identifier,
                  "key_fingerprint": _fingerprint(wallet)},
        "policy": {"status": "WITHIN LIMIT", "per_payment_limit": _m(limit),
                   "detail": f"Within your ₹{limit:,.2f} per-payment limit"},
        "authority": {"status": "ISSUED ON AUTHORIZATION", "amount": _m(amount_dec),
                      "detail": (f"A single-use mandate for exactly ₹{amount_dec:,.2f}, valid for "
                                 f"{int(MANDATE_TTL.total_seconds() // 3600)} hour, is issued to your wallet key only "
                                 "when you authorize. It cannot be delegated or reused.")},
        "risk": rv, "drunix": dx, "reservation_status": "PENDING YOUR APPROVAL",
        "checks": {"identity": "VERIFIED", "policy": "WITHIN LIMIT", "risk": risk.action.value,
                   "authority": "ON APPROVAL", "reservation": "PENDING APPROVAL",
                   **({"drunix": dx["status"]} if dx["mode"] == "enforce" else {})},
        "expires_at": expires.isoformat(), "after_confirmation": after,
    }
    row.status = PAYABLE
    _step(row, "awaiting", "Waiting for your authorization",
          f"Review and authorize ₹{amount_dec:,.2f} to {merchant}. The request expires at {expires.isoformat()}.",
          status="pending")
    audit_service.record(db, "PAYMENT_AUTHORIZATION_REQUIRED", "agentguard",
                         {"request": row.id, "type": kind, "merchant": merchant, "flow": "direct"},
                         amount=amount_dec, user_id=user.id)
    db.commit()
    return _response(db, row)


@router.post("/payments/recharge")
def recharge(req: RechargeRequest, db: Session = Depends(get_db)):
    plan = f" · {req.plan_description}" if req.plan_description else ""
    return _prepare(db, kind="recharge", amount=req.plan_amount, merchant=f"{req.operator} Recharge",
                    payee=f"Mobile {_mask(req.mobile_number)}",
                    description=f"{req.operator} prepaid recharge ₹{req.plan_amount:,.0f}{plan}",
                    request_fields=req.model_dump())


@router.post("/payments/bill")
def pay_bill(req: BillPaymentRequest, db: Session = Depends(get_db)):
    return _prepare(db, kind="bill", amount=req.amount, merchant=req.provider,
                    payee=f"Consumer {_mask(req.consumer_number)}",
                    description=f"{req.provider} bill payment", request_fields=req.model_dump())


@router.post("/payments/send-money")
def send_money(req: SendMoneyRequest, db: Session = Depends(get_db)):
    return _prepare(db, kind="send-money", amount=req.amount, merchant=f"UPI {req.recipient_upi}",
                    payee=req.recipient_upi, description=f"Transfer to {req.recipient_upi}: {req.purpose}",
                    request_fields=req.model_dump())


# ── Read / expire / cancel ───────────────────────────────────────────────────

def _load_direct(db: Session, request_id: str, lock: bool = False) -> AgentTask:
    row = _load_task(db, request_id, lock=lock)
    if not _is_direct(row):
        raise HTTPException(status_code=404, detail="Payment request not found")
    return row


def _withdraw(db: Session, row: AgentTask, reason: str) -> Dict[str, Optional[str]]:
    """Take back whatever the authorization had already issued: release the
    hold (signed by the wallet key) and revoke the single-use capability (a
    system control action, like containment). Each step commits on its own;
    both are mirrored on Drunix as restrictive operations."""
    from app.services import revocation_service

    user, policy = _context(db)
    refs = row.refs or {}
    out: Dict[str, Optional[str]] = {"released": None, "revoked": None}
    wallet = db.get(Agent, _uuid(refs.get("wallet_agent"))) if refs.get("wallet_agent") else None
    res = db.get(Reservation, _uuid(refs["reservation"])) if refs.get("reservation") else None
    if res is not None and res.status == ReservationStatus.RESERVED and wallet is not None:
        try:
            signed_ops.signed_release(db, agent=wallet, reservation_id=res.id)
            audit_service.record(db, "RESERVATION_RELEASED", wallet.agent_identifier,
                                 {"reason": reason, "request": row.id, "reservation_id": str(res.id),
                                  "signed": "Ed25519 release"}, capability_id=res.capability_id,
                                 amount=res.amount, user_id=user.id)
            db.commit()
            out["released"] = _m(res.amount)
        except Exception as exc:  # recorded, never hidden
            db.rollback()
            out["release_error"] = f"{type(exc).__name__}: {exc}"
    cap = db.get(Capability, _uuid(refs["capability"])) if refs.get("capability") else None
    if cap is not None and cap.status == CapabilityStatus.ACTIVE:
        system = db.scalar(select(Agent).where(Agent.agent_identifier == "system-agent", Agent.agent_type == "root"))
        try:
            revocation_service.revoke_capability(db, cap.id, system.id)
            audit_service.record(db, "CAPABILITY_REVOKED", "agentguard",
                                 {"reason": f"single-use authority withdrawn: {reason}", "request": row.id},
                                 capability_id=cap.id, amount=cap.total_authority, user_id=user.id)
            db.commit()
            out["revoked"] = str(cap.id)
        except Exception as exc:
            db.rollback()
            out["revoke_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _expire_if_needed(db: Session, row: AgentTask) -> bool:
    if row.status != PAYABLE:
        return False
    refs = row.refs or {}
    res = db.get(Reservation, _uuid(refs["reservation"])) if refs.get("reservation") else None
    expires = (row.authorization or {}).get("expires_at")
    from datetime import datetime
    request_expired = expires is not None and datetime.fromisoformat(expires) <= _now()
    hold_expired = res is not None and res.expires_at <= _now()
    if not (request_expired or hold_expired):
        return False
    row, prev = _claim(db, row.id, (PAYABLE,), "EXPIRED")
    if prev is None:
        return False
    wound = _withdraw(db, row, "authorization window expired")
    _set_step(row, "awaiting", "failed", "Not authorized before the request expired.")
    _step(row, "expired", "Request expired", "Nothing was paid." +
          (" The hold was released and the single-use authority withdrawn." if refs.get("capability") else ""),
          status="failed")
    row.error = {"error": "ReservationExpiredError", "detail": "The payment request expired. No money moved."}
    audit_service.record(db, "AUTHORIZATION_EXPIRED", "agentguard", {"request": row.id, **wound},
                         amount=row.budget, user_id=row.user_id)
    db.commit()
    return True


@router.get("/payments/requests/{request_id}")
def get_request(request_id: str, db: Session = Depends(get_db)):
    row = _load_direct(db, request_id)
    _expire_if_needed(db, row)
    return _response(db, _load_direct(db, request_id))


@router.post("/payments/requests/{request_id}/cancel")
def cancel_request(request_id: str, db: Session = Depends(get_db)):
    user, _ = _context(db)
    _load_direct(db, request_id)
    row, prev = _claim(db, request_id, (PAYABLE,), "CANCELLED")
    if prev is None:
        return _fail(db, row, 409, "InvalidTaskState", f"Request is {row.status} and can no longer be cancelled.")
    wound = _withdraw(db, row, "cancelled by user")
    _set_step(row, "awaiting", "failed", "Cancelled before authorization.")
    _step(row, "cancelled", "Cancelled by you", "Nothing was paid." +
          (" The hold was released and the single-use authority withdrawn." if (row.refs or {}).get("capability") else
           " No authority had been issued."), status="failed")
    audit_service.record(db, "TASK_CANCELLED", user.name, {"request": row.id, "flow": "direct", **wound},
                         amount=row.budget, user_id=user.id)
    row.error = None
    db.commit()
    return _response(db, row)


# ── Authorize: issue → reserve → Drunix Reserve → pay → Drunix Commit ───────

@router.post("/payments/requests/{request_id}/authorize")
def authorize_request(request_id: str, db: Session = Depends(get_db)):
    user, policy = _context(db)
    row = _load_direct(db, request_id)
    if _expire_if_needed(db, row):
        return _fail(db, _load_direct(db, request_id), 409, "ReservationExpiredError",
                     "The payment request expired. No money moved.")
    row, prev = _claim(db, request_id, (PAYABLE,), "AUTHORIZED")
    if prev is None:
        reason = {"COMPLETED": "This payment has already been authorized and completed.",
                  "AUTHORIZED": "This payment is already being authorized.",
                  "CANCELLED": "This request was cancelled.", "EXPIRED": "This request expired."
                  }.get(row.status, f"Request is {row.status}; there is nothing to authorize.")
        return _fail(db, row, 409, "InvalidTaskState", reason)

    refs = dict(row.refs or {})
    kind = refs["payment_type"]
    t = TYPES[kind]
    merchant = refs["merchant"]
    amount = _q(row.budget)
    wallet = db.get(Agent, _uuid(refs["wallet_agent"]))
    stage = "policy"
    audit_service.record(db, "PAYMENT_AUTHORIZED", user.name,
                         {"request": row.id, "type": kind, "approval": "user", "flow": "direct"},
                         amount=amount, user_id=user.id)
    db.commit()
    try:
        # Policy may have changed since the review: re-check before issuing anything.
        if amount > _limit(policy):
            raise _PolicyChanged(f"₹{amount:,.2f} now exceeds your per-payment limit ₹{_limit(policy):,.2f}.")
        if wallet is None or wallet.status != "active":
            raise _PolicyChanged("Your wallet key is no longer active.")

        # 1. Single-use authority for exactly this amount (fail closed on Drunix)
        if not refs.get("capability"):
            stage = "register"
            now = _now()
            mandate = Mandate(id=uuid.uuid4(), user_id=user.id, name=f"{t['label']} — {merchant}"[:255],
                              purpose=row.instruction[:500], currency="INR", total_authority=amount,
                              status=MandateStatus.ACTIVE, not_before=now - timedelta(minutes=1),
                              not_after=now + MANDATE_TTL)
            db.add(mandate)
            db.flush()
            cap = capability_service.create_capability(db, CapabilityCreate(
                parent_capability_id=None, root_mandate_id=mandate.id, issued_to_agent_id=wallet.id,
                issued_by_agent_id=None, total_authority=amount, purpose=row.instruction[:500],
                category=t["category"], max_delegation_depth=0, max_fanout=0,
                not_before=mandate.not_before, not_after=mandate.not_after))
            reg_tx = _dtx(cap)
            audit_service.record(db, "CAPABILITY_ISSUED", "system-agent",
                                 {"to": wallet.agent_identifier, "request": row.id, "single_use": True,
                                  "amount": _m(amount), **({"drunix_tx": reg_tx["tx_id"]} if reg_tx else {})},
                                 capability_id=cap.id, mandate_id=mandate.id, amount=amount, user_id=user.id)
            refs.update(mandate=str(mandate.id), capability=str(cap.id))
            row.refs = refs
            _step(row, "authority", "Single-use authority issued",
                  f"Mandate + capability for exactly ₹{amount:,.2f} issued to your wallet key; it cannot be "
                  "delegated or reused." + (f" Registered on Drunix (tx {_short(reg_tx['tx_id'])}, "
                                            f"block {reg_tx.get('block_number')})." if reg_tx else ""),
                  authority=_m(amount), **({"drunix_tx": reg_tx["tx_id"], "drunix_block": reg_tx.get("block_number"),
                                            "drunix_status": "VALID"} if reg_tx else {}))
            db.commit()
        cap = db.get(Capability, _uuid(refs["capability"]))

        # 2. Signed reserve: deterministic authority checks + IsolationForest + Drunix Reserve
        if not refs.get("reservation"):
            stage = "reserve"
            reservation = signed_ops.signed_reserve(db, agent=wallet, capability=cap, amount=amount,
                                                    merchant=merchant, category=t["category"],
                                                    risk_context=_risk_context(policy))
            risk = reservation.risk_result
            reserve_tx = _dtx(reservation)
            for ev, payload in [
                ("SIGNED_RESERVE_REQUEST", {"operation": "reserve", "verified": True, "merchant": merchant,
                                            "signing": "Ed25519 signature verified (timestamp, nonce, payload hash)"}),
                ("RISK_EVALUATED", {**_risk_dict(risk), "stage": "authoritative"}),
                ("AUTHORITY_RESERVED", {"reservation_id": str(reservation.id), "merchant": merchant,
                                        "expires_at": reservation.expires_at.isoformat()}),
            ]:
                audit_service.record(db, ev, wallet.agent_identifier if ev.startswith("SIGNED") else "agentguard",
                                     {"request": row.id, "flow": "direct", **payload},
                                     capability_id=cap.id, amount=amount, user_id=user.id)
            refs["reservation"] = str(reservation.id)
            row.refs = refs
            row.authorization = {**row.authorization,
                                 "risk_authoritative": _risk_view(risk, getattr(reservation, "risk_features", None),
                                                                  "authoritative"),
                                 "reservation_status": "RESERVED",
                                 "drunix_reserve": reserve_tx,
                                 "checks": {**row.authorization["checks"], "signature": "VERIFIED",
                                            "authority": "WITHIN LIMIT", "risk": risk.action.value,
                                            "reservation": "RESERVED",
                                            **({"drunix": f"VALID · block {reserve_tx['block_number']}"}
                                               if reserve_tx else {})}}
            _step(row, "signed", "Reserve signed with your wallet key and verified",
                  "AgentGuard verified the registered key, Ed25519 signature, timestamp window, one-time nonce, "
                  "payload hash and operation binding.", signing_verified=True)
            _step(row, "risk_authoritative", "Behavioural risk evaluated",
                  f"IsolationForest {risk.risk_level.value} → {risk.action.value} (score {risk.anomaly_score:.4f}); "
                  f"consumption measured against {_risk_context(policy)['consumption_basis']}.",
                  risk_level=risk.risk_level.value, anomaly_score=risk.anomaly_score)
            _drunix_steps(row, "reserve", reserve_tx,
                          "The agentauth chaincode re-validated the hold against the on-chain authority "
                          "(mandate active, holder, amount ≤ unallocated, merchant, category, time window, idempotency).")
            _step(row, "reserved", "Amount held", f"₹{amount:,.2f} reserved on the single-use capability"
                  f"{' — in AgentGuard and on Drunix' if reserve_tx else ''}.", reservation_id=str(reservation.id))
            db.commit()
        res = db.get(Reservation, _uuid(refs["reservation"]))
        if res.expires_at <= _now():
            raise ReservationExpiredError("The hold expired before it could be paid.")

        # 3. Signed pay: Drunix Commit (fail closed) + simulated rail, one DB transaction
        stage = "commit"
        payment = signed_ops.signed_pay(db, agent=wallet, reservation_id=res.id)
        commit_tx = _dtx(res)
        for ev, actor, payload in [
            ("SIGNED_PAY_REQUEST", wallet.agent_identifier, {"operation": "pay", "verified": True,
                                                              "signing": "Ed25519 signature verified"}),
            ("RESERVATION_COMMITTED", "agentguard", {"reservation_id": str(res.id)}),
            ("PAYMENT_SUCCESS", "simulated-rail", {"type": kind, "merchant": merchant, "payment_id": str(payment.id),
                                                   "utr": payment.utr_reference, "rail": "SIMULATED", "flow": "direct",
                                                   "risk_level": (row.authorization.get("risk_authoritative") or {}).get("level")}),
        ]:
            audit_service.record(db, ev, actor, {"request": row.id, **payload},
                                 capability_id=cap.id, amount=amount, user_id=user.id)
        db.commit()
    except _PolicyChanged as exc:
        db.rollback()
        return _end(db, request_id, "FAILED", "PolicyRejection", str(exc) + " Nothing was paid.", 409, "policy")
    except HighRiskContainmentError as exc:
        db.commit()   # the single-use capability was revoked by the risk engine
        row = _load_direct(db, request_id, lock=True)
        audit_service.record(db, "HIGH_RISK_CONTAINMENT", "risk-engine",
                             {"request": request_id, "flow": "direct", **_risk_dict(exc.risk_result)},
                             capability_id=_uuid(refs.get("capability")), amount=amount, user_id=user.id)
        _step(row, "risk_authoritative", "Behavioural risk HIGH — blocked",
              f"Score {exc.risk_result.anomaly_score:.4f}. The single-use capability was revoked; nothing was held "
              f"or paid. Reasons: {'; '.join(exc.risk_result.reasons) or 'model score'}.", status="failed",
              risk_level="HIGH", anomaly_score=exc.risk_result.anomaly_score)
        return _end(db, request_id, "BLOCKED", "HighRiskContainmentError", str(exc), 403, "risk",
                    risk=exc.risk_result)
    except RiskReviewError as exc:
        db.rollback()
        audit_service.record(db, "RISK_REVIEW", "risk-engine",
                             {"request": request_id, "flow": "direct", **_risk_dict(exc.risk_result)},
                             amount=amount, user_id=user.id)
        db.commit()
        return _end(db, request_id, "REVIEW_REQUIRED", "RiskReviewError", str(exc), 409, "risk",
                    risk=exc.risk_result)
    except DrunixError as exc:
        db.rollback()
        row = _load_direct(db, request_id, lock=True)
        _drunix_failure(db, row, exc, user, {"register": "register", "reserve": "reserve"}.get(stage, "commit"))
        db.commit()
        if stage == "commit" and not isinstance(exc, (DrunixRejectedError, DrunixInvalidCommitError)):
            # Unreachable / timed out: nothing was paid and the hold is untouched;
            # the user may authorize again (the retry resumes at the commit).
            row = _load_direct(db, request_id, lock=True)
            row.status = PAYABLE
            row.authorization = {**row.authorization, "checks": {**row.authorization["checks"], "drunix": exc.category}}
            return _fail(db, row, http_status_for(exc), type(exc).__name__,
                         f"{exc} Nothing was paid; the hold is kept — authorize again to retry.")
        return _end(db, request_id, "PAYMENT_FAILED" if stage == "commit" else "FAILED", type(exc).__name__,
                    f"{exc} Drunix did not commit it VALID, so nothing was paid.", http_status_for(exc), "drunix")
    except ReservationExpiredError as exc:
        db.rollback()
        return _end(db, request_id, "EXPIRED", "ReservationExpiredError", f"{exc} Nothing was paid.", 409, "expired")
    except Exception as exc:
        db.rollback()
        name = type(exc).__name__
        if isinstance(exc, SIGNATURE_ERRORS):
            audit_service.record(db, "SIGNATURE_INVALID", "agentguard",
                                 {"request": request_id, "error": name, "detail": str(exc)}, user_id=user.id)
            db.commit()
        code = http_status_for(exc) if isinstance(exc, AgentGuardError) else 500
        return _end(db, request_id, "PAYMENT_FAILED" if stage == "commit" else "FAILED", name,
                    f"{exc} Nothing was paid.", code, "error")

    # Success
    row = _load_direct(db, request_id, lock=True)
    refs = row.refs
    row.status = "COMPLETED"
    row.error = None
    _set_step(row, "awaiting", "completed", "Authorized by you.")
    _step(row, "pay", "Pay signed with your wallet key", "Ed25519 signature bound to this reservation verified.",
          signing_verified=True)
    _drunix_steps(row, "commit", commit_tx, "The chaincode re-checked that the hold is still RESERVED, unexpired and "
                  "that the capability and mandate are still active before settling it on-chain.")
    _step(row, "paid", "Simulated payment completed",
          f"₹{payment.amount:,.2f} to {merchant} · UTR {payment.utr_reference} (simulated rail).", utr=payment.utr_reference)
    _step(row, "receipt", "Receipt generated", f"Payment {payment.id} · reference {payment.utr_reference}"
          + (f" · Drunix Reserve {_short((row.authorization.get('drunix_reserve') or {}).get('tx_id'))} and Commit "
             f"{_short(commit_tx['tx_id'])}." if commit_tx else "."))
    risk = row.authorization.get("risk_authoritative")
    row.result = {
        "payment_id": str(payment.id), "reservation_id": refs["reservation"], "capability_id": refs["capability"],
        "mandate_id": refs["mandate"], "amount": _m(payment.amount), "merchant": merchant,
        "utr_reference": payment.utr_reference, "rail": "SIMULATED", "approval": "user",
        "authorized_at": _now().isoformat(), "agent": wallet.agent_identifier,
        "signing": {"algorithm": "Ed25519", "operations_verified": ["reserve", "pay"], "agent_id": str(wallet.id),
                    "capability_id": refs["capability"]},
        "risk": risk,
        "drunix": {"mode": "enforce" if ledger_sync.enabled() else "off",
                   "reserve": row.authorization.get("drunix_reserve"), "commit": commit_tx},
    }
    db.commit()
    return _response(db, row)


class _PolicyChanged(Exception):
    pass


def _end(db: Session, request_id: str, status: str, error: str, detail: str, code: int, why: str, risk=None):
    """Terminal failure: withdraw anything issued, record, never report success."""
    row = _load_direct(db, request_id, lock=True)
    row.status = status
    db.commit()
    wound = _withdraw(db, row, detail[:120])
    row = _load_direct(db, request_id, lock=True)
    _set_step(row, "awaiting", "failed", "Authorized by you, but not completed.")
    if why not in ("risk", "drunix"):
        _step(row, why, {"policy": "Blocked by your policy", "expired": "Hold expired"}.get(why, "Payment failed"),
              detail, status="failed")
    if wound.get("released") or wound.get("revoked"):
        _step(row, "withdrawn", "Authority withdrawn",
              (f"Hold of ₹{wound['released']} released. " if wound.get("released") else "")
              + ("Single-use capability revoked." if wound.get("revoked") else ""), status="failed")
    audit_service.record(db, "PAYMENT_REJECTED", "drunix" if why == "drunix" else "agentguard",
                         {"request": request_id, "flow": "direct", "error": error, "detail": detail, **wound},
                         amount=row.budget, user_id=row.user_id)
    extra = {}
    if risk is not None:
        extra = {"risk_level": risk.risk_level.value, "anomaly_score": risk.anomaly_score, "reasons": risk.reasons}
        row.authorization = {**(row.authorization or {}), "risk_authoritative": {**_risk_dict(risk), "stage": "authoritative"}}
    return _fail(db, row, code, error, detail, **extra)


# ── Transaction history (both kinds) ─────────────────────────────────────────

STATUS_LABELS = {
    "CREATED": "Draft", "RUNNING": "In progress", PAYABLE: "Awaiting your authorization", "AUTHORIZED": "Authorizing",
    "COMPLETED": "Completed", "CANCELLED": "Cancelled", "EXPIRED": "Expired", "FAILED": "Failed",
    "PAYMENT_FAILED": "Payment failed", "REVIEW_REQUIRED": "Held for risk review", "BLOCKED": "Blocked by risk controls",
    "CONTAINED": "Contained",
}


def _summary(db: Session, row: AgentTask) -> Dict[str, Any]:
    refs, res, auth = row.refs or {}, row.result or {}, row.authorization or {}
    direct = _is_direct(row)
    if direct:
        type_label = TYPES.get(refs.get("payment_type"), {}).get("label", "Direct payment")
        merchant, amount = refs.get("merchant"), _m(row.budget)
    else:
        type_label = f"{CATEGORY_LABELS.get(row.category, row.category)} · agent purchase"
        merchant = res.get("merchant") or (refs.get("basket") or {}).get("merchant") or None
        amount = res.get("amount") or auth.get("total") or None
    return {
        "id": row.id, "kind": "direct" if direct else "agent", "type_label": type_label,
        "payment_type": refs.get("payment_type") if direct else "shopping",
        "description": row.instruction, "merchant": merchant, "amount": amount,
        "budget": None if direct else _m(row.budget), "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
        "order_number": res.get("order_number"), "utr_reference": res.get("utr_reference"),
        "hidden": row.hidden_at is not None,
        "can_repeat": row.status in FINAL, "can_retry": row.status in FINAL and row.status != "COMPLETED",
        "open_url": (f"/payments?request={row.id}" if direct else f"/agent?task={row.id}"),
    }


@router.get("/transactions")
def list_transactions(include_hidden: bool = False, kind: str = "", limit: int = 50, db: Session = Depends(get_db)):
    user, _ = _context(db)
    q = select(AgentTask).where(AgentTask.user_id == user.id).order_by(AgentTask.created_at.desc())
    if not include_hidden:
        q = q.where(AgentTask.hidden_at.is_(None))
    rows = db.scalars(q.limit(max(1, min(limit, 200)) * (2 if kind else 1))).all()
    out = [_summary(db, r) for r in rows]
    if kind in ("direct", "agent"):
        out = [x for x in out if x["kind"] == kind]
    return {"transactions": out[:max(1, min(limit, 200))],
            "note": "Hiding removes an entry from this list only; payments, orders, ledger and audit records are kept."}


def _journal(db: Session, entity_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Drunix transactions confirmed VALID (or recovered) for these entities —
    read from the journal, never synthesised."""
    rows = db.scalars(select(LedgerTransaction).where(
        LedgerTransaction.entity_id.in_([e for e in entity_ids if e]),
        LedgerTransaction.outcome.in_(["VALID", "RECOVERED", "SYNCED"])).order_by(LedgerTransaction.created_at)).all()
    pick: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        pick.setdefault(r.function, {"function": r.function, "tx_id": r.tx_id, "block_number": r.block_number,
                                     "status": r.outcome, "latency_ms": r.latency_ms, "at": r.created_at.isoformat()})
    return pick


@router.get("/transactions/{task_id}")
def transaction_detail(task_id: str, db: Session = Depends(get_db)):
    """Unified receipt for an agent purchase or a direct payment. Every field
    comes from stored records; a field that does not exist is null (the UI
    shows "Not available")."""
    row = _load_task(db, task_id)
    if _is_direct(row):
        _expire_if_needed(db, row)
        row = _load_task(db, task_id)
    refs, res, auth = row.refs or {}, row.result or {}, row.authorization or {}
    direct = _is_direct(row)
    cap_id = refs.get("capability") if direct else refs.get("purchase_cap")
    reservation_id = refs.get("reservation")
    payment = db.scalar(select(Payment).where(Payment.reservation_id == _uuid(reservation_id))) if reservation_id else None
    order = db.get(Order, _uuid(res["order_id"])) if res.get("order_id") else None
    actor = None
    agent_id = refs.get("wallet_agent") if direct else refs.get("purchase_agent")
    if agent_id:
        a = db.get(Agent, _uuid(agent_id))
        actor = {"identifier": a.agent_identifier, "role": "Your wallet key" if direct else "Purchase Agent",
                 "key_fingerprint": _fingerprint(a), "status": a.status} if a else None
    trail = _journal(db, [reservation_id, cap_id, refs.get("mandate")])
    risk = (res.get("risk") if res else None) or auth.get("risk_authoritative") or auth.get("risk")
    signed_ops_verified = (res.get("signing") or {}).get("operations_verified") or []
    checks = auth.get("checks") or {}
    policy_step = next((st for st in (row.steps or []) if st.get("key") == "policy"), None)
    policy_result = checks.get("policy") or (
        None if policy_step is None else "BLOCKED" if policy_step.get("status") == "failed" else "WITHIN LIMIT")
    body = _summary(db, row)
    body.update({
        "flow_label": "Direct payment — authorized by you" if direct else "Agent-delegated purchase",
        "payee": refs.get("payee"),
        "timestamp": (res.get("authorized_at") or row.updated_at.isoformat()),
        "payment_id": str(payment.id) if payment else res.get("payment_id"),
        "payment_status": payment.status.value.upper() if payment else None,
        "utr_reference": payment.utr_reference if payment else res.get("utr_reference"),
        "rail": "SIMULATED",
        "actor": actor,
        "signature": ("VERIFIED (" + " + ".join(signed_ops_verified) + ")") if signed_ops_verified else checks.get("signature"),
        "risk": risk,
        "policy": policy_result,
        "approval": res.get("approval") or ("user" if row.status == "COMPLETED" and direct else None),
        "capability_id": cap_id, "reservation_id": reservation_id, "mandate_id": refs.get("mandate"),
        "order": commerce_service.order_dict(order) if order else None,
        "drunix": {
            "mode": (res.get("drunix") or {}).get("mode") or ("enforce" if ledger_sync.enabled() else "off"),
            "register_mandate": trail.get("RegisterMandate"), "register_capability": trail.get("RegisterRootCapability"),
            "delegate": trail.get("Delegate"), "reserve": trail.get("Reserve"), "commit": trail.get("Commit"),
            "return_unused": trail.get("ReturnUnused"), "release": trail.get("Release"), "revoke": trail.get("Revoke"),
        },
        "error": row.error, "steps": row.steps or [],
        "request": refs.get("request") if direct else {"instruction": row.instruction, "budget": _m(row.budget)},
    })
    return body


@router.post("/transactions/{task_id}/repeat")
def repeat_transaction(task_id: str, db: Session = Depends(get_db)):
    """Start a NEW request from an earlier one. The original transaction and
    every record behind it stay exactly as they are.

    * agent purchase → a new task (CREATED) with the same instruction and the
      same cart contents, fully editable before anything is prepared;
    * direct payment → the saved form values; a new request is created only
      when the user submits the (editable) form.
    """
    user, policy = _context(db)
    row = _load_task(db, task_id)
    audit_service.record(db, "TRANSACTION_REPEATED", user.name, {"from": task_id}, user_id=user.id)
    if _is_direct(row):
        refs = row.refs or {}
        db.commit()
        return {"kind": "direct", "from": task_id, "payment_type": refs.get("payment_type"),
                "tab": TYPES[refs["payment_type"]]["tab"], "prefill": refs.get("request") or {}}
    from app.api.product import AgentTaskRequest, create_agent_task
    db.commit()
    new = create_agent_task(AgentTaskRequest(instruction=row.instruction), db)
    new_id = new["task_id"]
    copied = 0
    if row.cart_id:
        old_cart = commerce_service.get_cart(db, row.cart_id)
        new_row = _load_task(db, new_id)
        cart = commerce_service.get_cart(db, new_row.cart_id, lock=True)
        for item in list(old_cart.items):
            try:
                commerce_service.set_quantity(db, cart, item.product_id, item.quantity)
                copied += 1
            except Exception:
                pass
        _step(new_row, "repeat", "Started from an earlier purchase",
              f"Instruction and {copied} cart line(s) copied from task {task_id}. Edit anything before checkout; "
              "the earlier purchase is unchanged.")
        db.commit()
    return {"kind": "agent", "from": task_id, "task_id": new_id, "copied_lines": copied}


def _set_hidden(db: Session, task_id: str, hidden: bool):
    user, _ = _context(db)
    row = _load_task(db, task_id, lock=True)
    row.hidden_at = _now() if hidden else None
    audit_service.record(db, "ACTIVITY_HIDDEN" if hidden else "ACTIVITY_UNHIDDEN", user.name,
                         {"transaction": task_id, "note": "presentation only; no record was deleted"},
                         user_id=user.id)
    db.commit()
    return _summary(db, row)


@router.post("/transactions/{task_id}/hide")
def hide_transaction(task_id: str, db: Session = Depends(get_db)):
    return _set_hidden(db, task_id, True)


@router.post("/transactions/{task_id}/unhide")
def unhide_transaction(task_id: str, db: Session = Depends(get_db)):
    return _set_hidden(db, task_id, False)
