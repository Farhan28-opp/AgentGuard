"""normalize enum labels to the names SQLAlchemy persists

Revision ID: 0003_enum_labels
Revises: 184744da11ad
Create Date: 2026-09-26

SQLAlchemy's ``Enum(PyEnum)`` stores enum member *names* (``ACTIVE``,
``RESERVED`` ...). The original 0001 migration declared lowercase labels
(``active``, ``reserved`` ...), so on a database built purely with
``alembic upgrade head`` every insert failed with
``invalid input value for enum``. 0001 now declares uppercase labels; this
revision repairs databases that were already migrated with the old ones.
It is a no-op when the labels are already uppercase.
"""
from alembic import op
from sqlalchemy import text

revision = "0003_enum_labels"
down_revision = "184744da11ad"
branch_labels = None
depends_on = None

_ENUMS = {
    "mandate_status": ["active", "revoked", "expired", "completed"],
    "capability_status": ["active", "exhausted", "revoked", "expired"],
    "reservation_status": ["reserved", "committed", "released"],
    "payment_status": ["pending", "succeeded", "failed", "refunded"],
}

_DEFAULTS = [
    ("mandates", "mandate_status", "ACTIVE"),
    ("capabilities", "capability_status", "ACTIVE"),
    ("reservations", "reservation_status", "RESERVED"),
    ("payments", "payment_status", "PENDING"),
]


def _labels(bind, type_name):
    rows = bind.execute(
        text(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = :n"
        ),
        {"n": type_name},
    )
    return {r[0] for r in rows}


def upgrade() -> None:
    bind = op.get_bind()
    for type_name, lower_labels in _ENUMS.items():
        existing = _labels(bind, type_name)
        for label in lower_labels:
            if label in existing and label.upper() not in existing:
                op.execute(
                    f"ALTER TYPE {type_name} RENAME VALUE '{label}' TO '{label.upper()}'"
                )
    for table, type_name, default in _DEFAULTS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN status SET DEFAULT '{default}'::{type_name}"
        )


def downgrade() -> None:
    # Intentionally irreversible: lowercase labels are incompatible with the ORM.
    pass
