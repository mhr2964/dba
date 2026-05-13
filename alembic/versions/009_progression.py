"""injuries, progression_log

Revision ID: 009
Revises: 008
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE injuries (
            id              SERIAL PRIMARY KEY,
            league_id       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id       INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            season          INT NOT NULL,
            severity        TEXT NOT NULL,
            games_missed    INT NOT NULL DEFAULT 0,
            incurred_game   INT REFERENCES games(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX idx_injuries_league_season ON injuries (league_id, season)")
    op.execute("CREATE INDEX idx_injuries_player ON injuries (player_id)")

    op.execute("""
        CREATE TABLE progression_log (
            id               SERIAL PRIMARY KEY,
            league_id        INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season_completed INT NOT NULL,
            player_id        INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            attribute        TEXT NOT NULL,
            before_value     SMALLINT NOT NULL,
            after_value      SMALLINT NOT NULL,
            reason           TEXT NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX idx_progression_log_league_season "
        "ON progression_log (league_id, season_completed)"
    )
    op.execute(
        "CREATE INDEX idx_progression_log_player_season "
        "ON progression_log (player_id, season_completed)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS progression_log")
    op.execute("DROP TABLE IF EXISTS injuries")
