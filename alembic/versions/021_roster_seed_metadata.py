"""Add seed_season and seed_source columns to players for roster reconciliation.

Tracks which season's seed file a player row came from (seed_season) and how
the row was introduced into the league (seed_source).  Both columns are NULL on
existing rows; the reconciler treats NULL seed_source as 'api' at read time so
no backfill is needed and the migration is safe to run on a live table.

Revision ID: 021
Revises: 020
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both columns are nullable — no backfill required, no table lock beyond the
    # lightweight metadata-only ADD COLUMN on Postgres 11+.
    op.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS seed_season INT NULL")
    op.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS seed_source TEXT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS seed_source")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS seed_season")
