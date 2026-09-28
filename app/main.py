from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.api import audit, capabilities, mandates, reservations
from app.api import agents, payments
from app.exceptions import (
    AgentGuardError,
    CapabilityNotActiveError,
    DelegationDepthExceededError,
    DelegationLoopError,
    ExpiryViolationError,
    FanoutExceededError,
    InsufficientAuthorityError,
    MandateNotActiveError,
    NotFoundError,
    ScopeViolationError,
    ZeroAuthorityViolationError,
    IssuerAuthorizationError,
    TargetAgentError,
    SelfDelegationError,
    ReservationExpiredError,
    InvalidReservationTransitionError,
    IdempotencyConflictError,
    AgentAuthorizationError,
    CurrencyMismatchError,
    MerchantDeniedError,
    TransactionTimeViolationError,
    HighRiskContainmentError,
    RiskReviewError,
    # Day 6 security exceptions
    InvalidSignatureError,
    UnknownAgentKeyError,
    RequestReplayError,
    ExpiredSignedRequestError,
    PayloadIntegrityError,
    UnauthorizedOperationError,
)

app = FastAPI(
    title="AgentGuard",
    description="Financial authority infrastructure for autonomous payment agents: bounded, delegable, reservable, revocable authority.",
    version="1.0.0-prototype",
)

app.include_router(mandates.router)
app.include_router(capabilities.router)
app.include_router(reservations.router)
app.include_router(audit.router)
app.include_router(agents.router)
app.include_router(payments.router)

from app.api import commerce, dashboard, demo, drunix, payment_requests, product
app.include_router(dashboard.router)
app.include_router(drunix.router)
app.include_router(demo.router)
app.include_router(product.router)
app.include_router(commerce.router)
app.include_router(payment_requests.router)

import logging
import os
import threading

from app.config import settings

_log = logging.getLogger("agentguard")


def _expiry_sweeper(stop: threading.Event) -> None:
    """Release RESERVED holds whose TTL has passed, so expired authority
    returns to the unallocated pool even if nobody touches the task again."""
    from app.database import SessionLocal
    from app.services import audit_service, reservation_service

    while not stop.wait(settings.expiry_sweep_seconds):
        db = SessionLocal()
        try:
            count = reservation_service.release_expired_reservations(db)
            if count:
                audit_service.record(db, "RESERVATIONS_EXPIRED", "agentguard",
                                     {"released": count, "reason": "reservation TTL elapsed"})
            db.commit()
            # Drunix: re-apply restrictive operations the ledger has not confirmed yet.
            from app.services import ledger_sync
            if ledger_sync.enabled():
                ledger_sync.retry_pending()
        except Exception:  # pragma: no cover - best-effort background task
            db.rollback()
            _log.exception("expiry sweep failed")
        finally:
            db.close()


_sweeper_stop = threading.Event()


def _bootstrap_demo_state() -> None:
    """Idempotent startup provisioning. NEVER deletes data: upserts the
    simulated catalogue (missing rows only) and makes sure the demo user has
    a policy, agents with usable keys and an active standing mandate. Skipped
    (with a log line) if migrations have not been applied yet."""
    from sqlalchemy import inspect
    from app.database import SessionLocal, engine
    from app.services import policy_service
    from app.services.catalog_seed import seed_catalog

    if not inspect(engine).has_table("agent_policies"):
        _log.warning("Schema not migrated — run `alembic upgrade head`. Skipping demo bootstrap.")
        return
    db = SessionLocal()
    try:
        seed_catalog(db)
        db.commit()
        policy_service.ensure_standing_authority(db)
    except Exception:  # pragma: no cover - surfaced by /health and first request
        db.rollback()
        _log.exception("demo bootstrap failed")
    finally:
        db.close()


@app.on_event("startup")
def _start_sweeper() -> None:
    if settings.bootstrap_on_startup:
        _bootstrap_demo_state()
    if settings.expiry_sweep_seconds > 0:
        threading.Thread(target=_expiry_sweeper, args=(_sweeper_stop,), daemon=True).start()


@app.on_event("shutdown")
def _stop_sweeper() -> None:
    _sweeper_stop.set()


from pathlib import Path

from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# Paths are resolved from this file, not the working directory, so the app
# finds its assets however it is launched (local, Railway, tests).
_ROOT = Path(__file__).resolve().parent.parent
app.mount("/static", StaticFiles(directory=_ROOT / "static"), name="static")
templates = Jinja2Templates(directory=_ROOT / "templates")


@app.get("/", include_in_schema=False)
def index(request: Request):
    """Consumer-facing homepage."""
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/payments", include_in_schema=False)
def payments_page(request: Request):
    return templates.TemplateResponse("payments.html", {"request": request})


@app.get("/agent", include_in_schema=False)
def agent_page(request: Request):
    return templates.TemplateResponse("agent.html", {"request": request})


@app.get("/activity", include_in_schema=False)
def activity_page(request: Request):
    return templates.TemplateResponse("activity.html", {"request": request})


@app.get("/orders/{order_ref}", include_in_schema=False)
def receipt_page(request: Request, order_ref: str):
    """Order confirmation / receipt (data loaded from /product/orders/{ref})."""
    return templates.TemplateResponse("receipt.html", {"request": request, "order_ref": order_ref})


@app.get("/transactions/{task_id}", include_in_schema=False)
def transaction_page(request: Request, task_id: str):
    """Unified receipt for any transaction (data from /product/transactions/{id})."""
    return templates.TemplateResponse("transaction.html", {"request": request, "task_id": task_id})


@app.get("/drunix", include_in_schema=False)
def drunix_page(request: Request):
    """Drunix ledger: live enforcement status, transactions, reconciliation, Security Lab."""
    return templates.TemplateResponse("drunix.html", {"request": request})


@app.get("/security", include_in_schema=False)
def security_page(request: Request):
    """Technical Security Center — the old dashboard, now at /security."""
    return templates.TemplateResponse("security.html", {"request": request})


from app.exceptions import STATUS_BY_EXCEPTION as _STATUS_BY_EXCEPTION  # noqa: E402


@app.exception_handler(AgentGuardError)
def handle_agentguard_error(request: Request, exc: AgentGuardError) -> JSONResponse:
    status_code = _STATUS_BY_EXCEPTION.get(type(exc), 400)

    content = {"error": type(exc).__name__, "detail": str(exc)}

    # If the exception carries a RiskResult (Day 5), include its structured fields
    if hasattr(exc, "risk_result") and exc.risk_result:
        risk = exc.risk_result
        content.update({
            "risk_level": risk.risk_level.value,
            "action": risk.action.value,
            "anomaly_score": risk.anomaly_score,
            "reason_codes": [rc.value for rc in risk.reason_codes],
            "reasons": risk.reasons,
            "capability_id": risk.capability_id,
        })

    return JSONResponse(
        status_code=status_code,
        content=content,
    )


@app.get("/health")
def health():
    """Liveness + a REAL database round-trip. Returns 503 if the database is
    unreachable. Reports which database is in use (no credentials)."""
    from sqlalchemy import text
    from app.database import engine
    from app.services.risk_engine import get_risk_engine

    body = {"status": "ok", "database": "ok", "version": app.version}
    details = {"dialect": engine.dialect.name}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1")).scalar()
            details["server_version"] = conn.execute(text("SHOW server_version")).scalar()
            details["name"] = conn.execute(text("SELECT current_database()")).scalar()
            try:
                details["alembic_revision"] = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:
                details["alembic_revision"] = None
    except Exception as exc:
        body.update({"status": "degraded", "database": "unreachable"})
        details["error"] = type(exc).__name__
        body["database_details"] = details
        return JSONResponse(status_code=503, content=body)
    body["database_details"] = details
    body["risk_model_loaded"] = get_risk_engine().is_loaded
    from app.security.keys import key_backend
    body["agent_key_backend"] = key_backend()
    # Drunix: reported, never faked. In enforce mode an unreachable ledger
    # degrades health because payments cannot be authorized without it.
    from app.services import ledger_sync
    from app.services.drunix_client import get_client
    drunix_info = {"mode": "enforce" if ledger_sync.enabled() else "off"}
    if ledger_sync.enabled():
        h = get_client().health() if settings.drunix_bridge_url else {"status": "not_configured"}
        drunix_info.update({"bridge": h.get("bridge", "unreachable"), "network": h.get("drunix", "unknown"),
                            "chaincode_status": h.get("chaincode_status", "unknown"),
                            "connected": h.get("status") == "ok" and h.get("chaincode_status") == "ready"})
        if not drunix_info["connected"]:
            body["status"] = "degraded"
    body["drunix"] = drunix_info
    return body
