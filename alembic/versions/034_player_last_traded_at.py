"""Add last_traded_at to players for 60-sim-day trade restriction.

A player who was just traded cannot be traded again for 60 sim-days. If fewer
than 60 sim-days remain before the trade deadline, they cannot be traded for
the rest of the regular season. NULL means never traded (no restriction).

Revision ID: 034
Revises: 033
Create Date: 2026-05-17
"""
from typing import Sequence, Union
from alembic import op

revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_traded_at DATE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_players_last_traded_at "
        "ON players (league_id, last_traded_at) WHERE last_traded_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_players_last_traded_at")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS last_traded_at")
