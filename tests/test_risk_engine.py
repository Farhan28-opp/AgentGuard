from pathlib import Path
from app.schemas.risk import RiskLevel, RiskAction, ReasonCode
from app.services.risk_engine import RiskEngine

MODEL_DIR = Path(__file__).resolve().parent.parent / "ml_models"


def test_risk_engine_normal():
    """Verify that a normal feature vector is classified as LOW/ALLOW."""
    engine = RiskEngine(model_dir=MODEL_DIR)
    
    # Feature vector matching typical normal data:
    # 0 transaction_velocity_1h = 2.0
    # 1 amount_z_score = 0.5
    # 2 new_merchant_ratio = 0.0
    # 3 authority_consumption_rate = 0.10
    # 4 delegation_rate = 0.0
    # 5 time_deviation = 0.0
    features = {
        "transaction_velocity_1h": 2.0,
        "amount_z_score": 0.5,
        "new_merchant_ratio": 0.0,
        "authority_consumption_rate": 0.10,
        "delegation_rate": 0.0,
        "time_deviation": 0.0,
    }
    
    result = engine.evaluate(features, capability_id="test")
    assert result.risk_level == RiskLevel.LOW
    assert result.action == RiskAction.ALLOW
    assert result.anomaly_score < engine.medium_threshold
    assert len(result.reason_codes) == 0


def test_risk_engine_high_risk():
    """Verify that an extreme feature vector is classified as HIGH/CONTAIN
    and generates the correct explanation codes."""
    engine = RiskEngine(model_dir=MODEL_DIR, high_threshold=0.05)
    
    features = {
        "transaction_velocity_1h": 25.0,  # Extreme
        "amount_z_score": 5.5,            # Extreme
        "new_merchant_ratio": 1.0,        # New merchant
        "authority_consumption_rate": 0.95, # Extreme
        "delegation_rate": 0.0,
        "time_deviation": 1.0,            # Out of hours
    }
    
    result = engine.evaluate(features, capability_id="test")
    assert result.risk_level == RiskLevel.HIGH
    assert result.action == RiskAction.CONTAIN
    assert result.anomaly_score >= engine.high_threshold
    
    # Check explanations
    assert ReasonCode.HIGH_TRANSACTION_VELOCITY in result.reason_codes
    assert ReasonCode.AMOUNT_DEVIATION in result.reason_codes
    assert ReasonCode.NEW_MERCHANT in result.reason_codes
    assert ReasonCode.AUTHORITY_CONSUMPTION_BURST in result.reason_codes
    assert ReasonCode.UNUSUAL_TIME in result.reason_codes


def test_risk_engine_cold_start():
    """Verify that missing model falls back to LOW risk gracefully."""
    # Point to a fake directory
    engine = RiskEngine(model_dir=Path("/does/not/exist"))
    
    features = {
        "transaction_velocity_1h": 50.0, # Would normally trigger HIGH
    }
    
    result = engine.evaluate(features, capability_id="test")
    assert result.risk_level == RiskLevel.LOW
    assert result.action == RiskAction.ALLOW
    assert result.anomaly_score == 0.0
    assert "Risk model not loaded" in result.reasons[0]
