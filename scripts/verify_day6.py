"""Verification script for Day 6 Capability Grant Signatures."""

import os
import sys
import uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta

# Ensure local imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import SessionLocal
from sqlalchemy import select

from app.models.agent import Agent
from app.models.capability import Capability, CapabilityStatus
from app.models.mandate import Mandate, MandateStatus
from app.models.user import User
from app.security.capability_signing import verify_capability_grant
from app.exceptions import InvalidSignatureError
from tests.conftest import make_agent_with_key
from app.services.capability_service import create_root_capability, create_capability
from app.schemas.capability import CapabilityCreate
from app.schemas.reservation import ReserveRequest
from app.services import reservation_service

from app.schemas.risk import RiskLevel, RiskAction, RiskResult
from app.services import risk_engine

class MockRiskEngine:
    def evaluate(self, features, capability_id=None):
        return RiskResult(
            risk_level=RiskLevel.LOW,
            action=RiskAction.ALLOW,
            anomaly_score=0.01,
            reason_codes=[],
            reasons=[],
            capability_id=capability_id,
        )

def verify():
    db = SessionLocal()
    risk_engine.set_risk_engine(MockRiskEngine())
    print("Starting PostgreSQL Verification for Day 6 Capability Grant Signatures...")

    try:
        # 1. Create a user and mandate
        user = User(id=uuid.uuid4(), name="Verify Day 6")
        db.add(user)
        db.commit()

        now = datetime.now(timezone.utc)
        mandate = Mandate(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Verify Mandate",
            purpose="verify",
            currency="INR",
            total_authority=Decimal("1000"),
            status=MandateStatus.ACTIVE,
            not_before=now,
            not_after=now + timedelta(days=7),
        )
        db.add(mandate)
        db.commit()

        # 2. Ensure agents exist
        system_agent = db.scalar(select(Agent).where(Agent.agent_identifier == "system-agent"))
        if not system_agent:
            system_agent, _ = make_agent_with_key(db, "system-agent", "root")

        root_agent, root_priv = make_agent_with_key(db, f"root-{uuid.uuid4()}", "root")
        child_agent, _ = make_agent_with_key(db, f"child-{uuid.uuid4()}", "purchase")

        from tests.conftest import create_root_capability
        
        # 3. Create Root Capability
        root_cap = create_root_capability(
            db=db,
            mandate=mandate,
            agent=root_agent,
            total_authority=Decimal("1000")
        )
        print(f"✓ Root capability created. grant_signature exists: {bool(root_cap.grant_signature)}")

        # 4. Create Child Capability
        data = CapabilityCreate(
            parent_capability_id=root_cap.id,
            root_mandate_id=mandate.id,
            issued_to_agent_id=child_agent.id,
            issued_by_agent_id=root_agent.id,
            total_authority=Decimal("500"),
            purpose="child grant",
            category="groceries",
            max_delegation_depth=1,
            max_fanout=5,
            not_before=mandate.not_before,
            not_after=mandate.not_after,
        )
        from app.services.capability_service import create_capability
        child_cap = create_capability(db, data)
        print(f"✓ Child capability created. grant_signature exists: {bool(child_cap.grant_signature)}")

        # Query real postgres table
        res = db.execute(select(Capability.grant_signature).where(Capability.id == child_cap.id)).scalar()
        print(f"✓ Querying postgres explicitly: grant_signature is NOT NULL: {res is not None}")

        # Verify signature cryptographically
        verify_capability_grant(child_cap, root_agent)
        print("✓ Cryptographic verification successful.")

        # Tamper amount
        original_amount = child_cap.total_authority
        child_cap.total_authority = Decimal("999")
        try:
            verify_capability_grant(child_cap, root_agent)
            print("❌ Verification failed to catch amount tampering!")
            sys.exit(1)
        except InvalidSignatureError:
            print("✓ Amount tampering correctly rejected.")
        
        # Restore amount
        child_cap.total_authority = original_amount
        verify_capability_grant(child_cap, root_agent)
        print("✓ Amount restored, signature valid again.")

        # Mutable state operations
        req = ReserveRequest(
            agent_id=child_agent.id,
            amount=Decimal("100"),
            currency="INR",
            merchant="Test",
            category="groceries",
            transaction_time=datetime.now(timezone.utc),
            idempotency_key=str(uuid.uuid4())
        )
        reservation = reservation_service.reserve_authority(db, child_cap.id, req)
        db.commit()
        db.refresh(child_cap)

        verify_capability_grant(child_cap, root_agent)
        print(f"✓ Signature valid after RESERVE. Unallocated: {child_cap.unallocated_authority}, Reserved: {child_cap.reserved_authority}")

        reservation_service.commit_reservation(db, reservation.id)
        db.commit()
        db.refresh(child_cap)

        verify_capability_grant(child_cap, root_agent)
        print(f"✓ Signature valid after COMMIT. Reserved: {child_cap.reserved_authority}, Committed: {child_cap.committed_authority}")

        from app.services.revocation_service import revoke_capability
        revoke_capability(db, child_cap.id, root_agent.id)
        db.commit()
        db.refresh(child_cap)

        verify_capability_grant(child_cap, root_agent)
        print(f"✓ Signature valid after REVOKE. Status: {child_cap.status}")

        try:
            verify_capability_grant(child_cap, child_agent)
            print("❌ Verification failed to catch wrong issuer!")
            sys.exit(1)
        except InvalidSignatureError:
            print("✓ Wrong issuer verification correctly rejected.")

        print("\nAll Day 6 Capability Grant Signature requirements met successfully.")

    finally:
        db.close()


if __name__ == "__main__":
    verify()
