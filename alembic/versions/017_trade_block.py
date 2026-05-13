"""trade_block table for player trade block listings

Revision ID: 017
Revises: 016
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE trade_block (
            id SERIAL PRIMARY KEY,
            league_id INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            player_id INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            asking_price BIGINT,
            note TEXT,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(league_id, player_id)
        )
    """)
    op.execute("CREATE INDEX idx_trade_block_league_team ON trade_block(league_id, team_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trade_block")
