"""hall_of_fame table

Revision ID: 013
Revises: 012
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hall_of_fame (
            id                    SERIAL PRIMARY KEY,
            league_id             INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id             INT NOT NULL REFERENCES players(id),
            inducted_season       INT NOT NULL,
            career_seasons        INT NOT NULL,
            career_championships  INT NOT NULL DEFAULT 0,
            career_mvp_votes      INT NOT NULL DEFAULT 0,
            career_overall_peak   INT NOT NULL,
            induction_reason      TEXT NOT NULL,
            UNIQUE(league_id, player_id)
        )
    """)
    op.execute(
        "CREATE INDEX idx_hof_league_season "
        "ON hall_of_fame (league_id, inducted_season)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS hall_of_fame")
