"""Add last_potm_year_month to leagues table.

Tracks when the Player of the Month award was last given so potm_service
can detect when a new month has been crossed during batch simulation and
award per-conference POTM honours without double-firing.

Format: "YYYY-MM" (e.g. "2024-10").  NULL means the award has never run.

Revision ID: 023
Revises: 022
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leagues",
        sa.Column("last_potm_year_month", sa.String(7), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("leagues", "last_potm_year_month")
