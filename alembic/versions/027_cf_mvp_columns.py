"""Add cfmvp columns to history_seasons.

Conference Finals MVP (East and West) are computed when each CF series ends and
stored here alongside the already-existing finals_mvp_player_id column.

Revision ID: 027
Revises: 026
Create Date: 2026-05-16
"""
from typing import Sequence, Union
from alembic import op

revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE history_seasons
            ADD COLUMN IF NOT EXISTS cfmvp_east_player_id INT REFERENCES players(id),
            ADD COLUMN IF NOT EXISTS cfmvp_west_player_id INT REFERENCES players(id)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE history_seasons DROP COLUMN IF EXISTS cfmvp_east_player_id")
    op.execute("ALTER TABLE history_seasons DROP COLUMN IF EXISTS cfmvp_west_player_id")
