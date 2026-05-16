"""Add per-player stat tendency columns (blk/stl/ast/reb).

These drive proportional stat distribution in the sim engine so high-assist
players like Jokić get realistic AST shares instead of a flat attribute split.
Values are scaled 5–95 relative to position-average (50 = league average for
that position group).

Revision ID: 026
Revises: 025
Create Date: 2026-05-16

"""
from typing import Sequence, Union

from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS blk_tendency SMALLINT NOT NULL DEFAULT 50"
    )
    op.execute(
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS stl_tendency SMALLINT NOT NULL DEFAULT 50"
    )
    op.execute(
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS ast_tendency SMALLINT NOT NULL DEFAULT 50"
    )
    op.execute(
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS reb_tendency SMALLINT NOT NULL DEFAULT 50"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS blk_tendency")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS stl_tendency")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS ast_tendency")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS reb_tendency")
