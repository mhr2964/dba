"""Add CHECK constraint on player_roles.role column

Prevents garbage role names from being written silently.
The IN list is generated from ROLE_REGISTRY keys in role_service.py — must
match that registry exactly.  If a new role is added to ROLE_REGISTRY, add
a migration to extend this constraint.

Revision ID: 041
Revises: 040
Create Date: 2026-05-19
"""
from typing import Sequence, Union
from alembic import op

revision: str = "041"
down_revision: Union[str, None] = "040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# All 23 roles from ROLE_REGISTRY in role_service.py.
# Keep in sync — add a new migration if ROLE_REGISTRY grows.
_VALID_ROLES = (
    "iso_scorer",
    "primary_initiator",
    "post_anchor",
    "movement_shooter",
    "slashing_lead",
    "secondary_creator",
    "wing_creator",
    "pick_and_pop",
    "spark_plug_scorer",
    "transition_engine",
    "catch_and_shoot",
    "floor_spacer",
    "cutter_finisher",
    "rim_runner",
    "screen_roller",
    "rim_protector",
    "wing_stopper",
    "on_ball_pest",
    "switching_big",
    "two_way_wing",
    "two_way_big",
    "glue_guy",
    "veteran_mentor",
    "developmental",
    "end_of_bench",
)


def upgrade() -> None:
    in_list = ", ".join(f"'{r}'" for r in _VALID_ROLES)
    op.execute(
        f"""
        ALTER TABLE player_roles
        ADD CONSTRAINT player_roles_role_check
        CHECK (role IN ({in_list}))
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE player_roles DROP CONSTRAINT IF EXISTS player_roles_role_check"
    )
