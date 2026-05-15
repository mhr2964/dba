"""Add all_time_records table for tracking league-lifetime records.

Unlike season_records (per-season), these persist across all seasons in a league.
The UNIQUE constraint on (league_id, record_type) means only one holder per record
type per league exists at a time — SET always replaces the prior record.

Revision ID: 024
Revises: 023
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE all_time_records (
            id          SERIAL PRIMARY KEY,
            league_id   INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            record_type VARCHAR(64) NOT NULL,
            value       FLOAT NOT NULL,
            player_id   INTEGER REFERENCES players(id),
            team_id     INTEGER REFERENCES teams(id),
            game_id     INTEGER REFERENCES games(id),
            season_set  INTEGER,
            set_at      TIMESTAMP DEFAULT NOW(),
            UNIQUE (league_id, record_type)
        )
    """)
    op.execute(
        "CREATE INDEX idx_all_time_records_league "
        "ON all_time_records (league_id, record_type)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_all_time_records_league")
    op.execute("DROP TABLE IF EXISTS all_time_records")
