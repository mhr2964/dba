"""history_seasons — completed season records

Revision ID: 010
Revises: 009
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE history_seasons (
            id                              SERIAL PRIMARY KEY,
            league_id                       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season                          INT NOT NULL,
            champion_team_id                INT REFERENCES teams(id),
            mvp_player_id                   INT REFERENCES players(id),
            finals_mvp_player_id            INT REFERENCES players(id),
            regular_season_wins_leader_id   INT REFERENCES teams(id),
            regular_season_losses_leader_id INT REFERENCES teams(id),
            completed_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(league_id, season)
        )
    """)
    op.execute("CREATE INDEX idx_history_seasons_league ON history_seasons (league_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS history_seasons")
