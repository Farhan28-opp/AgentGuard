"""Shared pytest fixtures and helpers.

These tests hit a real PostgreSQL database (see README "Running tests"
for how to point this at a disposable local/CI database via
TEST_DATABASE_URL) rather than mocking the ORM, because the invariants
under test are enforced partly by row locks and check constraints that a
mock session would not exercise.
"""
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from dotenv import load_dotenv

# Drunix enforcement in tests is opt-in from the shell (DRUNIX_MODE=enforce
# pytest ...), never inherited from a developer's .env: the unit suite must
# not depend on a running ledger. tests/test_drunix_*.py switch it per test.
_SHELL_DRUNIX_MODE = os.environ.get("DRUNIX_MODE")
load_dotenv()  # local .env (git-ignored) may define TEST_DATABASE_URL
os.environ["DRUNIX_MODE"] = _SHELL_DRUNIX_MODE or "off"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
if not TEST_DATABASE_URL:
    raise RuntimeError(
        "Set TEST_DATABASE_URL to a THROWAWAY PostgreSQL database (see .env.example); "
        "the test suite creates and drops all tables in it."
    )
# Point the *application* (FastAPI routes, demo endpoints, SessionLocal) at the
# test database too, BEFORE app.config is imported. Previously the product and
# demo tests ran against the dev database and /demo/reset wiped it.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Keep test-generated private keys out of the project's dev_keys/ directory.
os.environ.setdefault("DEV_KEYS_DIR", tempfile.mkdtemp(prefix="agentguard-test-keys-"))
os.environ.pop("AGENT_KEY_SEED", None)          # tests use the file key backend
os.environ["BOOTSTRAP_ON_STARTUP"] = "false"
os.environ["EXPIRY_SWEEP_SECONDS"] = "0"        # tests drive expiry explicitly

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent import Agent
from app.models.mandate import Mandate, MandateStatus
from app.models.user import User
from app.schemas.capability import CapabilityCreate
from app.services import capability_service

_engine = create_engine(TEST_DATABASE_URL)
_TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    try:
        yield
    finally:
        Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def db():
    session = _TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def seed_user(db):
    user = User(id=uuid.uuid4(), name="Test User")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def seed_mandate(db, seed_user):
    now = datetime.now(timezone.utc)
    mandate = Mandate(
        id=uuid.uuid4(),
        user_id=seed_user.id,
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


def make_agent(db, identifier: str, agent_type: str = "root") -> Agent:
    agent, _ = make_agent_with_key(db, identifier, agent_type)
    return agent


def create_root_capability(
    db,
    mandate: Mandate,
    agent: Agent,
    total_authority: Decimal = Decimal("10000"),
    max_delegation_depth: int = 3,
    max_fanout: int = 5,
    category: str = "groceries",
):
    # Ensure system-agent exists for grant signing
    system_agent = db.scalar(select(Agent).where(Agent.agent_identifier == "system-agent"))
    if not system_agent:
        system_agent, _ = make_agent_with_key(db, "system-agent", "root")

    data = CapabilityCreate(
        parent_capability_id=None,
        root_mandate_id=mandate.id,
        issued_to_agent_id=agent.id,
        issued_by_agent_id=None,
        total_authority=total_authority,
        purpose="root grant",
        category=category,
        max_delegation_depth=max_delegation_depth,
        max_fanout=max_fanout,
        not_before=mandate.not_before,
        not_after=mandate.not_after,
    )
    capability = capability_service.create_capability(db, data)
    db.commit()
    db.refresh(capability)
    return capability


from app.services import risk_engine
from app.schemas.risk import RiskLevel, RiskAction, RiskResult

class MockRiskEngine:
    def __init__(self, force_level=RiskLevel.LOW, action=RiskAction.ALLOW):
        self.force_level = force_level
        self.action = action
        self.is_loaded = True
        self.medium_threshold = 0.05
        self.high_threshold = 0.15

    def evaluate(self, features, capability_id=None, context=None):
        return RiskResult(
            risk_level=self.force_level,
            action=self.action,
            anomaly_score=0.01,
            reason_codes=[],
            reasons=["Mocked LOW risk"],
            capability_id=capability_id,
        )

@pytest.fixture(autouse=True)
def mock_risk_engine_fixture():
    original_engine = risk_engine._engine
    risk_engine.set_risk_engine(MockRiskEngine())
    yield
    risk_engine.set_risk_engine(original_engine)


def build_child_data(parent, agent, **overrides) -> CapabilityCreate:
    base = dict(
        parent_capability_id=parent.id,
        root_mandate_id=parent.root_mandate_id,
        issued_to_agent_id=agent.id,
        issued_by_agent_id=parent.issued_to_agent_id,
        total_authority=Decimal("1000"),
        purpose="child grant",
        category=parent.category,
        max_delegation_depth=parent.max_delegation_depth,
        max_fanout=parent.max_fanout,
        not_before=parent.not_before,
        not_after=parent.not_after,
    )
    base.update(overrides)
    return CapabilityCreate(**base)


def create_child_capability(db, parent, agent, **overrides):
    data = build_child_data(parent, agent, **overrides)
    capability = capability_service.create_capability(db, data)
    db.commit()
    db.refresh(capability)
    return capability


# ── Day 6: Signing test helpers ────────────────────────────────────────────

from app.security.keys import (
    generate_keypair,
    serialize_public_key,
    sign_and_encode,
    sha256_payload,
)
from datetime import datetime, timezone


def make_agent_with_key(db, identifier: str, agent_type: str = "purchase"):
    """Create an agent with a freshly-generated Ed25519 keypair.

    Returns (agent, private_key) — private_key is held in memory for tests only.
    """
    private_key, public_key = generate_keypair()
    pem = serialize_public_key(public_key)
    agent = Agent(
        id=uuid.uuid4(),
        agent_identifier=identifier,
        agent_type=agent_type,
        status="active",
        public_key=pem,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    
    from app.security.keys import save_dev_private_key
    save_dev_private_key(identifier, private_key)
    
    return agent, private_key


def build_signed_envelope(
    *,
    private_key,
    agent_id: uuid.UUID,
    operation: str,
    resource_id: str,
    payload_dict: dict = None,
    request_id: str = None,
    timestamp: str = None,
):
    """Build a complete signed envelope dict suitable for JSON submission."""
    if request_id is None:
        request_id = str(uuid.uuid4())
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if payload_dict is not None:
        payload_hash = sha256_payload(payload_dict)
    else:
        payload_hash = sha256_payload({})

    from app.security.signing import canonical_signed_bytes
    data = canonical_signed_bytes(
        agent_id=str(agent_id),
        operation=operation,
        resource_id=resource_id,
        request_id=request_id,
        timestamp=timestamp,
        payload_hash=payload_hash,
    )
    signature = sign_and_encode(private_key, data)

    return {
        "agent_id": str(agent_id),
        "operation": operation,
        "resource_id": resource_id,
        "request_id": request_id,
        "timestamp": timestamp,
        "payload_hash": payload_hash,
        "signature": signature,
    }


# ── Real risk engine (product / demo integration tests) ───────────────────────

@pytest.fixture()
def real_risk_engine(monkeypatch):
    """Use the real trained IsolationForest instead of the autouse mock.

    The model's "unusual time" feature depends on the wall clock; to keep
    these integration tests deterministic at any hour, every hour is treated
    as normal here. The time feature itself is covered with fixed timestamps
    in test_feature_extraction.py.
    """
    from app.config import settings
    from app.services.risk_engine import RiskEngine

    engine = RiskEngine(
        medium_threshold=settings.risk_medium_threshold,
        high_threshold=settings.risk_high_threshold,
    )
    assert engine.is_loaded, "ml_models/isolation_forest.joblib missing"
    monkeypatch.setattr(settings, "risk_normal_hours_start", 0)
    monkeypatch.setattr(settings, "risk_normal_hours_end", 24)
    original = risk_engine._engine
    risk_engine.set_risk_engine(engine)
    yield engine
    risk_engine.set_risk_engine(original)
