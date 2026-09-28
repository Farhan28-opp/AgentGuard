import threading
import uuid as _uuid
from decimal import Decimal
from datetime import datetime, timezone
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.exceptions import InsufficientAuthorityError, CapabilityNotActiveError
from app.schemas.reservation import ReserveRequest
from app.services import reservation_service, revocation_service
from app.models.capability import CapabilityStatus
from tests.conftest import TEST_DATABASE_URL, create_root_capability, make_agent

def _now():
    return datetime.now(timezone.utc)

def _key():
    return str(_uuid.uuid4())

def _uid():
    return str(_uuid.uuid4())[:8]

def test_concurrent_reservation_vs_revocation(db, seed_mandate):
    """
    Scenario:
    Capability = ₹5,000
    Concurrent operations:
      A = reserve ₹3,000
      B = revoke capability
      
    Expected: Final state must be consistent.
    Either reservation commits first, then revocation releases it.
    Or revocation commits first, and reservation is rejected (CapabilityNotActiveError).
    """
    root_agent = make_agent(db, f"root-agent-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")
    db.commit()

    engine = create_engine(TEST_DATABASE_URL, pool_size=5)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def reserve_worker(key, results):
        session = SessionLocal()
        req = ReserveRequest(
            agent_id=root_agent.id,
            amount=Decimal("3000"),
            currency="INR",
            merchant="M",
            category="groceries",
            transaction_time=_now(),
            idempotency_key=key
        )
        try:
            reservation_service.reserve_authority(session, root.id, req)
            session.commit()
            results.append("reserve_success")
        except CapabilityNotActiveError:
            session.rollback()
            results.append("reserve_failed_not_active")
        except Exception as e:
            session.rollback()
            results.append(f"reserve_failed_other_{e}")
        finally:
            session.close()

    def revoke_worker(results):
        session = SessionLocal()
        try:
            revocation_service.revoke_capability(session, root.id, root_agent.id)
            session.commit()
            results.append("revoke_success")
        except Exception as e:
            session.rollback()
            results.append(f"revoke_failed_{e}")
        finally:
            session.close()

    results = []
    t1 = threading.Thread(target=reserve_worker, args=(_key(), results))
    t2 = threading.Thread(target=revoke_worker, args=(results,))

    # We start them essentially at the same time
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # The revoke operation must always succeed
    assert "revoke_success" in results

    # Refresh the root capability
    db.refresh(root)
    assert root.status == CapabilityStatus.REVOKED
    
    # Check outcomes
    if "reserve_success" in results:
        # If reservation succeeded, it must have been released by the revocation
        assert root.reserved_authority == Decimal("0")
        assert root.unallocated_authority == Decimal("5000")
    elif "reserve_failed_not_active" in results:
        # If reservation failed due to revoked status, authority was never locked
        assert root.reserved_authority == Decimal("0")
        assert root.unallocated_authority == Decimal("5000")
    else:
        pytest.fail(f"Unexpected results: {results}")

