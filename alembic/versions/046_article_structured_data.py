"""Add structured_data JSONB column to league_articles.

Lets a columnist call site persist machine-readable data alongside the
rendered article text -- e.g. The Power List's team-code-to-rank mapping --
so a later caller (rank-delta tracking) can read real structured data instead
of regex-parsing the LLM's own rendered markdown, which silently degrades to
"everyone is NEW" on any formatting deviation.

Revision ID: 046
Revises: 045
Create Date: 2026-07-24
"""
from typing import Sequence, Union

from alembic import op

revision: str = "046"
down_revision: Union[str, None] = "045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE league_articles ADD COLUMN structured_data JSONB"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE league_articles DROP COLUMN IF EXISTS structured_data"
    )
