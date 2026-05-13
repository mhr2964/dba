"""season_records, streak columns on standings_cache, injuries backfill columns

Revision ID: 012
Revises: 011
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE season_records (
            id          SERIAL PRIMARY KEY,
            league_id   INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season      INT NOT NULL,
            record_type TEXT NOT NULL,
            value       REAL NOT NULL,
            player_id   INT REFERENCES players(id),
            team_id     INT REFERENCES teams(id),
            game_id     INT REFERENCES games(id),
            set_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(league_id, season, record_type)
        )
    """)
    op.execute(
        "CREATE INDEX idx_season_records_league_season "
        "ON season_records (league_id, season)"
    )

    op.execute(
        "ALTER TABLE standings_cache "
        "ADD COLUMN IF NOT EXISTS win_streak SMALLINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE standings_cache "
        "ADD COLUMN IF NOT EXISTS loss_streak SMALLINT NOT NULL DEFAULT 0"
    )

    # Injuries table (009) was missing several columns required by the sim layer.
    # Add them idempotently so this migration is safe to run on a DB where 009
    # already exists without the columns.
    op.execute(
        "ALTER TABLE injuries "
        "ADD COLUMN IF NOT EXISTS team_id INT REFERENCES teams(id)"
    )
    op.execute(
        "ALTER TABLE injuries "
        "ADD COLUMN IF NOT EXISTS incurred_in_game_id INT REFERENCES games(id)"
    )
    op.execute(
        "ALTER TABLE injuries "
        "ADD COLUMN IF NOT EXISTS start_date DATE"
    )
    op.execute(
        "ALTER TABLE injuries "
        "ADD COLUMN IF NOT EXISTS return_date DATE"
    )
    op.execute(
        "ALTER TABLE injuries "
        "ADD COLUMN IF NOT EXISTS affects_progression BOOL NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE injuries DROP COLUMN IF EXISTS affects_progression")
    op.execute("ALTER TABLE injuries DROP COLUMN IF EXISTS return_date")
    op.execute("ALTER TABLE injuries DROP COLUMN IF EXISTS start_date")
    op.execute("ALTER TABLE injuries DROP COLUMN IF EXISTS incurred_in_game_id")
    op.execute("ALTER TABLE injuries DROP COLUMN IF EXISTS team_id")
    op.execute("ALTER TABLE standings_cache DROP COLUMN IF EXISTS loss_streak")
    op.execute("ALTER TABLE standings_cache DROP COLUMN IF EXISTS win_streak")
    op.execute("DROP TABLE IF EXISTS season_records")
