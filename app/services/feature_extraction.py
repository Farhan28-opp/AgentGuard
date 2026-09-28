"""Feature extraction for Day 5 Behavioural Risk Engine.

Computes a compact feature vector from an agent's recent reservation
history (across every capability the agent holds) and the *current* (projected) request.  Every feature that
represents a count or ratio includes the current request so the risk
engine sees the state *as it would be if the request were allowed*.

Feature vector (ordered):
    0  transaction_velocity_1h   — reservations in last hour + 1 (current)
    1  amount_z_score            — (current_amount − mean) / std of historical amounts
    2  new_merchant_ratio        — fraction of unique merchants (incl. current) that are new
    3  authority_consumption_rate — (committed + reserved + current) / authority pool
                                   (the capability's total authority; for a
                                   single-purpose capability sized to one
                                   payment, the user's per-payment limit —
                                   see ``authority_reference`` below)
    4  delegation_rate           — children created in last hour
    5  time_deviation            — 1.0 if outside the configured normal-hours
                                   window in the configured local timezone
                                   (default 06:00-23:00 Asia/Kolkata), else 0.0
"""
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional, Dict, Any

import numpy as np
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models.capability import Capability
from app.models.reservation import Reservation, ReservationStatus
from app.schemas.reservation import ReserveRequest


# Feature names in canonical order — must match training script
FEATURE_NAMES: List[str] = [
    "transaction_velocity_1h",
    "amount_z_score",
    "new_merchant_ratio",
    "authority_consumption_rate",
    "delegation_rate",
    "time_deviation",
]


def extract_features(
    db: Session,
    capability: Capability,
    request: ReserveRequest,
    now: Optional[datetime] = None,
    authority_reference: Optional[Decimal] = None,
) -> Dict[str, float]:
    """Return a dict of feature_name → value for the current request,
    projected to include the current request in every applicable metric.

    ``authority_reference`` (server-side only, never taken from a request):
    the authority pool against which consumption is measured when the
    capability itself is a *single-purpose* grant sized to exactly this
    payment (a user-authorized direct payment). Such a grant is 100 %
    consumed by construction, which says nothing about behaviour; measuring
    it against the user's per-payment limit gives the same meaning the
    feature has for an agent's Purchase capability (which is sized to that
    limit). It can only widen the denominator (max with the capability's own
    total), so it never hides a capability being drained.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    one_hour_ago = now - timedelta(hours=1)

    # ── historical reservations for this AGENT ───────────────────────
    # Behaviour belongs to the agent, not to one grant: an agent that holds a
    # fresh capability per task still carries its spending history across
    # tasks. (For an agent with a single capability this is identical to the
    # per-capability history.)
    agent_caps = select(Capability.id).where(
        Capability.issued_to_agent_id == capability.issued_to_agent_id)
    hist_query = (
        select(Reservation)
        .where(Reservation.capability_id.in_(agent_caps))
        .where(Reservation.status.in_([
            ReservationStatus.RESERVED,
            ReservationStatus.COMMITTED,
        ]))
    )
    all_reservations: list = db.scalars(hist_query).all()

    recent_reservations = [
        r for r in all_reservations if r.created_at >= one_hour_ago
    ]

    # ── 1. Transaction Velocity (projected: +1 for this request) ─────
    velocity_1h = float(len(recent_reservations) + 1)

    # ── 2. Amount Z-Score ────────────────────────────────────────────
    historical_amounts = [float(r.amount) for r in all_reservations]
    current_amount = float(request.amount)
    if len(historical_amounts) >= 2:
        mean_amt = float(np.mean(historical_amounts))
        std_amt = float(np.std(historical_amounts, ddof=1))
        if std_amt > 0:
            amount_z = (current_amount - mean_amt) / std_amt
        else:
            amount_z = 0.0
    elif len(historical_amounts) == 1:
        # Only one historical point — can't compute std, use ratio
        amount_z = (current_amount / historical_amounts[0]) - 1.0 if historical_amounts[0] > 0 else 0.0
    else:
        # Cold start — no history, no deviation
        amount_z = 0.0

    # ── 3. New-Merchant Ratio (projected) ────────────────────────────
    known_merchants = set(
        r.merchant for r in all_reservations if r.merchant
    )
    current_merchant = request.merchant
    projected_merchants = known_merchants | {current_merchant}
    new_merchants = projected_merchants - known_merchants
    if len(projected_merchants) > 0:
        new_merchant_ratio = float(len(new_merchants)) / float(len(projected_merchants))
    else:
        new_merchant_ratio = 0.0

    # ── 4. Authority Consumption Rate (projected) ────────────────────
    total = float(capability.total_authority)
    if authority_reference is not None and Decimal(authority_reference) > capability.total_authority:
        total = float(authority_reference)
    if total > 0:
        already_consumed = float(
            capability.committed_authority + capability.reserved_authority
        )
        projected_consumed = already_consumed + current_amount
        consumption_rate = projected_consumed / total
    else:
        consumption_rate = 1.0

    # ── 5. Delegation Rate (children created in last hour) ───────────
    child_count = db.scalar(
        select(func.count(Capability.id))
        .where(Capability.parent_capability_id == capability.id)
        .where(Capability.created_at >= one_hour_ago)
    ) or 0
    delegation_rate = float(child_count)

    # ── 6. Time Deviation ────────────────────────────────────────────
    time_deviation = 0.0 if is_normal_hour(request.transaction_time) else 1.0

    return {
        "transaction_velocity_1h": velocity_1h,
        "amount_z_score": amount_z,
        "new_merchant_ratio": new_merchant_ratio,
        "authority_consumption_rate": consumption_rate,
        "delegation_rate": delegation_rate,
        "time_deviation": time_deviation,
    }


def _risk_tz():
    from zoneinfo import ZoneInfo
    from app.config import settings
    return ZoneInfo(settings.risk_timezone)


def is_normal_hour(ts: datetime) -> bool:
    """True if ``ts`` falls inside the configured normal-hours window,
    evaluated in the configured local timezone (naive datetimes are UTC)."""
    from app.config import settings
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    hour = ts.astimezone(_risk_tz()).hour
    return settings.risk_normal_hours_start <= hour < settings.risk_normal_hours_end


def normal_hours_label() -> str:
    from app.config import settings
    return (f"{settings.risk_normal_hours_start:02d}:00-"
            f"{settings.risk_normal_hours_end:02d}:00 {settings.risk_timezone}")


def features_to_array(features: Dict[str, float]) -> np.ndarray:
    """Convert the feature dict to a numpy array in canonical order."""
    return np.array([[features[name] for name in FEATURE_NAMES]])
