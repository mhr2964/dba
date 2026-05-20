"""Add context_signals JSONB column to trades table

Persists the per-player context signals that drove a CPU trade decision.
Enables /trade why <id> (per-trade breakdown) and /trade signals <team>
(aggregate signal patterns).  Column is nullable — human-to-human trades
and pre-migration trades stay NULL.

Revision ID: 043
Revises: 042
Create Date: 2026-05-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "043"
down_revision: Union[str, None] = "042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("context_signals", postgresql.JSONB(), nullable=True),
    )
    # Partial index: only index rows that actually have signals.
    # Pre-migration rows (NULL) are excluded automatically, keeping the index small.
    op.create_index(
        "idx_trades_has_context_signals",
        "trades",
        ["context_signals"],
        postgresql_where=sa.text("context_signals IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_trades_has_context_signals", table_name="trades")
    op.drop_column("trades", "context_signals")
