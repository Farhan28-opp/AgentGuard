"""Domain exceptions for AgentGuard's authority model.

These are distinct from generic HTTP errors: the API layer (see
app/main.py) maps each of these to a specific HTTP status code, but the
service and authority layers raise them without any knowledge of HTTP at
all, so the same validation logic can be reused outside a web context
(e.g. from the seed script, or a future CLI).
"""


class AgentGuardError(Exception):
    """Base class for all AgentGuard domain errors."""


class NotFoundError(AgentGuardError):
    """A referenced entity (mandate, capability, user, agent...) does not exist."""


class IssuerAuthorizationError(AgentGuardError):
    """The issuing agent does not hold the parent capability."""


class SelfDelegationError(AgentGuardError):
    """An agent attempted to delegate a capability to itself."""


class TargetAgentError(AgentGuardError):
    """The target agent is invalid or inactive."""

class MandateNotActiveError(AgentGuardError):
    """An operation was attempted against a mandate that is not active."""


class CapabilityNotActiveError(AgentGuardError):
    """An operation was attempted against a capability that is not active."""


class InsufficientAuthorityError(AgentGuardError):
    """Invariant 1 / 8 (No authority creation / Authority conservation):
    a requested amount exceeds what is actually available to delegate or spend."""


class ScopeViolationError(AgentGuardError):
    """Invariant 2 (Scope can only narrow): a child's category or merchant
    scope is broader than its parent's."""


class ExpiryViolationError(AgentGuardError):
    """Invariant 3 (Expiry can only narrow): a child's active window is not
    contained within its parent's (or the mandate's, for root capabilities)."""


class DelegationDepthExceededError(AgentGuardError):
    """Invariant 4 (Delegation depth): a child's depth or delegation ceiling
    is invalid relative to its parent."""


class FanoutExceededError(AgentGuardError):
    """Invariant 5 (Fanout): a parent has reached its max_fanout."""


class DelegationLoopError(AgentGuardError):
    """Invariant 6 (No delegation loops): the new capability's recipient
    agent already appears in its own ancestor chain."""


class ZeroAuthorityViolationError(AgentGuardError):
    """Invariant 7 (No zero-authority payment capability): a capability
    issued with zero total authority attempted to delegate nonzero authority."""

class ReservationExpiredError(AgentGuardError):
    """The reservation TTL has elapsed."""


class InvalidReservationTransitionError(AgentGuardError):
    """Illegal state transition (e.g. committed to released)."""


class IdempotencyConflictError(AgentGuardError):
    """Same idempotency key used with different request parameters."""


class AgentAuthorizationError(AgentGuardError):
    """The requesting agent does not hold this capability."""


class CurrencyMismatchError(AgentGuardError):
    """The requested currency does not match the capability's currency."""


class MerchantDeniedError(AgentGuardError):
    """The merchant is on the denylist or not on the allowlist."""


class TransactionTimeViolationError(AgentGuardError):
    """The transaction time falls outside the capability's validity window."""


class RiskReviewError(AgentGuardError):
    """MEDIUM behavioural risk — the transaction is blocked pending review.

    The risk_result attribute carries the full RiskResult so that the
    API layer can return structured reason_codes and reasons.
    """

    def __init__(self, message: str, risk_result=None):
        super().__init__(message)
        self.risk_result = risk_result


class HighRiskContainmentError(AgentGuardError):
    """HIGH behavioural risk — the capability subtree has been revoked.

    By the time this exception propagates to the API layer, the
    revocation has already been committed to the database.  The
    exception handler must NOT roll back the transaction.
    """

    def __init__(self, message: str, risk_result=None):
        super().__init__(message)
        self.risk_result = risk_result


# ── Day 6: Cryptographic Identity Exceptions ─────────────────────────────────

class InvalidSignatureError(AgentGuardError):
    """Ed25519 signature verification failed.

    Either the message was tampered with, the wrong key was used, or the
    signature was malformed.
    HTTP: 401 Unauthorized
    """


class UnknownAgentKeyError(AgentGuardError):
    """No registered Ed25519 public key found for the claiming agent.

    The agent either has not been registered with a key, or the agent_id
    does not exist.
    HTTP: 401 Unauthorized
    """


class RequestReplayError(AgentGuardError):
    """A previously consumed request_id was submitted again.

    Signed requests are one-time-use.  Store and check request_id values
    in the request_nonces table.
    HTTP: 409 Conflict
    """


class ExpiredSignedRequestError(AgentGuardError):
    """The signed request timestamp falls outside the server's clock-skew window.

    The default window is ±300 seconds (configurable via clock_skew_seconds).
    HTTP: 401 Unauthorized
    """


class PayloadIntegrityError(AgentGuardError):
    """The payload hash in the signed envelope does not match the request body.

    This indicates either tampering or a client-side serialisation mismatch.
    HTTP: 400 Bad Request
    """


class UnauthorizedOperationError(AgentGuardError):
    """The authenticated agent is not permitted to perform this operation.

    Authentication succeeded (valid signature) but authorization failed
    (e.g. attempting to revoke a capability it did not issue).
    HTTP: 403 Forbidden
    """


# ---------------------------------------------------------------------------
# HTTP mapping (shared by app.main's exception handler and the product API)
# ---------------------------------------------------------------------------

SIGNATURE_ERRORS = (
    InvalidSignatureError,
    UnknownAgentKeyError,
    RequestReplayError,
    ExpiredSignedRequestError,
    PayloadIntegrityError,
    UnauthorizedOperationError,
)

class DrunixError(AgentGuardError):
    """A financial operation was not confirmed by the Drunix ledger.

    Carries the bridge's structured error: ``category`` (CHAINCODE_REJECTED,
    MVCC_READ_CONFLICT, DRUNIX_UNAVAILABLE, DRUNIX_TIMEOUT,
    DRUNIX_INVALID_COMMIT, BRIDGE_ERROR), the chaincode's rejection ``code``
    (e.g. INSUFFICIENT_AUTHORITY), the failing ``stage`` and, when Drunix
    assigned one, the transaction id and block number.
    """

    def __init__(self, message: str, *, category: str = "", code: str = "", stage: str = "",
                 function: str = "", tx_id: str = "", block_number=None):
        super().__init__(message)
        self.category = category
        self.code = code
        self.stage = stage
        self.function = function
        self.tx_id = tx_id
        self.block_number = block_number

    def as_dict(self) -> dict:
        return {"category": self.category, "code": self.code, "stage": self.stage,
                "function": self.function, "tx_id": self.tx_id or None,
                "block_number": self.block_number, "message": str(self)}


class DrunixRejectedError(DrunixError):
    """The agentauth chaincode on Drunix refused the operation."""


class DrunixConflictError(DrunixError):
    """The transaction lost an MVCC race on Drunix and was not applied."""


class DrunixInvalidCommitError(DrunixError):
    """The transaction was ordered but not committed as VALID."""


class DrunixUnavailableError(DrunixError):
    """The Drunix bridge or network could not be reached."""


class DrunixTimeoutError(DrunixError):
    """Drunix did not return a final answer in time (outcome unknown)."""


STATUS_BY_EXCEPTION = {
    NotFoundError: 404,
    MandateNotActiveError: 409,
    CapabilityNotActiveError: 409,
    InsufficientAuthorityError: 409,
    FanoutExceededError: 409,
    ScopeViolationError: 422,
    ExpiryViolationError: 422,
    DelegationDepthExceededError: 422,
    DelegationLoopError: 422,
    ZeroAuthorityViolationError: 422,
    IssuerAuthorizationError: 403,
    TargetAgentError: 422,
    SelfDelegationError: 422,
    ReservationExpiredError: 409,
    InvalidReservationTransitionError: 409,
    IdempotencyConflictError: 409,
    AgentAuthorizationError: 403,
    CurrencyMismatchError: 422,
    MerchantDeniedError: 422,
    TransactionTimeViolationError: 422,
    HighRiskContainmentError: 403,
    RiskReviewError: 409,
    InvalidSignatureError: 401,
    UnknownAgentKeyError: 401,
    RequestReplayError: 409,
    ExpiredSignedRequestError: 401,
    PayloadIntegrityError: 400,
    UnauthorizedOperationError: 403,
    DrunixRejectedError: 409,
    DrunixConflictError: 409,
    DrunixInvalidCommitError: 502,
    DrunixUnavailableError: 503,
    DrunixTimeoutError: 504,
    DrunixError: 502,
}


def http_status_for(exc: Exception) -> int:
    return STATUS_BY_EXCEPTION.get(type(exc), 400 if isinstance(exc, AgentGuardError) else 500)
