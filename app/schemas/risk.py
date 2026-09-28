"""Risk assessment schemas for Day 5 Behavioural AI layer.

These structures describe the output of the risk evaluation pipeline.
The ML model produces an anomaly_score; the explanation engine produces
reason_codes and human-readable reasons; the decision service maps
the result to an action.
"""
import enum
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskAction(str, enum.Enum):
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    CONTAIN = "CONTAIN"


# Canonical reason codes — deterministic, derived from feature values.
class ReasonCode(str, enum.Enum):
    HIGH_TRANSACTION_VELOCITY = "HIGH_TRANSACTION_VELOCITY"
    AMOUNT_DEVIATION = "AMOUNT_DEVIATION"
    NEW_MERCHANT = "NEW_MERCHANT"
    AUTHORITY_CONSUMPTION_BURST = "AUTHORITY_CONSUMPTION_BURST"
    DELEGATION_BURST = "DELEGATION_BURST"
    UNUSUAL_TIME = "UNUSUAL_TIME"


class RiskResult(BaseModel):
    risk_level: RiskLevel
    action: RiskAction
    anomaly_score: float
    reason_codes: List[ReasonCode] = []
    reasons: List[str] = []
    capability_id: Optional[str] = None
