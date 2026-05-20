"""Add CHECK constraint on teams.coach_philosophy column

Validates that only known philosophy names can be stored.  If a new philosophy
is added to COACH_PHILOSOPHIES in role_service.py, extend this constraint with
a new migration.

Revision ID: 042
Revises: 041
Create Date: 2026-05-19
"""
from typing import Sequence, Union
from alembic import op

revision: str = "042"
down_revision: Union[str, None] = "041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Must match COACH_PHILOSOPHIES in services/role_service.py exactly.
_VALID_PHILOSOPHIES = (
    "star_maxer",
    "egalitarian",
    "defense_first",
    "tendency_respecter",
    "vet_overrater",
    "youth_developer",
    "chaos",
)


def upgrade() -> None:
    in_list = ", ".join(f"'{p}'" for p in _VALID_PHILOSOPHIES)
    op.execute(
        f"""
        ALTER TABLE teams
        ADD CONSTRAINT teams_coach_philosophy_check
        CHECK (coach_philosophy IN ({in_list}))
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE teams DROP CONSTRAINT IF EXISTS teams_coach_philosophy_check"
    )
