"""Add signed_at timestamp to contracts for 60-day trade restriction.

A player cannot be traded within 60 sim days (~60 games) of signing their
contract. signed_at captures when the contract was created so the CPU trade
engine can skip recently-signed players.

Existing rows get signed_at = NOW() as a safe default (treated as "just signed"
only for the initial deployment window, then irrelevant once a full season
completes).

Revision ID: 032
Revises: 031
Create Date: 2026-05-17
"""
from typing import Sequence, Union
from alembic import op

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add signed_at; default NOW() so existing active contracts are stamped
    # conservatively. The CPU trade engine treats NULL as "no restriction".
    op.execute(
        "ALTER TABLE contracts ADD COLUMN IF NOT EXISTS signed_at TIMESTAMPTZ"
    )
    # Back-fill existing active contracts with a sentinel far in the past so
    # they are not blocked from trading on first deployment.
    op.execute(
        "UPDATE contracts SET signed_at = '2000-01-01'::timestamptz WHERE signed_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_contracts_signed_at "
        "ON contracts (league_id, signed_at) WHERE is_active = TRUE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_contracts_signed_at")
    op.execute("ALTER TABLE contracts DROP COLUMN IF EXISTS signed_at")
