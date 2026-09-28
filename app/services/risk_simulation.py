"""Behavioural Risk Simulation — a CONTROLLED security demonstration.

This is not real customer history. To show the behavioural layer
deterministically, we inject a burst of synthetic holds on an agent's
capability — the pattern a compromised or malfunctioning agent would
produce — and then let that agent send ONE genuine signed request through
the real pipeline:

    valid Ed25519 identity  → signature verified
    valid financial authority → deterministic checks pass
    behaviour deviates (velocity burst + amount jump + new merchant)
        → REAL IsolationForest → HIGH → CONTAIN
        → recursive capability revocation (existing revocation service)
        → active holds (synthetic and real) released
        → a follow-up request is blocked by the ledger

Hard policy violations (e.g. asking for more than the capability holds)
never reach the model — they are rejected deterministically first. The
model is only consulted for requests that are already within authority.

Synthetic holds are real ``reservations`` rows (status RESERVED) whose
amounts move unallocated → reserved, so the conservation invariant holds
throughout. They are tagged with ``SIMULATION_MERCHANT`` and an
``sim-burst-`` idempotency prefix and are released either by containment
or, if the model does not contain, by explicit cleanup.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.exceptions import (
    AgentGuardError,
    HighRiskContainmentError,
    RiskReviewError,
)
from app.models.agent import Agent
from app.models.capability import Capability
from app.models.reservation import Reservation, ReservationStatus
from app.services import audit_service, reservation_service, signed_ops

SIMULATION_MERCHANT = "SIM Rapid-Fire Kiosk"
SIMULATION_KEY_PREFIX = "sim-burst-"
BURST_SIZE = 20


def _q(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _risk_payload(risk) -> Dict[str, Any]:
    if risk is None:
        return {}
    return {
        "level": risk.risk_level.value,
        "action": risk.action.value,
        "anomaly_score": risk.anomaly_score,
        "reason_codes": [rc.value for rc in risk.reason_codes],
        "reasons": risk.reasons,
    }


def _sim_merchant(cap: Capability, db: Session = None) -> str:
    """Synthetic holds respect the capability's own scope: with a merchant
    allowlist they target the merchant the agent is already using (its
    pending hold), else the first allowed merchant."""
    if not cap.merchant_allowlist:
        return SIMULATION_MERCHANT
    if db is not None:
        current = db.scalar(select(Reservation.merchant).where(
            Reservation.capability_id == cap.id, Reservation.status == ReservationStatus.RESERVED,
            ~Reservation.idempotency_key.startswith(SIMULATION_KEY_PREFIX)).limit(1))
        if current in cap.merchant_allowlist:
            return current
    return cap.merchant_allowlist[0]


def _unfamiliar_allowed_merchant(db: Session, cap: Capability) -> str:
    """An allowed merchant this agent has never used (falls back to any
    allowed merchant). Staying inside the allowlist keeps the request within
    authority, so only the behavioural layer can object to it."""
    known = {m for (m,) in db.execute(select(Reservation.merchant).join(
        Capability, Capability.id == Reservation.capability_id).where(
        Capability.issued_to_agent_id == cap.issued_to_agent_id)).all()}
    for m in cap.merchant_allowlist:
        if m not in known:
            return m
    return cap.merchant_allowlist[-1]


def seed_burst(db: Session, capability: Capability, now: datetime) -> List[Reservation]:
    """Inject BURST_SIZE synthetic RESERVED micro-holds created over the last
    ~13 minutes (each ~0.6% of the unallocated authority, ₹1-₹12)."""
    cap = db.get(Capability, capability.id, with_for_update=True, populate_existing=True)
    merchant = _sim_merchant(cap, db)
    per_hold = _q(max(Decimal("1"), min(Decimal("10"), cap.unallocated_authority * Decimal("0.006"))))
    holds = []
    for i in range(BURST_SIZE):
        amount = per_hold + Decimal(i % 3)  # small variation, e.g. 20, 21, 22
        if cap.unallocated_authority - amount < Decimal("1"):
            break
        cap.unallocated_authority -= amount
        cap.reserved_authority += amount
        res = Reservation(
            id=uuid.uuid4(),
            capability_id=cap.id,
            amount=amount,
            currency="INR",
            merchant=merchant,
            category=cap.category,
            status=ReservationStatus.RESERVED,
            idempotency_key=f"{SIMULATION_KEY_PREFIX}{uuid.uuid4()}",
            created_at=now - timedelta(seconds=40 * (BURST_SIZE - i)),
            expires_at=now + timedelta(seconds=settings.reservation_ttl_seconds),
        )
        db.add(res)
        holds.append(res)
    db.flush()
    return holds


def _release_quietly(db: Session, reservation_ids: List[uuid.UUID]) -> None:
    for rid in reservation_ids:
        res = db.get(Reservation, rid)
        if res is not None and res.status == ReservationStatus.RESERVED:
            reservation_service.release_reservation(db, rid)


def run_behavioural_simulation(
    db: Session,
    *,
    agent: Agent,
    capability: Capability,
    context: Optional[Dict[str, Any]] = None,
    user_id=None,
) -> Dict[str, Any]:
    context = dict(context or {})
    now = datetime.now(timezone.utc)

    # Holds that were already active before the simulation (e.g. a real
    # payment awaiting authorization) — containment must release them too.
    pre_existing = [r.id for r in db.scalars(
        select(Reservation).where(
            Reservation.capability_id == capability.id,
            Reservation.status == ReservationStatus.RESERVED,
        )
    ).all()]

    burst_merchant = _sim_merchant(capability, db)
    holds = seed_burst(db, capability, now)
    seeded_total = sum((h.amount for h in holds), Decimal("0"))
    audit_service.record(
        db, "BEHAVIOUR_SIMULATION_SEEDED", "security-simulation",
        {**context, "simulation": True,
         "label": "Controlled Security Simulation — synthetic holds, not customer history",
         "holds": len(holds), "total": f"{seeded_total:.2f}", "merchant": burst_merchant,
         "agent": agent.agent_identifier},
        capability_id=capability.id, amount=seeded_total, user_id=user_id,
    )
    db.commit()
    seeded_ids = [h.id for h in holds]

    db.refresh(capability)
    # Pattern: a burst of micro-transactions followed by one large request to
    # an unfamiliar merchant using most of the remaining authority (the
    # classic "card testing, then cash-out" shape). Still within authority.
    live_amount = _q(capability.unallocated_authority * Decimal("0.9"))
    live_amount = max(live_amount, Decimal("1.00"))
    # The live request must stay within authority — including the merchant
    # allowlist — otherwise the ledger rejects it before the model runs.
    live_merchant = (_unfamiliar_allowed_merchant(db, capability) if capability.merchant_allowlist
                     else f"Unfamiliar Merchant {uuid.uuid4().hex[:4].upper()}")

    outcome: Dict[str, Any] = {
        "label": "Controlled Security Simulation",
        "simulated_history": {
            "holds": len(holds), "total": f"{seeded_total:.2f}", "merchant": burst_merchant,
            "note": "Synthetic burst injected for a deterministic demo — not real customer history.",
        },
        "live_request": {"amount": f"{live_amount:.2f}", "merchant": live_merchant,
                         "agent": agent.agent_identifier, "capability_id": str(capability.id)},
        "checks": {"signature": None, "authority": None, "behaviour": None},
    }

    try:
        reservation = signed_ops.signed_reserve(
            db, agent=agent, capability=capability, amount=live_amount,
            merchant=live_merchant, category=capability.category,
        )
    except HighRiskContainmentError as exc:
        db.commit()  # persist the revocation staged by reserve_authority
        risk = exc.risk_result
        released_ids = seeded_ids + pre_existing
        db.expire_all()
        released = [db.get(Reservation, rid) for rid in released_ids]
        released = [r for r in released if r is not None and r.status == ReservationStatus.RELEASED]
        cap_after = db.get(Capability, capability.id)
        subtree_revoked = cap_after.status.value
        containment = {
            "action": "recursive capability revocation",
            "capability_status": subtree_revoked.upper(),
            "released_reservations": len(released),
            "released_amount": f"{sum((r.amount for r in released), Decimal('0')):.2f}",
            "reserved_after": f"{cap_after.reserved_authority:.2f}",
        }
        audit_service.record(
            db, "HIGH_RISK_CONTAINMENT", "risk-engine",
            {**context, "simulation": True, **_risk_payload(risk),
             "signature": "Ed25519 verified before risk check",
             "authority": "within capability limits"},
            capability_id=capability.id, amount=live_amount,
        )
        audit_service.record(
            db, "CAPABILITY_REVOKED", "agentguard",
            {**context, "simulation": True, "reason": "HIGH behavioural risk containment",
             **containment},
            capability_id=capability.id, user_id=user_id,
        )
        # Agent-level containment: suspend the identity, revoke its other grants.
        from app.services.containment_service import contain_agent
        agent_containment = contain_agent(db, agent, reason="HIGH behavioural risk (controlled simulation)",
                                          user_id=user_id)
        containment["agent"] = agent_containment
        db.commit()

        # Future transactions are blocked (suspended key + revoked capability).
        blocked: Dict[str, Any]
        try:
            signed_ops.signed_reserve(db, agent=agent, capability=capability,
                                      amount=Decimal("1.00"), merchant=live_merchant,
                                      category=capability.category)
            db.rollback()
            blocked = {"blocked": False, "error": None}
        except AgentGuardError as follow_exc:
            db.rollback()
            blocked = {"blocked": True, "error": type(follow_exc).__name__,
                       "detail": str(follow_exc)}
            audit_service.record(
                db, "POST_CONTAINMENT_BLOCKED", "agentguard",
                {**context, "simulation": True, **blocked, "agent": agent.agent_identifier},
                capability_id=capability.id,
            )
            db.commit()

        outcome.update({
            "status": "contained",
            "risk": _risk_payload(risk),
            "containment": containment,
            "follow_up_request": blocked,
            "checks": {"signature": "verified", "authority": "within limits",
                       "behaviour": "anomalous"},
        })
        return outcome

    except RiskReviewError as exc:
        db.rollback()
        _release_quietly(db, seeded_ids)
        db.commit()
        outcome.update({"status": "review", "risk": _risk_payload(exc.risk_result),
                        "checks": {"signature": "verified", "authority": "within limits",
                                   "behaviour": "suspicious (MEDIUM)"}})
        return outcome

    # Model allowed it: undo the simulation — nothing should remain held.
    risk = getattr(reservation, "risk_result", None)
    db.commit()
    _release_quietly(db, seeded_ids + [reservation.id])
    db.commit()
    outcome.update({"status": "not_contained", "risk": _risk_payload(risk),
                    "checks": {"signature": "verified", "authority": "within limits",
                               "behaviour": "not flagged"}})
    return outcome
