import threading
import uuid as _uuid
from decimal import Decimal
from datetime import datetime, timezone
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.exceptions import InsufficientAuthorityError
from app.schemas.reservation import ReserveRequest
from app.services import reservation_service
from tests.conftest import TEST_DATABASE_URL, create_root_capability, make_agent

def _now():
    return datetime.now(timezone.utc)

def _key():
    """Return a unique idempotency key for this test invocation.

    Concurrency tests open independent sessions that commit and are NOT
    rolled back by the db fixture. Using static key strings (e.g. "reqA")
    would cause UniqueViolation on the second pytest run in the same
    session-scoped schema. Using uuid4 guarantees each call is fresh.
    """
    return str(_uuid.uuid4())

def test_concurrent_reservation_spend_race(db, seed_mandate):
    """
    Scenario:
    Capability = ₹5,000
    Concurrent Request A = ₹3,000
    Concurrent Request B = ₹3,000
    Expected: One success, one failure.
    final reserved = ₹3,000, final unallocated = ₹2,000
    """
    root_agent = make_agent(db, f"shopping-agent-c1-{_key()[:8]}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")
    db.commit()

    engine = create_engine(TEST_DATABASE_URL, pool_size=5)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def worker(key, results):
        session = SessionLocal()
        req = ReserveRequest(
            agent_id=root_agent.id,
            amount=Decimal("3000"),
            currency="INR",
            merchant="Store",
            category="groceries",
            transaction_time=_now(),
            idempotency_key=key
        )
        try:
            reservation_service.reserve_authority(session, root.id, req)
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
    t1 = threading.Thread(target=worker, args=(_key(), results))
    t2 = threading.Thread(target=worker, args=(_key(), results))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert "success" in results
    assert "failed_authority" in results
    assert len(results) == 2

    db.refresh(root)
    assert root.unallocated_authority == Decimal("2000")
    assert root.reserved_authority == Decimal("3000")


def test_concurrent_dual_reservation(db, seed_mandate):
    """
    Test B:
    Capability: 5000
    Concurrent: A=2500, B=2500
    Expected: Both succeed, reserved=5000, unallocated=0.
    Third concurrent request C=1 fails.
    """
    root_agent = make_agent(db, f"shopping-agent-c2-{_key()[:8]}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")
    db.commit()

    engine = create_engine(TEST_DATABASE_URL, pool_size=5)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def worker(amt, key, results):
        session = SessionLocal()
        req = ReserveRequest(
            agent_id=root_agent.id,
            amount=Decimal(amt),
            currency="INR",
            merchant="Store",
            category="groceries",
            transaction_time=_now(),
            idempotency_key=key
        )
        try:
            reservation_service.reserve_authority(session, root.id, req)
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
    t1 = threading.Thread(target=worker, args=("2500", _key(), results))
    t2 = threading.Thread(target=worker, args=("2500", _key(), results))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results.count("success") == 2

    db.refresh(root)
    assert root.reserved_authority == Decimal("5000")
    assert root.unallocated_authority == Decimal("0")

    # Third request must fail
    res3 = []
    worker("1", _key(), res3)
    assert res3 == ["failed_authority"]
