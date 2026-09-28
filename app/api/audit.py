import uuid
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog

router = APIRouter(tags=["audit"])


@router.get("/capabilities/{capability_id}/audit-trail")
def get_audit_trail(capability_id: uuid.UUID, db: Session = Depends(get_db)) -> List[dict]:
    """Returns whatever audit records currently exist for this capability.
    Nothing writes to audit_logs yet on Day 1 (no events are logged), and
    hash-chain verification is a Day 4 feature -- this endpoint exists so
    the response shape is stable once both land.
    """
    entries = db.scalars(
        select(AuditLog)
        .where(AuditLog.capability_id == capability_id)
        .order_by(AuditLog.created_at)
    ).all()
    return [
        {
            "id": e.id,
            "capability_id": e.capability_id,
            "mandate_id": e.mandate_id,
            "event_type": e.event_type,
            "actor": e.actor,
            "amount": e.amount,
            "payload": e.payload,
            "previous_hash": e.previous_hash,
            "event_hash": e.event_hash,
            "created_at": e.created_at,
        }
        for e in entries
    ]
