"""Provision the clean demo state (idempotent wrapper around the scoped reset).

    PYTHONPATH=. python seed.py

Requires an already-migrated database (`alembic upgrade head`). Equivalent to
POST /demo/reset: one demo user with a policy, standing mandate, Main /
Search / Negotiation agents, a Purchase Agent and a wallet key, plus the
simulated marketplace catalogue. Only demo-user data is ever deleted.
The historical Day-1 seed scenario is described in docs/ENGINEERING_LOG.md.
"""
from app import models  # noqa: F401
from app.database import SessionLocal
from app.services import policy_service


def main() -> None:
    db = SessionLocal()
    try:
        deleted = policy_service.reset_demo_data(db)
        policy = policy_service.ensure_standing_authority(db)
        print("Deleted previous demo rows:", deleted or "none")
        print("Clean demo state ready:", policy_service.policy_dict(db, policy)["standing"])
    finally:
        db.close()


if __name__ == "__main__":
    main()
