"""reservation day3 fields

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("currency", sa.String(8), nullable=True))
    op.add_column("reservations", sa.Column("merchant", sa.String(255), nullable=True))
    op.add_column("reservations", sa.Column("category", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("reservations", "category")
    op.drop_column("reservations", "merchant")
    op.drop_column("reservations", "currency")
