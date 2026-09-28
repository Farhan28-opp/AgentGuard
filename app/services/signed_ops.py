"""Signed agent operations used by the consumer product and demo surfaces.

Every money-touching operation an agent performs goes through the same
Day-6 envelope and verification pipeline as the public HTTP routes:

    reserve  ->  POST /capabilities/{capability_id}/reserve
    pay      ->  POST /reservations/{reservation_id}/pay   (commits the hold)
    release  ->  POST /reservations/{reservation_id}/release

Agent side (the runtime holding the private key):
    canonical bytes = agent_id | operation | resource_id | request_id |
                      timestamp | sha256(payload)
    signature       = Ed25519(private_key, canonical bytes)

Server side (``verify_signed_request`` — identical to the HTTP routes):
    timestamp window -> registered public key -> Ed25519 verify ->
    payload hash integrity -> anti-replay nonce (request_id) ->
    operation / resource binding -> ownership -> ledger operation

No loopback HTTP call is made because the agent runtimes are hosted in the
same process, but nothing is skipped: the server half only uses the public
key registered in PostgreSQL.

  * ``attenuate`` — the issuer (Main Agent) takes back a task capability's
    unused authority after the task ends.

What is *not* independently signed (documented, not hidden):
  * ``commit`` in the product flow happens inside the signed ``pay``
    operation (``payment_service.execute_payment`` commits the reservation
    and writes the Payment row in the same DB transaction). The public
    ``/reservations/{id}/commit`` route remains available and signed.
  * HIGH-risk containment revokes the capability subtree from inside the
    signed ``reserve`` request that triggered it (system control action,
    attributed to the capability's issuer). Manual revocation via
    ``/capabilities/{id}/revoke`` requires a signed ``revoke`` envelope.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Optional

from sqlalchemy.orm import Session

from app.exceptions import AgentAuthorizationError, NotFoundError
from app.models.agent import Agent
from app.models.capability import Capability
from app.models.payment import Payment
from app.models.reservation import Reservation
from app.schemas.reservation import ReserveRequest
from app.security.keys import load_dev_private_key, sha256_payload, sign_and_encode
from app.security.signing import canonical_signed_bytes, verify_signed_request
from app.services import payment_service, reservation_service


@dataclass
class SignedEnvelope:
    agent_id: str
    operation: str
    resource_id: str
    request_id: str
    timestamp: str
    payload_hash: str
    signature: str

    def as_dict(self) -> Dict[str, str]:
        return dict(self.__dict__)


# ── Agent runtime side ───────────────────────────────────────────────────────

def sign_request(
    agent: Agent,
    operation: str,
    resource_id: str,
    payload: Optional[dict] = None,
) -> SignedEnvelope:
    """Sign an operation with the agent's private key (agent runtime side)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    request_id = str(uuid.uuid4())
    payload_hash = sha256_payload(payload if payload is not None else {})
    private_key = load_dev_private_key(agent.agent_identifier)
    signature = sign_and_encode(
        private_key,
        canonical_signed_bytes(
            agent_id=str(agent.id),
            operation=operation,
            resource_id=resource_id,
            request_id=request_id,
            timestamp=timestamp,
            payload_hash=payload_hash,
        ),
    )
    return SignedEnvelope(
        agent_id=str(agent.id),
        operation=operation,
        resource_id=resource_id,
        request_id=request_id,
        timestamp=timestamp,
        payload_hash=payload_hash,
        signature=signature,
    )


# ── Server side ──────────────────────────────────────────────────────────────

def verify(db: Session, env: SignedEnvelope, payload: Optional[dict] = None) -> Agent:
    """Server-side verification — the exact function the HTTP routes use.
    The nonce insert is flushed into the caller's transaction."""
    return verify_signed_request(
        db,
        agent_id=uuid.UUID(env.agent_id),
        operation=env.operation,
        resource_id=env.resource_id,
        request_id=env.request_id,
        timestamp=env.timestamp,
        payload_hash=env.payload_hash,
        signature=env.signature,
        payload_dict=payload if payload is not None else {},
    )


def _assert_holds_reservation(db: Session, reservation_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    res = db.get(Reservation, reservation_id)
    if res is None:
        raise NotFoundError(f"Reservation {reservation_id} not found.")
    cap = db.get(Capability, res.capability_id)
    if cap is None or cap.issued_to_agent_id != agent_id:
        raise AgentAuthorizationError(
            "Signing agent does not hold the capability backing this reservation."
        )


# A test / demo hook that receives the envelope + payload after signing and
# may alter them before they reach the verifier (used to prove tampering is
# rejected). Never set in the normal product flow.
Tamper = Callable[[SignedEnvelope, dict], None]


def reserve_payload(
    amount: Decimal,
    merchant: str,
    category: str,
    transaction_time: datetime,
    idempotency_key: str,
    currency: str = "INR",
) -> dict:
    """Canonical reserve payload — same normalisation as the HTTP route."""
    return {
        "amount": str(amount),
        "currency": currency,
        "merchant": merchant,
        "category": category,
        "transaction_time": transaction_time.isoformat(),
        "idempotency_key": idempotency_key,
    }


def signed_reserve(
    db: Session,
    *,
    agent: Agent,
    capability: Capability,
    amount: Decimal,
    merchant: str,
    category: str,
    transaction_time: Optional[datetime] = None,
    tamper: Optional[Tamper] = None,
    risk_context: Optional[dict] = None,
) -> Reservation:
    """Agent signs a reserve request; server verifies it, then runs the
    deterministic authority checks and the behavioural risk engine
    (inside ``reservation_service.reserve_authority``).

    The returned reservation carries ``risk_result`` / ``risk_features``
    (the decision that actually admitted it). Raises on any failure; a
    HIGH-risk result raises ``HighRiskContainmentError`` *after* the
    subtree revocation has been staged — the caller must commit it.
    """
    tx_time = transaction_time or datetime.now(timezone.utc)
    payload = reserve_payload(amount, merchant, category, tx_time, str(uuid.uuid4()))
    env = sign_request(agent, "reserve", str(capability.id), payload)
    if tamper is not None:
        tamper(env, payload)

    verify(db, env, payload)
    if env.resource_id != str(capability.id):
        from app.exceptions import UnauthorizedOperationError
        raise UnauthorizedOperationError("Envelope resource_id does not match capability.")

    request = ReserveRequest(
        agent_id=uuid.UUID(env.agent_id),
        amount=Decimal(payload["amount"]),
        currency=payload["currency"],
        merchant=payload["merchant"],
        category=payload["category"],
        transaction_time=datetime.fromisoformat(payload["transaction_time"]),
        idempotency_key=payload["idempotency_key"],
    )
    return reservation_service.reserve_authority(db, capability.id, request, risk_context=risk_context)


def signed_pay(
    db: Session,
    *,
    agent: Agent,
    reservation_id: uuid.UUID,
    tamper: Optional[Tamper] = None,
) -> Payment:
    """Agent signs a ``pay`` operation bound to the reservation; the server
    verifies it and settles the hold on the simulated rail (commit + Payment
    row, same transaction). Caller commits."""
    payload: dict = {}
    env = sign_request(agent, "pay", str(reservation_id), payload)
    if tamper is not None:
        tamper(env, payload)
    verify(db, env, payload)
    if env.resource_id != str(reservation_id):
        from app.exceptions import UnauthorizedOperationError
        raise UnauthorizedOperationError("Envelope resource_id does not match reservation.")
    _assert_holds_reservation(db, reservation_id, uuid.UUID(env.agent_id))
    return payment_service.execute_payment(db, reservation_id)


def signed_release(
    db: Session,
    *,
    agent: Agent,
    reservation_id: uuid.UUID,
) -> Reservation:
    """Agent signs a ``release`` for its own hold; authority returns to the
    capability's unallocated pool. Caller commits."""
    payload: dict = {}
    env = sign_request(agent, "release", str(reservation_id), payload)
    verify(db, env, payload)
    _assert_holds_reservation(db, reservation_id, uuid.UUID(env.agent_id))
    return reservation_service.release_reservation(db, reservation_id)


def signed_revoke(
    db: Session,
    *,
    agent: Agent,
    capability_id: uuid.UUID,
):
    """Agent signs a ``revoke`` for a capability it issued (or, for a root
    capability, an agent of type ``root``); the server verifies it and runs
    the recursive subtree revocation, releasing active holds. Caller commits.
    """
    from app.services import revocation_service

    payload: dict = {}
    env = sign_request(agent, "revoke", str(capability_id), payload)
    verify(db, env, payload)
    return revocation_service.revoke_capability(db, capability_id, uuid.UUID(env.agent_id))


def signed_return_unused(db: Session, *, agent: Agent, capability_id: uuid.UUID) -> Decimal:
    """Issuer signs an ``attenuate`` operation: shrink a child capability to
    what it used and take the unused authority back. Caller commits."""
    from app.services import capability_service

    payload: dict = {}
    env = sign_request(agent, "attenuate", str(capability_id), payload)
    verify(db, env, payload)
    return capability_service.return_unused_authority(db, capability_id, uuid.UUID(env.agent_id))
