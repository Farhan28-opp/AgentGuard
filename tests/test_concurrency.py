"""Tests for concurrent Capability Issuance (Day 2 requirements)."""
import threading
import uuid as _uuid
from decimal import Decimal
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.exceptions import InsufficientAuthorityError, FanoutExceededError
from app.schemas.capability import CapabilityIssueRequest
from app.services import capability_service
from tests.conftest import TEST_DATABASE_URL, create_root_capability, make_agent

def _uid():
    """Short unique suffix to avoid agent_identifier collisions across runs."""
    return str(_uuid.uuid4())[:8]

def test_concurrent_issuance_authority_conservation(db, seed_mandate):
    """
    Scenario:
    Parent authority = 5000
    Concurrent request A = 3000
    Concurrent request B = 3000
    Expected: Exactly one succeeds, exactly one fails, parent unallocated = 2000.
    """
    root_agent = make_agent(db, f"shopping-agent-concurrency-{_uid()}", "root")
    target_a = make_agent(db, f"target-a-{_uid()}", "purchase")
    target_b = make_agent(db, f"target-b-{_uid()}", "purchase")
    
    root = create_root_capability(
        db, seed_mandate, root_agent,
        total_authority=Decimal("5000"),
        max_fanout=2,  # Allow both threads to attempt issuance; authority is what limits them
    )
    db.commit()

    engine = create_engine(TEST_DATABASE_URL, pool_size=5)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def worker(target_agent_id, results):
        session = SessionLocal()
        req = CapabilityIssueRequest(
            parent_capability_id=root.id,
            issued_to_agent_id=target_agent_id,
            issued_by_agent_id=root_agent.id,
            amount=Decimal("3000"),
            purpose="test",
            category=root.category,
            max_delegation_depth=1,
            max_fanout=2,
            not_before=root.not_before,
            not_after=root.not_after
        )
        try:
            capability_service.issue_capability(session, req)
            session.commit()
            results.append("success")
        except InsufficientAuthorityError:
            session.rollback()
            results.append("failed_authority")
        except Exception as e:
            session.rollback()
            results.append(f"failed_other_{e}")
        finally:
            session.close()

    results = []
    t1 = threading.Thread(target=worker, args=(target_a.id, results))
    t2 = threading.Thread(target=worker, args=(target_b.id, results))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one success and one failure expected
    assert "success" in results
    assert "failed_authority" in results
    assert len(results) == 2

    # Verify state
    db.refresh(root)
    assert root.unallocated_authority == Decimal("2000")


def test_concurrent_issuance_fanout_limit(db, seed_mandate):
    """
    Scenario:
    max_fanout = 1
    two concurrent child creation requests
    Expected: Exactly one child created.
    """
    root_agent = make_agent(db, f"shopping-agent-fanout-concurrency-{_uid()}", "root")
    target_a = make_agent(db, f"target-fanout-a-{_uid()}", "purchase")
    target_b = make_agent(db, f"target-fanout-b-{_uid()}", "purchase")
    
    root = create_root_capability(
        db, seed_mandate, root_agent, total_authority=Decimal("5000"), max_fanout=1
    )
    db.commit()

    engine = create_engine(TEST_DATABASE_URL, pool_size=5)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def worker(target_agent_id, results):
        session = SessionLocal()
        req = CapabilityIssueRequest(
            parent_capability_id=root.id,
            issued_to_agent_id=target_agent_id,
            issued_by_agent_id=root_agent.id,
            amount=Decimal("1000"),
            purpose="test",
            category=root.category,
            max_delegation_depth=1,
            max_fanout=1,
            not_before=root.not_before,
            not_after=root.not_after
        )
        try:
            capability_service.issue_capability(session, req)
            session.commit()
            results.append("success")
        except FanoutExceededError:
            session.rollback()
            results.append("failed_fanout")
        except Exception as e:
            session.rollback()
            results.append(f"failed_other_{e}")
        finally:
            session.close()

    results = []
    t1 = threading.Thread(target=worker, args=(target_a.id, results))
    t2 = threading.Thread(target=worker, args=(target_b.id, results))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert "success" in results
    assert "failed_fanout" in results
    
    db.refresh(root)
    assert root.unallocated_authority == Decimal("4000")
