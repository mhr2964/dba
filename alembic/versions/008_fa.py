"""fa_state, fa_offers, fa_counters

Revision ID: 008
Revises: 007
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE fa_state (
            league_id   INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE PRIMARY KEY,
            season      INT NOT NULL,
            current_day SMALLINT NOT NULL DEFAULT 0,
            phase       TEXT NOT NULL DEFAULT 'closed',
            total_days  SMALLINT NOT NULL DEFAULT 8
        )
    """)

    op.execute("""
        CREATE TABLE fa_offers (
            id               SERIAL PRIMARY KEY,
            league_id        INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season           INT NOT NULL,
            fa_day           SMALLINT NOT NULL,
            team_id          INT NOT NULL REFERENCES teams(id),
            player_id        INT NOT NULL REFERENCES players(id),
            salary_per_year  BIGINT NOT NULL,
            years            SMALLINT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'submitted',
            submitted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX idx_fa_offers_player_day ON fa_offers (league_id, season, fa_day, player_id)"
    )
    op.execute(
        "CREATE INDEX idx_fa_offers_team ON fa_offers (league_id, season, team_id)"
    )

    op.execute("""
        CREATE TABLE fa_counters (
            id               SERIAL PRIMARY KEY,
            original_offer_id INT NOT NULL REFERENCES fa_offers(id) ON DELETE CASCADE,
            salary_per_year  BIGINT NOT NULL,
            years            SMALLINT NOT NULL,
            team_response    TEXT NOT NULL DEFAULT 'pending',
            responded_at     TIMESTAMPTZ
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fa_counters")
    op.execute("DROP TABLE IF EXISTS fa_offers")
    op.execute("DROP TABLE IF EXISTS fa_state")
