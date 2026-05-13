"""waiver_claims table for priority-based waiver system

Revision ID: 016
Revises: 015
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE waiver_claims (
            id SERIAL PRIMARY KEY,
            league_id INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            team_id INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(league_id, player_id, team_id)
        )
    """)
    op.execute("CREATE INDEX idx_waiver_claims_league_player ON waiver_claims(league_id, player_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS waiver_claims")
