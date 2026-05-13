"""playoffs: series table and games.series_id FK

Revision ID: 005
Revises: 004
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE series (
            id                  SERIAL PRIMARY KEY,
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season              INT NOT NULL,
            round               TEXT NOT NULL,
            high_seed_team_id   INT NOT NULL REFERENCES teams(id),
            low_seed_team_id    INT NOT NULL REFERENCES teams(id),
            wins_high           SMALLINT NOT NULL DEFAULT 0,
            wins_low            SMALLINT NOT NULL DEFAULT 0,
            games_needed        SMALLINT NOT NULL DEFAULT 7,
            winner_team_id      INT REFERENCES teams(id),
            status              TEXT NOT NULL DEFAULT 'active'
        )
    """)
    op.execute("CREATE INDEX idx_series_league_season_round ON series (league_id, season, round)")
    op.execute("CREATE INDEX idx_series_league_season_status ON series (league_id, season, status)")

    # games.series_id column already exists (003_games.py); add the FK now that series table exists
    op.execute(
        "ALTER TABLE games ADD CONSTRAINT fk_games_series_id FOREIGN KEY (series_id) REFERENCES series(id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE games DROP CONSTRAINT IF EXISTS fk_games_series_id")
    op.execute("DROP TABLE IF EXISTS series")
