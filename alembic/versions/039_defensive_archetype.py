"""Add defensive_archetype column to players

Stores the dominant defensive profile derived from split interior/perimeter
defensive scores (see scripts/import_players._compute_defensive_scores).
Values: rim_protector | wing_stopper | on_ball_pest | two_way_big |
        size_defender | non_defender | generalist | NULL (pre-backfill).

Revision ID: 039
Revises: 038
Create Date: 2026-05-19
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "players",
        sa.Column(
            "defensive_archetype",
            sa.Text(),
            nullable=True,
            comment=(
                "Dominant defensive profile: rim_protector, wing_stopper, "
                "on_ball_pest, two_way_big, size_defender, non_defender, generalist. "
                "NULL until backfill_overall_from_defense.py has run."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("players", "defensive_archetype")
