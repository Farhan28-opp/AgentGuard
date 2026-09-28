import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.capability import CapabilityStatus
from app.models.reservation import Reservation, ReservationStatus
from app.models.mandate import Mandate, MandateStatus
from app.models.user import User
from app.schemas.reservation import ReserveRequest
from app.services.feature_extraction import extract_features

from tests.conftest import make_agent, create_root_capability, create_child_capability

def make_mandate(db: Session):
    user = User(id=uuid.uuid4(), name="Test User")
    db.add(user)
    db.commit()
    now = datetime.now(timezone.utc)
    mandate = Mandate(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Test Mandate",
        purpose="testing",
        currency="INR",
        total_authority=Decimal("10000"),
        status=MandateStatus.ACTIVE,
        not_before=now,
        not_after=now + timedelta(days=7),
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    return mandate


def test_feature_extraction(db: Session):
    """Verify that feature extraction calculates moving averages, counts,
    and velocity correctly, including the projected current request."""
    
    mandate = make_mandate(db)
    agent = make_agent(db, str(uuid.uuid4()), "purchase")
    cap = create_root_capability(db, mandate, agent, total_authority=Decimal("10000"))
    
    now = datetime.now(timezone.utc)
    
    # Create 3 historical reservations in the last hour
    r1 = Reservation(
        id=uuid.uuid4(),
        capability_id=cap.id,
        amount=Decimal("100"),
        currency="INR",
        merchant="M1",
        category="C1",
        status=ReservationStatus.COMMITTED,
        expires_at=now + timedelta(hours=1),
        created_at=now - timedelta(minutes=10),
    )
    r2 = Reservation(
        id=uuid.uuid4(),
        capability_id=cap.id,
        amount=Decimal("150"),
        currency="INR",
        merchant="M1",
        category="C1",
        status=ReservationStatus.RESERVED,
        expires_at=now + timedelta(hours=1),
        created_at=now - timedelta(minutes=5),
    )
    r3 = Reservation(
        id=uuid.uuid4(),
        capability_id=cap.id,
        amount=Decimal("200"),
        currency="INR",
        merchant="M2",
        category="C1",
        status=ReservationStatus.COMMITTED,
        expires_at=now + timedelta(hours=1),
        created_at=now - timedelta(minutes=2),
    )
    
    db.add_all([r1, r2, r3])
    db.commit()
    
    # Cap authority: unallocated=10000. Wait, create_root sets unallocated=total.
    # The reservations above manually inserted didn't debit the cap, but feature extraction
    # only reads the Reservation table for amounts, and uses cap.committed/reserved for consumption.
    # Let's adjust cap balances so they are consistent.
    cap.unallocated_authority = Decimal("9550")
    cap.committed_authority = Decimal("300")
    cap.reserved_authority = Decimal("150")
    db.commit()

    # The current request (projected)
    request = ReserveRequest(
        agent_id=agent.id,
        amount=Decimal("400"),  # Mean of hist=(100, 150, 200)=150. Std=50. Z-score = (400-150)/50 = 5.0
        currency="INR",
        merchant="M3",  # M1, M2 known. M3 is new. Total projected merchants = 3. New = 1. Ratio = 1/3
        category="C1",
        transaction_time=now,
        idempotency_key="tx1",
    )
    
    features = extract_features(db, cap, request, now=now)
    
    assert features["transaction_velocity_1h"] == 4.0  # 3 historical + 1 current
    
    # amount_z_score = (400 - 150) / 50 = 5.0
    assert features["amount_z_score"] == 5.0
    
    # new_merchant_ratio = 1 / 3
    assert abs(features["new_merchant_ratio"] - (1.0 / 3.0)) < 0.001
    
    # authority_consumption_rate: (300 + 150 + 400) / 10000 = 850 / 10000 = 0.085
    assert abs(features["authority_consumption_rate"] - 0.085) < 0.001
    
    # delegation_rate: 0 children
    assert features["delegation_rate"] == 0.0
    
    # time_deviation: depends on `now.hour`. Let's mock the request time to force it.
    request_out_of_hours = request.model_copy(update={"transaction_time": now.replace(hour=22)})
    features_out = extract_features(db, cap, request_out_of_hours, now=now)
    assert features_out["time_deviation"] == 1.0

    request_in_hours = request.model_copy(update={"transaction_time": now.replace(hour=10)})
    features_in = extract_features(db, cap, request_in_hours, now=now)
    assert features_in["time_deviation"] == 0.0
