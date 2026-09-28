from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import select, func, or_
from typing import List, Dict, Any

from app.database import get_db
from app.models.mandate import Mandate, MandateStatus
from app.models.agent import Agent
from app.models.capability import Capability, CapabilityStatus
from app.models.reservation import Reservation, ReservationStatus
from app.models.nonce import RequestNonce
from app.models.payment import Payment
from app.models.audit_log import AuditLog

router = APIRouter(tags=["dashboard"])

@router.get("/dashboard/summary")
def get_dashboard_summary(db: Session = Depends(get_db)) -> Dict[str, Any]:
    active_mandates = db.scalar(select(func.count(Mandate.id)).where(Mandate.status == MandateStatus.ACTIVE))
    active_agents = db.scalar(select(func.count(Agent.id)).where(Agent.status == "active"))
    active_capabilities = db.scalar(select(func.count(Capability.id)).where(Capability.status == CapabilityStatus.ACTIVE))
    revoked_capabilities = db.scalar(select(func.count(Capability.id)).where(Capability.status == CapabilityStatus.REVOKED))
    active_reservations = db.scalar(select(func.count(Reservation.id)).where(Reservation.status == ReservationStatus.RESERVED))

    reserved_authority = db.scalar(select(func.sum(Capability.reserved_authority))) or 0
    committed_authority = db.scalar(select(func.sum(Capability.committed_authority))) or 0
    
    risk_events = db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type.in_(["HIGH_RISK_CONTAINMENT", "RISK_REVIEW"])))
    contained_agents = db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type == "HIGH_RISK_CONTAINMENT"))
    
    return {
        "active_mandates": active_mandates,
        "active_agents": active_agents,
        "active_capabilities": active_capabilities,
        "active_reservations": active_reservations,
        "revoked_capabilities": revoked_capabilities,
        "reserved_authority": str(reserved_authority),
        "committed_authority": str(committed_authority),
        "risk_events": risk_events,
        "containments": contained_agents,
    }

@router.get("/dashboard/capabilities-tree")
def get_capability_tree(db: Session = Depends(get_db), limit: int = 60) -> List[Dict[str, Any]]:
    """Capabilities belonging to the most recent mandates (whole trees)."""
    # Always include delegation trees (mandates with child capabilities, e.g.
    # the standing agent authority and the Security Lab), plus the most recent
    # mandates (e.g. single-grant direct payments).
    recent = [r[0] for r in db.execute(select(Mandate.id).order_by(Mandate.created_at.desc())
                                       .limit(max(1, min(limit, 500)))).all()]
    trees = [r[0] for r in db.execute(select(Capability.root_mandate_id).where(
        Capability.parent_capability_id.isnot(None)).distinct()).all()]
    capabilities = db.scalars(
        select(Capability)
        .where(Capability.root_mandate_id.in_(set(recent) | set(trees)))
        .order_by(Capability.created_at.desc(), Capability.delegation_depth)
    ).all()
    agents = {a.id: a for a in db.scalars(select(Agent)).all()}
    system_agent = next((a for a in agents.values() if a.agent_identifier == "system-agent"), None)
    from app.security.capability_signing import verify_capability_grant

    def grant_valid(c: Capability):
        # Re-verified on every read against the issuer's registered public key.
        issuer = agents.get(c.issued_by_agent_id) if c.issued_by_agent_id else system_agent
        if not c.grant_signature or issuer is None:
            return False
        try:
            verify_capability_grant(c, issuer)
            return True
        except Exception:
            return False

    def format_cap(c: Capability):
        issued_to = agents.get(c.issued_to_agent_id)
        issued_by = agents.get(c.issued_by_agent_id)
        return {
            "id": str(c.id),
            "parent_id": str(c.parent_capability_id) if c.parent_capability_id else None,
            "root_mandate_id": str(c.root_mandate_id),
            "agent_identifier": issued_to.agent_identifier if issued_to else "unknown",
            "issuer_identifier": issued_by.agent_identifier if issued_by else "system-agent",
            "status": c.status.value.upper() if hasattr(c.status, 'value') else str(c.status).upper(),
            "total_authority": str(c.total_authority),
            "unallocated_authority": str(c.unallocated_authority),
            "reserved_authority": str(c.reserved_authority),
            "committed_authority": str(c.committed_authority),
            "category": c.category,
            "delegation_depth": c.delegation_depth,
            "grant_signature": c.grant_signature,
            "grant_signature_valid": grant_valid(c),
            "not_before": c.not_before.isoformat(),
            "not_after": c.not_after.isoformat()
        }
    
    return [format_cap(c) for c in capabilities]

@router.get("/events")
def get_global_events(db: Session = Depends(get_db), limit: int = 50) -> List[Dict[str, Any]]:
    events = db.scalars(
        select(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ).all()
    
    return [
        {
            "id": str(e.id),
            "timestamp": e.created_at.isoformat(),
            "event_type": e.event_type,
            "actor": e.actor,
            "amount": str(e.amount) if e.amount else None,
            "payload": e.payload,
            "capability_id": str(e.capability_id) if e.capability_id else None
        }
        for e in events
    ]

@router.get("/dashboard/agents")
def get_agents(db: Session = Depends(get_db), limit: int = 40) -> List[Dict[str, Any]]:
    agents = db.scalars(select(Agent).order_by(Agent.created_at.desc()).limit(limit)).all()
    
    result = []
    for a in agents:
        pk_fingerprint = "N/A"
        if a.public_key:
            import hashlib
            pk_fingerprint = hashlib.sha256(a.public_key.encode()).hexdigest()[:8]
            
        result.append({
            "id": str(a.id),
            "identifier": a.agent_identifier,
            "type": a.agent_type,
            "status": a.status,
            "public_key_fingerprint": pk_fingerprint,
            "created_at": a.created_at.isoformat()
        })
    return result

@router.get("/dashboard/payments")
def get_payments(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    payments = db.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(50)).all()
    return [
        {
            "id": str(p.id),
            "reservation_id": str(p.reservation_id),
            "amount": str(p.amount),
            "merchant": p.merchant,
            "status": p.status.value.upper() if hasattr(p.status, "value") else str(p.status),
            "rail": "SIMULATED",
            "utr_reference": p.utr_reference,
            "created_at": p.created_at.isoformat()
        }
        for p in payments
    ]


@router.get("/dashboard/reservations")
def get_reservations(db: Session = Depends(get_db), limit: int = 40) -> List[Dict[str, Any]]:
    rows = db.scalars(select(Reservation).order_by(Reservation.created_at.desc()).limit(limit)).all()
    return [
        {
            "id": str(r.id),
            "capability_id": str(r.capability_id),
            "amount": str(r.amount),
            "merchant": r.merchant,
            "status": r.status.value.upper(),
            "simulation": bool(r.idempotency_key and r.idempotency_key.startswith("sim-burst-")),
            "expires_at": r.expires_at.isoformat(),
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/dashboard/risk")
def get_risk_events(db: Session = Depends(get_db), limit: int = 15) -> Dict[str, Any]:
    from app.services.risk_engine import get_risk_engine
    from app.services.feature_extraction import normal_hours_label

    engine = get_risk_engine()
    events = db.scalars(
        select(AuditLog)
        .where(AuditLog.event_type.in_(["RISK_EVALUATED", "RISK_REVIEW", "HIGH_RISK_CONTAINMENT"]))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "model": "IsolationForest" if engine.is_loaded else "not loaded (cold-start fallback)",
        "thresholds": {"medium": engine.medium_threshold, "high": engine.high_threshold},
        "normal_hours": normal_hours_label(),
        "recent": [
            {
                "timestamp": e.created_at.isoformat(),
                "event_type": e.event_type,
                "level": (e.payload or {}).get("level"),
                "anomaly_score": (e.payload or {}).get("anomaly_score"),
                "reasons": (e.payload or {}).get("reasons", []),
                "simulation": bool((e.payload or {}).get("simulation")),
                "amount": str(e.amount) if e.amount is not None else None,
            }
            for e in events
        ],
    }


@router.get("/dashboard/crypto")
def get_crypto_status(db: Session = Depends(get_db), sample: int = 25) -> Dict[str, Any]:
    """Cryptographic verification status, computed from stored data.

    * verified signed requests = consumed anti-replay nonces (a nonce is only
      stored after the Ed25519 signature and payload hash verified);
    * rejected = SIGNATURE_INVALID audit events;
    * grant signatures = re-verified now for the most recent capabilities
      against the issuer's registered public key.
    """
    from app.security.capability_signing import verify_capability_grant

    by_op = dict(db.execute(
        select(RequestNonce.operation, func.count()).group_by(RequestNonce.operation)
    ).all())
    rejected = db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type == "SIGNATURE_INVALID"))
    agents_with_keys = db.scalar(select(func.count(Agent.id)).where(Agent.public_key.isnot(None)))
    agents_total = db.scalar(select(func.count(Agent.id)))

    caps = db.scalars(select(Capability).order_by(Capability.created_at.desc()).limit(sample)).all()
    system_agent = db.scalar(select(Agent).where(Agent.agent_identifier == "system-agent"))
    ok = bad = unsigned = 0
    for c in caps:
        if not c.grant_signature:
            unsigned += 1
            continue
        issuer = db.get(Agent, c.issued_by_agent_id) if c.issued_by_agent_id else system_agent
        try:
            verify_capability_grant(c, issuer)
            ok += 1
        except Exception:
            bad += 1
    return {
        "algorithm": "Ed25519",
        "verified_signed_requests": {k: int(v) for k, v in by_op.items()},
        "verified_signed_requests_total": int(sum(by_op.values())),
        "rejected_signed_requests": int(rejected or 0),
        "agents_with_registered_keys": int(agents_with_keys or 0),
        "agents_total": int(agents_total or 0),
        "grant_signatures_checked": len(caps),
        "grant_signatures_valid": ok,
        "grant_signatures_invalid": bad,
        "grant_signatures_missing": unsigned,
    }
