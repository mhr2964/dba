"""Add pending_progression_season to leagues

run_rollover increments leagues.current_season before setting
current_phase=PROGRESSION_PENDING. The commissioner's later /offseason
progression command was reading current_season (the already-incremented
value) to filter that season's games/injuries, which don't exist yet under
the new season number -- silently suppressing low-minutes penalties and
season-ending-injury setbacks. This column carries the pre-increment season
across the rollover -> progression handoff so progression filters on the
season that actually just finished.

Revision ID: 047
Revises: 046
Create Date: 2026-07-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "047"
down_revision: Union[str, None] = "046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "leagues",
        sa.Column(
            "pending_progression_season",
            sa.Integer(),
            nullable=True,
            comment="Pre-increment season set by run_rollover; consumed and cleared by /offseason progression",
        ),
    )


def downgrade() -> None:
    op.drop_column("leagues", "pending_progression_season")
