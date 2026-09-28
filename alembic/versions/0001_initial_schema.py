"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-22

NOTE: this migration was hand-written (not produced by `alembic
autogenerate` against a live database, since that wasn't available while
drafting it). Before relying on it, run it against a throwaway Postgres
instance and/or run `alembic check` against the current models to confirm
it matches exactly. If anything drifts, regenerating with autogenerate is
safer than patching this file by hand.

Final hardening pass: enum labels are UPPERCASE because SQLAlchemy's
``Enum(PyEnum)`` persists the enum *member names* (ACTIVE, RESERVED, ...),
not their lowercase values. The original lowercase labels made every insert
fail on a freshly migrated database. ``0003_normalize_enum_labels`` repairs
databases that were already migrated with the old lowercase labels.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    mandate_status = postgresql.ENUM(
        "ACTIVE", "REVOKED", "EXPIRED", "COMPLETED", name="mandate_status",
        create_type=False,
    )
    capability_status = postgresql.ENUM(
        "ACTIVE", "EXHAUSTED", "REVOKED", "EXPIRED", name="capability_status",
        create_type=False,
    )
    reservation_status = postgresql.ENUM(
        "RESERVED", "COMMITTED", "RELEASED", name="reservation_status",
        create_type=False,
    )
    payment_status = postgresql.ENUM(
        "PENDING", "SUCCEEDED", "FAILED", "REFUNDED", name="payment_status",
        create_type=False,
    )
    mandate_status.create(bind, checkfirst=True)
    capability_status.create(bind, checkfirst=True)
    reservation_status.create(bind, checkfirst=True)
    payment_status.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_identifier", sa.String(255), nullable=False, unique=True),
        sa.Column("agent_type", sa.String(64), nullable=False),
        sa.Column(
            "parent_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "mandates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(500), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="INR"),
        sa.Column("total_authority", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", mandate_status, nullable=False, server_default="ACTIVE"),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("total_authority >= 0", name="ck_mandate_total_nonnegative"),
        sa.CheckConstraint("not_after > not_before", name="ck_mandate_window_valid"),
    )

    op.create_table(
        "capabilities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "parent_capability_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("capabilities.id"),
            nullable=True,
        ),
        sa.Column(
            "root_mandate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mandates.id"),
            nullable=False,
        ),
        sa.Column(
            "issued_to_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "issued_by_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id"),
            nullable=True,
        ),
        sa.Column("total_authority", sa.Numeric(14, 2), nullable=False),
        sa.Column("unallocated_authority", sa.Numeric(14, 2), nullable=False),
        sa.Column("reserved_authority", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("committed_authority", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("purpose", sa.String(500), nullable=False),
        sa.Column("category", sa.String(128), nullable=False),
        sa.Column("merchant_allowlist", sa.JSON(), nullable=True),
        sa.Column("merchant_denylist", sa.JSON(), nullable=True),
        sa.Column("delegation_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_delegation_depth", sa.Integer(), nullable=False),
        sa.Column("max_fanout", sa.Integer(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", capability_status, nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("total_authority >= 0", name="ck_capability_total_nonnegative"),
        sa.CheckConstraint(
            "unallocated_authority >= 0", name="ck_capability_unallocated_nonnegative"
        ),
        sa.CheckConstraint(
            "reserved_authority >= 0", name="ck_capability_reserved_nonnegative"
        ),
        sa.CheckConstraint(
            "committed_authority >= 0", name="ck_capability_committed_nonnegative"
        ),
        sa.CheckConstraint(
            "unallocated_authority + reserved_authority + committed_authority <= total_authority",
            name="ck_capability_pool_conservation",
        ),
        sa.CheckConstraint("delegation_depth >= 0", name="ck_capability_depth_nonnegative"),
        sa.CheckConstraint("max_fanout >= 0", name="ck_capability_fanout_nonnegative"),
        sa.CheckConstraint("not_after > not_before", name="ck_capability_window_valid"),
    )
    op.create_index(
        "ix_capabilities_parent_capability_id", "capabilities", ["parent_capability_id"]
    )
    op.create_index("ix_capabilities_root_mandate_id", "capabilities", ["root_mandate_id"])

    op.create_table(
        "reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "capability_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("capabilities.id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", reservation_status, nullable=False, server_default="RESERVED"),
        sa.Column("idempotency_key", sa.String(255), nullable=True, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_reservation_amount_positive"),
    )
    op.create_index("ix_reservations_capability_id", "reservations", ["capability_id"])

    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reservations.id"),
            nullable=False,
        ),
        sa.Column("merchant", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="INR"),
        sa.Column("category", sa.String(128), nullable=False),
        sa.Column("status", payment_status, nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_payment_amount_positive"),
    )
    op.create_index("ix_payments_reservation_id", "payments", ["reservation_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "capability_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("capabilities.id"),
            nullable=True,
        ),
        sa.Column(
            "mandate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mandates.id"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("previous_hash", sa.String(128), nullable=True),
        sa.Column("event_hash", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_capability_id", "audit_logs", ["capability_id"])
    op.create_index("ix_audit_logs_mandate_id", "audit_logs", ["mandate_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("payments")
    op.drop_table("reservations")
    op.drop_table("capabilities")
    op.drop_table("mandates")
    op.drop_table("agents")
    op.drop_table("users")

    bind = op.get_bind()
    postgresql.ENUM(name="payment_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="reservation_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="capability_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="mandate_status").drop(bind, checkfirst=True)
