"""Application configuration.

All configuration is read from environment variables (optionally via a
local .env file). Never hardcode credentials here -- see .env.example
for the variables this project expects.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # REQUIRED. No default: the app refuses to start without a PostgreSQL
    # DATABASE_URL instead of silently using a local or in-memory database.
    database_url: str = ""
    app_env: str = "development"
    reservation_ttl_seconds: int = 90
    expiry_sweep_seconds: int = 30  # background release of expired holds; 0 disables
    risk_medium_threshold: float = 0.02
    risk_high_threshold: float = 0.05
    clock_skew_seconds: int = 300  # ±5 minutes for signed request timestamps

    # Behavioural "unusual time" feature. The hour window is evaluated in the
    # user's local timezone (AgentGuard is an INR consumer product), not UTC:
    # the original 08:00-20:00 UTC window flagged every Indian-morning payment.
    risk_timezone: str = "Asia/Kolkata"
    risk_normal_hours_start: int = 6   # inclusive, local hour
    risk_normal_hours_end: int = 23    # exclusive, local hour

    # Agent private keys: file backend (dev_keys/, never packaged) unless
    # AGENT_KEY_SEED is set, in which case keys are derived from that secret
    # (use this on Railway: nothing is written to the ephemeral disk).
    dev_keys_dir: str = ""  # empty -> <project>/dev_keys
    agent_key_seed: str = ""

    # Demo mode enables /demo/* (scoped reset + Security Center scenarios).
    demo_mode: bool = True
    # Upsert the simulated catalogue + demo user's standing authority at startup.
    bootstrap_on_startup: bool = True

    # Drunix enforcement layer (see docs/DRUNIX_INTEGRATION.md).
    #   off     -- AgentGuard behaves exactly as before (PostgreSQL only).
    #   enforce -- delegation, reserve and commit only succeed after the
    #              agentauth chaincode on Drunix accepted them and the
    #              transaction committed VALID; release, return and revoke
    #              are mirrored and journalled as SYNC_PENDING if Drunix is
    #              unavailable.
    drunix_mode: str = "off"
    drunix_bridge_url: str = ""  # required in enforce mode, e.g. the local bridge (see .env.example)
    drunix_bridge_token: str = ""
    # One Drunix transaction = endorse + order (block cut ~2 s) + commit
    # status. Must stay well inside reservation_ttl_seconds.
    drunix_timeout_seconds: float = 45.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def drunix_enforced(self) -> bool:
        return self.drunix_mode.strip().lower() == "enforce"


settings = Settings()


def normalized_database_url(raw: str) -> str:
    """Validate DATABASE_URL and normalise it for SQLAlchemy + psycopg2.

    Accepts postgres://, postgresql:// and postgresql+psycopg2:// (Railway
    and Heroku-style URLs). Anything else — including SQLite — is rejected.
    """
    url = (raw or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. AgentGuard requires PostgreSQL, e.g. "
            "postgresql+psycopg2://user:pass@host:5432/dbname (see .env.example)."
        )
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    if url.startswith("postgresql+psycopg2://"):
        return url
    raise RuntimeError("DATABASE_URL must point to PostgreSQL (postgresql://...).")
