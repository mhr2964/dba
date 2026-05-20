"""Add per-quarter score columns to the games table.

Quarter splits enable line-score display and Pat Chen enrichment without
re-querying box scores at read time.  All columns are NULLABLE so existing
rows (scheduled or pre-migration simmed games) remain valid.

Revision ID: 025
Revises: 024
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for col in (
        "q1_home", "q1_away",
        "q2_home", "q2_away",
        "q3_home", "q3_away",
        "q4_home", "q4_away",
        "ot_home", "ot_away",
    ):
        op.add_column("games", sa.Column(col, sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in (
        "q1_home", "q1_away",
        "q2_home", "q2_away",
        "q3_home", "q3_away",
        "q4_home", "q4_away",
        "ot_home", "ot_away",
    ):
        op.drop_column("games", col)
