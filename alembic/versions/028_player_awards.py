"""Create player_awards table.

Stores per-player, per-season award records (CF MVP East/West, Finals MVP, and
any future awards wired through player_awards_repo). The UNIQUE constraint on
(player_id, league_id, season, award_type) makes all inserts idempotent via
ON CONFLICT DO NOTHING.

Revision ID: 028
Revises: 027
Create Date: 2026-05-16
"""
from typing import Sequence, Union
from alembic import op

revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS player_awards (
            id         SERIAL PRIMARY KEY,
            player_id  INTEGER NOT NULL REFERENCES players(id)  ON DELETE CASCADE,
            league_id  INTEGER NOT NULL REFERENCES leagues(id)  ON DELETE CASCADE,
            season     INTEGER NOT NULL,
            award_type TEXT    NOT NULL,
            is_winner  BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (player_id, league_id, season, award_type)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_player_awards_player ON player_awards(player_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_player_awards_league_season ON player_awards(league_id, season)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS player_awards")
