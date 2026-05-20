"""Add last_derived_game_index to franchise_plans

Tracks which game index triggered each plan derivation so checkpoint
detection can avoid redundant re-derives within the same window.

Revision ID: 038
Revises: 037
Create Date: 2026-05-19

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "franchise_plans",
        sa.Column(
            "last_derived_game_index",
            sa.Integer(),
            nullable=True,
            comment="game_index at time of last derive; NULL = never derived in a running season",
        ),
    )


def downgrade() -> None:
    op.drop_column("franchise_plans", "last_derived_game_index")
