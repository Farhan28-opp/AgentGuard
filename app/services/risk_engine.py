"""Day 5 Risk Engine — IsolationForest anomaly scoring + rule-based explanation.

Architecture
~~~~~~~~~~~~

    Feature Extraction
            ↓
    IsolationForest   →  raw_score  (decision_function)
            ↓
    Score Conversion  →  anomaly_score  (higher = more anomalous)
            ↓
    Risk Classification  →  LOW / MEDIUM / HIGH  →  ALLOW / REVIEW / CONTAIN
            ↓
    Explanation Engine  →  reason_codes + human-readable reasons

IsolationForest's ``decision_function`` returns values where:
  * **positive** → normal (inlier)
  * **negative** → anomalous (outlier)
  * magnitude indicates degree

We convert to ``anomaly_score = -1 × decision_function`` so that
**higher anomaly_score = more anomalous**, which is the representation
used throughout the rest of the system.

Thresholds (configurable via ``app.config.settings``):
  * anomaly_score < MEDIUM_THRESHOLD → LOW  → ALLOW
  * MEDIUM_THRESHOLD ≤ anomaly_score < HIGH_THRESHOLD → MEDIUM → REVIEW
  * anomaly_score ≥ HIGH_THRESHOLD → HIGH → CONTAIN
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.schemas.risk import (
    ReasonCode,
    RiskAction,
    RiskLevel,
    RiskResult,
)
from app.services.feature_extraction import FEATURE_NAMES, features_to_array

logger = logging.getLogger(__name__)

# Default model path relative to project root
_DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "ml_models"
_MODEL_FILE = "isolation_forest.joblib"
_META_FILE = "model_metadata.json"


class RiskEngine:
    """Singleton-style risk evaluator.

    Loads a pre-trained IsolationForest from a joblib artifact and uses a
    rule-based explanation engine to annotate feature deviations.
    """

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        medium_threshold: float = 0.0,
        high_threshold: float = 0.15,
    ):
        self._model = None
        self._metadata: Optional[dict] = None
        self._model_dir = model_dir or _DEFAULT_MODEL_DIR
        self.medium_threshold = medium_threshold
        self.high_threshold = high_threshold
        self._load_model()

    # ── Model loading ────────────────────────────────────────────────

    def _load_model(self) -> None:
        model_path = self._model_dir / _MODEL_FILE
        meta_path = self._model_dir / _META_FILE

        if not model_path.exists():
            logger.warning(
                "Risk model not found at %s — cold-start fallback active "
                "(all requests will be classified LOW/ALLOW).",
                model_path,
            )
            return

        import joblib
        self._model = joblib.load(model_path)

        if meta_path.exists():
            with open(meta_path) as f:
                self._metadata = json.load(f)
            # Sanity-check feature order
            expected = self._metadata.get("feature_names", [])
            if expected != FEATURE_NAMES:
                logger.warning(
                    "Model feature order %s differs from runtime order %s",
                    expected,
                    FEATURE_NAMES,
                )

        logger.info("Risk model loaded from %s", model_path)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    # ── Scoring ──────────────────────────────────────────────────────

    def _raw_score(self, X: np.ndarray) -> float:
        """Return the IsolationForest decision_function value.

        decision_function semantics:
          positive → inlier (normal)
          negative → outlier (anomalous)
        """
        return float(self._model.decision_function(X)[0])

    @staticmethod
    def _to_anomaly_score(raw: float) -> float:
        """Convert raw decision_function value to anomaly_score.

        anomaly_score = -1 × raw

        Result: higher anomaly_score = more anomalous.
        """
        return -1.0 * raw

    # ── Classification ───────────────────────────────────────────────

    def _classify(self, anomaly_score: float) -> Tuple[RiskLevel, RiskAction]:
        if anomaly_score >= self.high_threshold:
            return RiskLevel.HIGH, RiskAction.CONTAIN
        elif anomaly_score >= self.medium_threshold:
            return RiskLevel.MEDIUM, RiskAction.REVIEW
        else:
            return RiskLevel.LOW, RiskAction.ALLOW

    # ── Explanation Engine ───────────────────────────────────────────

    @staticmethod
    def _explain(features: Dict[str, float], context: Optional[Dict[str, str]] = None) -> Tuple[List[ReasonCode], List[str]]:
        """Derive reason_codes and human-readable reasons from feature values.

        This is entirely rule-based and does NOT depend on the ML model.
        ``context`` only changes the wording (e.g. which authority pool the
        consumption rate was measured against), never the decision.
        """
        context = context or {}
        codes: List[ReasonCode] = []
        reasons: List[str] = []

        # Transaction velocity
        v = features.get("transaction_velocity_1h", 0)
        if v > 5:
            codes.append(ReasonCode.HIGH_TRANSACTION_VELOCITY)
            reasons.append(
                f"Transaction velocity is {v:.0f} transactions/hour "
                f"(threshold: 5)"
            )

        # Amount deviation
        z = features.get("amount_z_score", 0)
        if abs(z) > 2.0:
            codes.append(ReasonCode.AMOUNT_DEVIATION)
            reasons.append(
                f"Transaction amount is {z:.1f} standard deviations "
                f"from the historical mean"
            )

        # New merchant
        nm = features.get("new_merchant_ratio", 0)
        if nm > 0.5:
            codes.append(ReasonCode.NEW_MERCHANT)
            reasons.append(
                f"{nm*100:.0f}% of merchants in this session are previously unseen"
            )

        # Authority consumption
        ac = features.get("authority_consumption_rate", 0)
        if ac > 0.8:
            codes.append(ReasonCode.AUTHORITY_CONSUMPTION_BURST)
            basis = context.get("consumption_basis")
            reasons.append(
                f"{ac*100:.0f}% of {basis} would be used by this payment" if basis else
                f"{ac*100:.0f}% of total authority would be consumed after this request"
            )

        # Delegation burst
        dr = features.get("delegation_rate", 0)
        if dr > 3:
            codes.append(ReasonCode.DELEGATION_BURST)
            reasons.append(
                f"{dr:.0f} child capabilities created in the last hour"
            )

        # Time deviation
        td = features.get("time_deviation", 0)
        if td > 0.5:
            codes.append(ReasonCode.UNUSUAL_TIME)
            from app.services.feature_extraction import normal_hours_label
            reasons.append(
                "Transaction time falls outside normal operating hours "
                f"({normal_hours_label()})"
            )

        return codes, reasons

    # ── Public API ───────────────────────────────────────────────────

    def evaluate(
        self,
        features: Dict[str, float],
        capability_id: Optional[str] = None,
        context: Optional[Dict[str, str]] = None,
    ) -> RiskResult:
        """Full risk evaluation pipeline.

        1. Convert features → numpy array
        2. IsolationForest → raw score → anomaly_score
        3. Classify → risk_level + action
        4. Explain → reason_codes + reasons
        """
        # Cold-start fallback: no model → always LOW/ALLOW
        if not self.is_loaded:
            return RiskResult(
                risk_level=RiskLevel.LOW,
                action=RiskAction.ALLOW,
                anomaly_score=0.0,
                reason_codes=[],
                reasons=["Risk model not loaded — cold-start fallback active"],
                capability_id=capability_id,
            )

        X = features_to_array(features)
        raw = self._raw_score(X)
        anomaly_score = self._to_anomaly_score(raw)
        risk_level, action = self._classify(anomaly_score)
        reason_codes, reasons = self._explain(features, context)

        return RiskResult(
            risk_level=risk_level,
            action=action,
            anomaly_score=round(anomaly_score, 4),
            reason_codes=reason_codes,
            reasons=reasons,
            capability_id=capability_id,
        )


# ── Module-level singleton ───────────────────────────────────────────
# Loaded lazily on first import so tests can override.
_engine: Optional[RiskEngine] = None


def get_risk_engine() -> RiskEngine:
    global _engine
    if _engine is None:
        from app.config import settings
        _engine = RiskEngine(
            medium_threshold=getattr(settings, "risk_medium_threshold", 0.0),
            high_threshold=getattr(settings, "risk_high_threshold", 0.15),
        )
    return _engine


def set_risk_engine(engine: RiskEngine) -> None:
    """Replace the module-level singleton (used by tests)."""
    global _engine
    _engine = engine
