"""Activity: hide a transaction from the consumer history (presentation only).

Additive and nullable: nothing is deleted or rewritten. A hidden transaction's
payment, order, Drunix journal and audit events are unchanged.

Revision ID: 0006_hidden_at
Revises: 0005_drunix_ledger
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_hidden_at"
down_revision = "0005_drunix_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_tasks", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_tasks", "hidden_at")
