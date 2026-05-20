"""Add defense_tendency column to players.

Stores a per-player defensive rating derived from actual stl+blk per game
relative to position average, so that high-OVR but poor-defending players
(e.g. Luka) get a low defensive weight in the sim rather than inheriting
the full defense attribute which is pulled from overall OVR.

Revision ID: 029
Revises: 028
Create Date: 2026-05-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS defense_tendency SMALLINT NOT NULL DEFAULT 50"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS defense_tendency")
