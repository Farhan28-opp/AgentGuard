"""Train the Day 5 Risk Engine model (IsolationForest) on synthetic normal data.

This script demonstrates deterministic model training for the Behavioural AI MVP.
It produces `ml_models/isolation_forest.joblib` and `ml_models/model_metadata.json`
which are treated as trusted artifacts by the application.
"""
import json
import logging
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest

# Ensure imports work from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.feature_extraction import FEATURE_NAMES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# The artifact destination directory
MODEL_DIR = Path(__file__).resolve().parent.parent / "ml_models"


def generate_synthetic_normal_data(n_samples: int = 1000) -> np.ndarray:
    """Generate synthetic feature vectors representing normal behavior.
    
    Order must match FEATURE_NAMES:
    0 transaction_velocity_1h
    1 amount_z_score
    2 new_merchant_ratio
    3 authority_consumption_rate
    4 delegation_rate
    5 time_deviation
    """
    # Deterministic generation
    rng = np.random.RandomState(42)
    
    # 1. velocity: typically 1 to 4 transactions
    velocity = rng.poisson(lam=2.5, size=n_samples)
    velocity = np.clip(velocity, 1, 5).astype(float)
    
    # 2. z_score: normally distributed around 0.0, tightly bounded
    z_score = rng.normal(loc=0.0, scale=0.5, size=n_samples)
    
    # 3. new_merchant_ratio: mostly 0.0 or low (agents reuse merchants)
    # 80% 0.0, 20% small random
    is_new = rng.binomial(n=1, p=0.2, size=n_samples)
    new_merchant = is_new * rng.uniform(0.1, 0.3, size=n_samples)
    
    # 4. consumption_rate: typically 5% to 50%
    consumption = rng.uniform(0.05, 0.50, size=n_samples)
    
    # 5. delegation_rate: typically 0
    delegation = rng.binomial(n=1, p=0.05, size=n_samples).astype(float)
    
    # 6. time_deviation: typically 0.0 (during hours)
    time_dev = rng.binomial(n=1, p=0.05, size=n_samples).astype(float)
    
    X = np.column_stack([
        velocity,
        z_score,
        new_merchant,
        consumption,
        delegation,
        time_dev,
    ])
    return X


def main():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    
    logger.info("Generating synthetic normal data...")
    X_train = generate_synthetic_normal_data(n_samples=2000)
    
    logger.info("Training IsolationForest model (random_state=42)...")
    model = IsolationForest(
        n_estimators=100,
        max_samples="auto",
        contamination=0.05,
        max_features=1.0,
        bootstrap=False,
        n_jobs=1,
        random_state=42,
    )
    
    model.fit(X_train)
    
    # Save the model
    model_path = MODEL_DIR / "isolation_forest.joblib"
    joblib.dump(model, model_path)
    logger.info(f"Model saved to {model_path}")
    
    # Save metadata
    meta_path = MODEL_DIR / "model_metadata.json"
    metadata = {
        "training_timestamp": datetime.now(timezone.utc).isoformat(),
        "feature_names": FEATURE_NAMES,
        "python_version": platform.python_version(),
        "scikit_learn_version": sklearn.__version__,
        "joblib_version": joblib.__version__,
        "training_sample_count": len(X_train),
        "model_configuration": {
            "n_estimators": model.n_estimators,
            "contamination": model.contamination,
            "random_state": model.random_state,
        },
    }
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)
    logger.info(f"Metadata saved to {meta_path}")


if __name__ == "__main__":
    main()
