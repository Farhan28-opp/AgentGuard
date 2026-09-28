"""Security Center demo scenarios ("Security Lab").

Enabled only when DEMO_MODE is true. All lab data belongs to the demo user
(users.is_demo) and is removed by the scoped reset — nothing else is touched.

Every scenario runs the REAL pipeline — real Ed25519 signatures verified by
``verify_signed_request``, the real deterministic authority checks and the
real IsolationForest. Nothing here swaps in a mocked risk engine or writes
an audit event for something that did not happen.

  POST /demo/reset                delete demo-user data only, restore catalogue,
                                  re-provision one clean demo user + standing agents
  POST /demo/initialize           Security Lab tree under the demo user: mandate ₹10,000 →
                                  lab-shopping(10,000) → lab-search(0) / lab-negotiation(2,000) /
                                  lab-purchase(6,000)
  POST /demo/normal-payment       signed reserve ₹650 → risk → signed pay → commit
  POST /demo/policy-violation     signed reserve above the Purchase Agent's authority →
                                  rejected by the ledger (hard rule, model never consulted)
  POST /demo/concurrent-race      two signed reserves by the Negotiation Agent for 60% of
                                  its authority each, concurrently → exactly one wins
                                  (row lock); the winning hold is then released
  POST /demo/behavioural-anomaly  Controlled Behavioural Risk Simulation → HIGH → contain
  POST /demo/tamper-request       signed request altered after signing → rejected
"""
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, engine, get_db
from app.exceptions import AgentGuardError, HighRiskContainmentError
from app.models.agent import Agent
from app.models.capability import Capability
from app.models.mandate import Mandate, MandateStatus
from app.models.user import User
from app.schemas.capability import CapabilityCreate
from app.services import audit_service, capability_service, signed_ops
from app.services.identity_service import ensure_system_agent, provision_local_agent
from app.services.risk_simulation import run_behavioural_simulation

router = APIRouter(tags=["demo"], prefix="/demo")

_DEMO_AGENTS = [
    ("lab-shopping-agent", "root"),
    ("lab-search-agent", "search"),
    ("lab-negotiation-agent", "negotiation"),
    ("lab-purchase-agent", "purchase"),
]


def _require_demo_mode():
    from app.config import settings
    if not settings.demo_mode:
        raise HTTPException(status_code=403, detail="Demo endpoints are disabled (DEMO_MODE=false).")


router.dependencies.append(Depends(_require_demo_mode))


def _purchase(db: Session):
    agent = db.scalar(select(Agent).where(Agent.agent_identifier == "lab-purchase-agent"))
    if agent is None:
        raise HTTPException(status_code=400, detail="Initialize the Security Lab first.")
    cap = db.scalar(select(Capability).where(Capability.issued_to_agent_id == agent.id))
    if cap is None:
        raise HTTPException(status_code=400, detail="Purchase capability not found.")
    return agent, cap


@router.post("/reset")
def reset_demo(db: Session = Depends(get_db)):
    """Scoped demo reset: deletes ONLY rows belonging to demo users (and the
    agents they own), restores catalogue prices/stock and re-provisions one
    clean demo user with its standing Main/Search/Negotiation/Purchase agents.
    Schema, Alembic state and any non-demo data are untouched."""
    from app.services import policy_service
    counts = policy_service.reset_demo_data(db)
    user = policy_service.demo_user(db)
    audit_service.record(db, "DEMO_RESET", "agentguard", {"deleted": counts}, user_id=user.id)
    db.commit()
    return {"status": "reset_successful", "deleted": counts}


@router.post("/initialize")
def initialize_demo(db: Session = Depends(get_db)):
    from app.services import policy_service
    if db.scalar(select(Agent).where(Agent.agent_identifier == "lab-purchase-agent")):
        raise HTTPException(status_code=409, detail="Security Lab already initialized — reset first.")
    ensure_system_agent(db)
    user = policy_service.demo_user(db)
    now = datetime.now(timezone.utc)
    mandate = Mandate(user_id=user.id, name="Demo Mandate", purpose="Shopping", currency="INR",
                      total_authority=Decimal("10000"), status=MandateStatus.ACTIVE,
                      not_before=now - timedelta(minutes=1), not_after=now + timedelta(days=7))
    db.add(mandate)
    db.flush()
    audit_service.record(db, "MANDATE_CREATED", "Demo User", {"amount": "10000.00"},
                         mandate_id=mandate.id, amount=Decimal("10000"))

    agents = {}
    for identifier, agent_type in _DEMO_AGENTS:
        agents[identifier] = provision_local_agent(db, identifier, agent_type, owner_user_id=user.id)
        audit_service.record(db, "AGENT_CREATED", "agentguard",
                             {"agent": identifier, "key": "Ed25519", "lab": True}, user_id=user.id)

    def grant(parent, to, amount, purpose):
        cap = capability_service.create_capability(db, CapabilityCreate(
            parent_capability_id=parent.id if parent else None, root_mandate_id=mandate.id,
            issued_to_agent_id=agents[to].id,
            issued_by_agent_id=agents["lab-shopping-agent"].id if parent else None,
            total_authority=Decimal(amount), purpose=purpose, category="shopping",
            max_delegation_depth=3 if parent is None else 2, max_fanout=5,
            not_before=mandate.not_before if parent is None else parent.not_before,
            not_after=mandate.not_after if parent is None else parent.not_after))
        audit_service.record(db, "CAPABILITY_ISSUED" if parent is None else "CAPABILITY_DELEGATED",
                             "system-agent" if parent is None else "lab-shopping-agent",
                             {"to": to, "amount": f"{Decimal(amount):.2f}"},
                             capability_id=cap.id, mandate_id=mandate.id, amount=Decimal(amount))
        return cap

    root = grant(None, "lab-shopping-agent", "10000", "Security Lab root")
    grant(root, "lab-search-agent", "0", "Search Only")
    grant(root, "lab-negotiation-agent", "2000", "Negotiation")
    purchase = grant(root, "lab-purchase-agent", "6000", "Purchase execution")
    db.commit()
    return {"status": "initialized", "root_cap": str(root.id), "purchase_cap": str(purchase.id)}


@router.post("/normal-payment")
def normal_payment(db: Session = Depends(get_db)):
    """Signed reserve ₹650 → real risk engine → signed pay → commit."""
    agent, cap = _purchase(db)
    amount = Decimal("650")
    try:
        res = signed_ops.signed_reserve(db, agent=agent, capability=cap, amount=amount,
                                        merchant="NormalShop", category=cap.category)
    except HighRiskContainmentError:
        db.commit()
        raise
    except AgentGuardError:
        db.rollback()
        raise
    risk = res.risk_result
    audit_service.record(db, "SIGNED_RESERVE_REQUEST", "lab-purchase-agent",
                         {"operation": "reserve", "verified": True, "merchant": "NormalShop"},
                         capability_id=cap.id, amount=amount)
    audit_service.record(db, "RISK_EVALUATED", "risk-engine",
                         {"level": risk.risk_level.value, "anomaly_score": risk.anomaly_score,
                          "reasons": risk.reasons}, capability_id=cap.id, amount=amount)
    audit_service.record(db, "AUTHORITY_RESERVED", "agentguard",
                         {"reservation_id": str(res.id)}, capability_id=cap.id, amount=amount)
    payment = signed_ops.signed_pay(db, agent=agent, reservation_id=res.id)
    audit_service.record(db, "SIGNED_PAY_REQUEST", "lab-purchase-agent",
                         {"operation": "pay", "verified": True}, capability_id=cap.id, amount=amount)
    audit_service.record(db, "RESERVATION_COMMITTED", "agentguard",
                         {"reservation_id": str(res.id)}, capability_id=cap.id, amount=amount)
    audit_service.record(db, "PAYMENT_SUCCESS", "simulated-rail",
                         {"payment_id": str(payment.id), "utr": payment.utr_reference,
                          "rail": "SIMULATED"}, capability_id=cap.id, amount=amount)
    db.commit()
    return {"status": "success", "reservation_id": str(res.id), "payment_id": str(payment.id),
            "utr_reference": payment.utr_reference,
            "risk": {"level": risk.risk_level.value, "anomaly_score": risk.anomaly_score}}


@router.post("/policy-violation")
def policy_violation(db: Session = Depends(get_db)):
    """Hard policy violation: a validly signed request for more than the
    Purchase Agent holds. Rejected deterministically by the ledger before
    the behavioural model is consulted."""
    agent, cap = _purchase(db)
    amount = (cap.unallocated_authority + Decimal("5000")).quantize(Decimal("0.01"))
    try:
        signed_ops.signed_reserve(db, agent=agent, capability=cap, amount=amount,
                                  merchant="NormalShop", category=cap.category)
        db.rollback()
        return {"status": "unexpected_allow"}
    except AgentGuardError as exc:
        db.rollback()
        audit_service.record(db, "POLICY_VIOLATION_REJECTED", "agentguard",
                             {"requested": f"{amount:.2f}",
                              "available": f"{cap.unallocated_authority:.2f}",
                              "error": type(exc).__name__, "detail": str(exc),
                              "layer": "deterministic authority ledger (ML not consulted)"},
                             capability_id=cap.id, amount=amount)
        db.commit()
        return {"status": "rejected", "error": type(exc).__name__, "detail": str(exc),
                "requested": f"{amount:.2f}", "available": f"{cap.unallocated_authority:.2f}",
                "layer": "deterministic authority ledger"}


@router.post("/behavioural-anomaly")
def behavioural_anomaly(db: Session = Depends(get_db)):
    """Controlled Behavioural Risk Simulation on the demo Purchase Agent."""
    agent, cap = _purchase(db)
    if cap.status.value != "active":
        raise HTTPException(status_code=409, detail="Purchase Agent is already contained.")
    from app.services import policy_service
    outcome = run_behavioural_simulation(db, agent=agent, capability=cap,
                                         context={"scenario": "security-lab"},
                                         user_id=policy_service.demo_user(db).id)
    return outcome


@router.post("/tamper-request")
def tamper_request(db: Session = Depends(get_db)):
    """Two real tampering attempts against the verifier:
    1. payload altered after signing (amount ₹100 → ₹99,999)
    2. signature bytes altered (forged signature)
    """
    agent, cap = _purchase(db)
    results = []

    def alter_payload(env, payload):
        payload["amount"] = "99999.00"

    def forge_signature(env, payload):
        sig = env.signature
        env.signature = ("A" if sig[0] != "A" else "B") + sig[1:]

    for name, hook in [("payload_altered_after_signing", alter_payload),
                       ("forged_signature", forge_signature)]:
        try:
            signed_ops.signed_reserve(db, agent=agent, capability=cap, amount=Decimal("100"),
                                      merchant="NormalShop", category=cap.category, tamper=hook)
            db.rollback()
            results.append({"attempt": name, "status": "accepted"})
        except AgentGuardError as exc:
            db.rollback()
            audit_service.record(db, "SIGNATURE_INVALID", "agentguard",
                                 {"attempt": name, "error": type(exc).__name__, "detail": str(exc)},
                                 capability_id=cap.id)
            db.commit()
            results.append({"attempt": name, "status": "rejected",
                            "error": type(exc).__name__, "detail": str(exc)})
    ok = all(r["status"] == "rejected" for r in results)
    return {"status": "rejected" if ok else "unexpected_accept", "attempts": results}


@router.post("/concurrent-race")
def concurrent_race():
    """Two concurrent signed reserves, each for 60% of the Negotiation Agent's
    unallocated authority. The capability row lock serialises them; exactly
    one wins, the other fails the deterministic authority check. The winning
    hold is released afterwards (signed release) so no authority is stranded.

    Uses the Negotiation Agent (fresh, no spending history) so the race
    exercises the lock rather than the behavioural model.
    """
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        agent = db.scalar(select(Agent).where(Agent.agent_identifier == "lab-negotiation-agent"))
        if agent is None:
            raise HTTPException(status_code=400, detail="Initialize the demo first.")
        cap = db.scalar(select(Capability).where(Capability.issued_to_agent_id == agent.id))
        if cap is None or cap.unallocated_authority <= 0 or cap.status.value != "active":
            raise HTTPException(status_code=400, detail="No active unallocated authority left for the race.")
        amount = (cap.unallocated_authority * Decimal("0.6")).quantize(Decimal("0.01"))
        cap_id, agent_id, category = cap.id, agent.id, cap.category
    finally:
        db.close()

    results = []
    barrier = threading.Barrier(2)

    def worker(worker_id: int):
        session = SessionLocal()
        try:
            a = session.get(Agent, agent_id)
            c = session.get(Capability, cap_id)
            barrier.wait(timeout=5)
            res = signed_ops.signed_reserve(session, agent=a, capability=c, amount=amount,
                                            merchant="RaceShop", category=category)
            audit_service.record(session, "RACE_WINNER", f"Worker-{worker_id}",
                                 {"reservation_id": str(res.id)}, capability_id=cap_id, amount=amount)
            session.commit()
            results.append({"worker": worker_id, "status": "success", "reservation_id": str(res.id)})
        except Exception as exc:
            session.rollback()
            audit_service.record(session, "RACE_LOSER", f"Worker-{worker_id}",
                                 {"error": type(exc).__name__}, capability_id=cap_id, amount=amount)
            session.commit()
            results.append({"worker": worker_id, "status": "failed", "error": type(exc).__name__})
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    released = []
    db = SessionLocal()
    try:
        a = db.get(Agent, agent_id)
        for r in results:
            if r["status"] == "success":
                signed_ops.signed_release(db, agent=a, reservation_id=uuid.UUID(r["reservation_id"]))
                audit_service.record(db, "RESERVATION_RELEASED", "lab-negotiation-agent",
                                     {"reason": "race demo cleanup", "reservation_id": r["reservation_id"]},
                                     capability_id=cap_id, amount=amount)
                released.append(r["reservation_id"])
        db.commit()
    finally:
        db.close()
    return {"status": "race_completed", "agent": "lab-negotiation-agent", "results": results,
            "attempted_amount": f"{amount:.2f}", "released_after_race": released}
