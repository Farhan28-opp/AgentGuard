from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
import pytest

from app.exceptions import (
    ReservationExpiredError,
    InvalidReservationTransitionError,
    IdempotencyConflictError,
    MerchantDeniedError,
    InsufficientAuthorityError,
)
from app.models.reservation import ReservationStatus
from app.schemas.reservation import ReserveRequest
from app.services import reservation_service
from tests.conftest import create_root_capability, make_agent


def _now():
    return datetime.now(timezone.utc)


def _uid():
    """Unique 8-char suffix for agent identifiers to avoid collision across runs."""
    return str(uuid.uuid4())[:8]


def _key():
    """Unique idempotency key per call."""
    return str(uuid.uuid4())


def test_valid_reservation_succeeds(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs1-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")

    req = ReserveRequest(
        agent_id=root_agent.id,
        amount=Decimal("2000"),
        currency="INR",
        merchant="Supermarket",
        category="groceries",
        transaction_time=_now(),
        idempotency_key=_key(),
    )

    res = reservation_service.reserve_authority(db, root.id, req)
    db.commit()
    db.refresh(root)
    db.refresh(res)

    assert res.amount == Decimal("2000")
    assert res.status == ReservationStatus.RESERVED
    assert root.unallocated_authority == Decimal("3000")
    assert root.reserved_authority == Decimal("2000")
    assert root.committed_authority == Decimal("0")


def test_insufficient_authority_fails(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs2-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")

    req = ReserveRequest(
        agent_id=root_agent.id,
        amount=Decimal("6000"),
        currency="INR",
        merchant="Supermarket",
        category="groceries",
        transaction_time=_now(),
        idempotency_key=_key(),
    )

    with pytest.raises(InsufficientAuthorityError):
        reservation_service.reserve_authority(db, root.id, req)
    db.rollback()


def test_invalid_amount_fails(db, seed_mandate):
    with pytest.raises(ValueError):
        ReserveRequest(
            agent_id=uuid.uuid4(),
            amount=Decimal("0"),
            currency="INR",
            merchant="M",
            category="C",
            transaction_time=_now(),
            idempotency_key=_key(),
        )
    with pytest.raises(ValueError):
        ReserveRequest(
            agent_id=uuid.uuid4(),
            amount=Decimal("-100"),
            currency="INR",
            merchant="M",
            category="C",
            transaction_time=_now(),
            idempotency_key=_key(),
        )


def test_scope_violation_category_fails(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs3-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")

    req = ReserveRequest(
        agent_id=root_agent.id,
        amount=Decimal("100"),
        currency="INR",
        merchant="Store",
        category="electronics",
        transaction_time=_now(),
        idempotency_key=_key(),
    )
    with pytest.raises(MerchantDeniedError):
        reservation_service.reserve_authority(db, root.id, req)
    db.rollback()


def test_commit_reservation(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs4-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")

    req = ReserveRequest(
        agent_id=root_agent.id,
        amount=Decimal("2000"),
        currency="INR",
        merchant="Supermarket",
        category="groceries",
        transaction_time=_now(),
        idempotency_key=_key(),
    )
    res = reservation_service.reserve_authority(db, root.id, req)
    db.commit()

    committed = reservation_service.commit_reservation(db, res.id)
    db.commit()
    db.refresh(root)

    assert committed.status == ReservationStatus.COMMITTED
    assert root.unallocated_authority == Decimal("3000")
    assert root.reserved_authority == Decimal("0")
    assert root.committed_authority == Decimal("2000")


def test_release_reservation(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs5-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")

    req = ReserveRequest(
        agent_id=root_agent.id,
        amount=Decimal("2000"),
        currency="INR",
        merchant="Supermarket",
        category="groceries",
        transaction_time=_now(),
        idempotency_key=_key(),
    )
    res = reservation_service.reserve_authority(db, root.id, req)
    db.commit()

    released = reservation_service.release_reservation(db, res.id)
    db.commit()
    db.refresh(root)

    assert released.status == ReservationStatus.RELEASED
    assert root.unallocated_authority == Decimal("5000")
    assert root.reserved_authority == Decimal("0")
    assert root.committed_authority == Decimal("0")


def test_double_commit_idempotent(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs6-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")
    req = ReserveRequest(
        agent_id=root_agent.id, amount=Decimal("100"), currency="INR",
        merchant="M", category="groceries", transaction_time=_now(), idempotency_key=_key(),
    )
    res = reservation_service.reserve_authority(db, root.id, req)
    db.commit()

    reservation_service.commit_reservation(db, res.id)
    db.commit()
    reservation_service.commit_reservation(db, res.id)
    db.commit()
    db.refresh(root)
    assert root.committed_authority == Decimal("100")
    assert root.reserved_authority == Decimal("0")


def test_commit_after_release_fails(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs7-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")
    req = ReserveRequest(
        agent_id=root_agent.id, amount=Decimal("100"), currency="INR",
        merchant="M", category="groceries", transaction_time=_now(), idempotency_key=_key(),
    )
    res = reservation_service.reserve_authority(db, root.id, req)
    db.commit()

    reservation_service.release_reservation(db, res.id)
    db.commit()

    with pytest.raises(InvalidReservationTransitionError):
        reservation_service.commit_reservation(db, res.id)
    db.rollback()


def test_expired_reservation(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs8-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")
    req = ReserveRequest(
        agent_id=root_agent.id, amount=Decimal("100"), currency="INR",
        merchant="M", category="groceries", transaction_time=_now(), idempotency_key=_key(),
    )
    res = reservation_service.reserve_authority(db, root.id, req)

    # Artificially expire it.
    res.expires_at = _now() - timedelta(seconds=10)
    db.commit()

    cnt = reservation_service.release_expired_reservations(db)
    db.commit()
    assert cnt == 1

    db.refresh(res)
    db.refresh(root)
    assert res.status == ReservationStatus.RELEASED
    assert root.unallocated_authority == Decimal("5000")


def test_idempotency(db, seed_mandate):
    root_agent = make_agent(db, f"shopping-agent-rs9-{_uid()}", "root")
    root = create_root_capability(db, seed_mandate, root_agent, total_authority=Decimal("5000"), category="groceries")

    shared_key = _key()
    req1 = ReserveRequest(
        agent_id=root_agent.id, amount=Decimal("100"), currency="INR",
        merchant="M", category="groceries", transaction_time=_now(), idempotency_key=shared_key,
    )
    res1 = reservation_service.reserve_authority(db, root.id, req1)
    db.commit()

    # Same request — must return the same reservation, not create a second.
    res2 = reservation_service.reserve_authority(db, root.id, req1)
    db.commit()
    assert res1.id == res2.id
    db.refresh(root)
    assert root.reserved_authority == Decimal("100")  # not 200

    # Different amount, same key → 409 Conflict.
    req3 = ReserveRequest(
        agent_id=root_agent.id, amount=Decimal("200"), currency="INR",
        merchant="M", category="groceries", transaction_time=_now(), idempotency_key=shared_key,
    )
    with pytest.raises(IdempotencyConflictError):
        reservation_service.reserve_authority(db, root.id, req3)
    db.rollback()
