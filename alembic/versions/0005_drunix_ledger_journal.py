"""Drunix integration: journal of ledger transactions (additive only).

Revision ID: 0005_drunix_ledger
Revises: 0004_marketplace
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_drunix_ledger"
down_revision = "0004_marketplace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ledger_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("function", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("tx_id", sa.String(128), nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("validation_code", sa.String(64), nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("code", sa.String(64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ledger_transactions_entity_id", "ledger_transactions", ["entity_id"])
    op.create_index("ix_ledger_transactions_outcome", "ledger_transactions", ["outcome"])
    op.create_index("ix_ledger_transactions_tx_id", "ledger_transactions", ["tx_id"])
    op.create_index("ix_ledger_transactions_created_at", "ledger_transactions", ["created_at"])


def downgrade() -> None:
    op.drop_table("ledger_transactions")
