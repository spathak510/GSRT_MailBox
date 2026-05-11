"""create processed_emails table

Revision ID: 20260316_0001
Revises: 
Create Date: 2026-03-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260316_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_emails",
        sa.Column("email_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("folder", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("processed_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processed_emails")
