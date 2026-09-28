"""Audit event recording shared by the product and demo surfaces.

``record()`` stages an ``AuditLog`` row in the caller's transaction
(add + flush, no commit). That keeps an audit event atomic with the state
change it describes: if the transaction rolls back, the event disappears
with it and the feed never claims something happened that did not.

Events describing a *failure* are recorded after the failed transaction
has been rolled back, and committed on their own.
"""
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


def record(
    db: Session,
    event_type: str,
    actor: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    capability_id: Optional[uuid.UUID] = None,
    mandate_id: Optional[uuid.UUID] = None,
    amount: Optional[Decimal] = None,
    user_id: Optional[uuid.UUID] = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user_id,
        capability_id=capability_id,
        mandate_id=mandate_id,
        event_type=event_type,
        actor=actor,
        amount=amount,
        payload=payload or {},
    )
    db.add(entry)
    db.flush()
    return entry
