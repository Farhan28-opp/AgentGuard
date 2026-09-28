"""Reset demo data from the command line.

    python scripts/reset_demo_data.py                 # scoped: demo-user data only (same as POST /demo/reset)
    python scripts/reset_demo_data.py --wipe-all --yes-delete-everything
                                                      # LOCAL DEV ONLY: empties every AgentGuard table
                                                      # (for databases full of pre-1.0 test debris that is
                                                      # not flagged as demo data), then re-provisions.

Uses DATABASE_URL. Never run --wipe-all against a database you care about.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app import models  # noqa: E402,F401
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.services import policy_service  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wipe-all", action="store_true")
    ap.add_argument("--yes-delete-everything", action="store_true")
    args = ap.parse_args()
    print(f"Database: {engine.url.render_as_string(hide_password=True)}")
    db = SessionLocal()
    try:
        if args.wipe_all:
            if not args.yes_delete_everything:
                print("Refusing: --wipe-all also needs --yes-delete-everything.")
                return 2
            tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
            db.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            db.commit()
            print("All AgentGuard tables emptied (schema and alembic_version kept).")
        counts = policy_service.reset_demo_data(db)
        print("Scoped demo reset done. Deleted:", counts or "nothing")
        print("Clean demo state provisioned: 1 demo user, standing Main/Search/Negotiation agents, Purchase Agent, wallet key.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
