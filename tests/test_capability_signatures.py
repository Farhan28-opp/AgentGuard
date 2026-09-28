"""Tests for Capability Grant Signatures / Attestation (Day 6).

Verifies that the grant_signature correctly authenticates the immutable
authorization definition of a capability, and correctly rejects tampering
with immutable fields while ignoring changes to mutable ledger state.
"""
import uuid
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import pytest

from sqlalchemy import select

from app.database import SessionLocal
from app.models.agent import Agent
from app.models.capability import Capability, CapabilityStatus
from app.security.capability_signing import verify_capability_grant
from app.exceptions import InvalidSignatureError
from app.schemas.reservation import ReserveRequest
from app.services import reservation_service
from app.services.capability_service import get_capability

from tests.conftest import (
    make_agent_with_key,
    create_root_capability,
    create_child_capability,
    seed_user,
    seed_mandate,
)


def _setup_tree(db):
    try:
        from app.models.user import User
        from app.models.mandate import Mandate, MandateStatus
        
        user = User(id=uuid.uuid4(), name="Signature Test")
        db.add(user)
        db.commit()
        
        mandate = Mandate(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Signature Mandate",
            purpose="testing",
            currency="INR",
            total_authority=Decimal("10000"),
            status=MandateStatus.ACTIVE,
            not_before=datetime.now(timezone.utc),
            not_after=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(mandate)
        db.commit()

        # system-agent is created in conftest if it doesn't exist
        root_agent, _ = make_agent_with_key(db, f"root-{uuid.uuid4()}", "root")
        child_agent, _ = make_agent_with_key(db, f"child-{uuid.uuid4()}", "purchase")

        root_cap = create_root_capability(db, mandate, root_agent, total_authority=Decimal("5000"))
        
        # Child cap is issued by root_agent
        child_cap = create_child_capability(db, root_cap, child_agent, total_authority=Decimal("2000"))
        
        return root_agent, child_agent, root_cap, child_cap
    except Exception:
        raise


class TestCapabilityGrantSignatures:
    
    def test_valid_grant_signature(self, db):
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        system_agent = db.scalar(select(Agent).where(Agent.agent_identifier == "system-agent"))
        
        # Verify root capability (signed by system-agent)
        assert root_cap.grant_signature is not None
        verify_capability_grant(root_cap, system_agent)
        
        # Verify child capability (signed by root_agent)
        assert child_cap.grant_signature is not None
        verify_capability_grant(child_cap, root_agent)

    def test_amount_tampering(self, db):
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        child_cap.total_authority = Decimal("9999")
        with pytest.raises(InvalidSignatureError):
            verify_capability_grant(child_cap, root_agent)

    def test_issued_to_tampering(self, db):
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        child_cap.issued_to_agent_id = root_agent.id
        with pytest.raises(InvalidSignatureError):
            verify_capability_grant(child_cap, root_agent)

    def test_parent_tampering(self, db):
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        child_cap.parent_capability_id = None
        with pytest.raises(InvalidSignatureError):
            verify_capability_grant(child_cap, root_agent)

    def test_scope_tampering(self, db):
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        child_cap.category = "everything"
        with pytest.raises(InvalidSignatureError):
            verify_capability_grant(child_cap, root_agent)

    def test_expiry_tampering(self, db):
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        child_cap.not_after = child_cap.not_after + timedelta(days=365)
        with pytest.raises(InvalidSignatureError):
            verify_capability_grant(child_cap, root_agent)

    def test_issuer_tampering(self, db):
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        child_cap.issued_by_agent_id = child_agent.id
        with pytest.raises(InvalidSignatureError):
            verify_capability_grant(child_cap, root_agent)

    def test_wrong_issuer_verification(self, db):
        """Attempt to verify using the wrong agent's public key."""
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        # root_cap was signed by system_agent. Try verifying with child_agent.
        with pytest.raises(InvalidSignatureError):
            verify_capability_grant(root_cap, child_agent)

    def test_signature_replacement(self, db):
        """Replace the signature entirely with another valid signature for a different capability."""
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        # Replace child's signature with root's signature
        child_cap.grant_signature = root_cap.grant_signature
        
        with pytest.raises(InvalidSignatureError):
            verify_capability_grant(child_cap, root_agent)

    def test_mutable_state_changes_do_not_invalidate_signature(self, db):
        """Verify that operations like reserve, commit, release, revoke do not break the signature."""
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
        risk_engine.set_risk_engine(MockRiskEngine())
        
        root_agent, child_agent, root_cap, child_cap = _setup_tree(db)
        
        # 1. Reserve
        req = ReserveRequest(
            agent_id=child_agent.id,
            amount=Decimal("100"),
            currency="INR",
            merchant="Test",
            category=child_cap.category,
            transaction_time=datetime.now(timezone.utc),
            idempotency_key=str(uuid.uuid4())
        )
        res = reservation_service.reserve_authority(db, child_cap.id, req)
        db.commit()
        
        # Refetch and verify
        child_cap_fresh = get_capability(db, child_cap.id)
        assert child_cap_fresh.reserved_authority == Decimal("100")
        assert child_cap_fresh.unallocated_authority == Decimal("1900")
        verify_capability_grant(child_cap_fresh, root_agent)  # Still valid
        
        # 2. Commit
        reservation_service.commit_reservation(db, res.id)
        db.commit()
        
        child_cap_fresh = get_capability(db, child_cap.id)
        assert child_cap_fresh.committed_authority == Decimal("100")
        assert child_cap_fresh.reserved_authority == Decimal("0")
        verify_capability_grant(child_cap_fresh, root_agent)  # Still valid
        
        # 3. Revoke
        from app.services.revocation_service import revoke_capability
        revoke_capability(db, child_cap.id, root_agent.id)
        db.commit()
        
        child_cap_fresh = get_capability(db, child_cap.id)
        assert child_cap_fresh.status == CapabilityStatus.REVOKED
        verify_capability_grant(child_cap_fresh, root_agent)  # Still valid!
