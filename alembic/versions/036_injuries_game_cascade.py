"""injuries.incurred_in_game_id ON DELETE CASCADE

When /season start regenerates the schedule, it deletes existing games for
the league. If any injury rows reference those games via incurred_in_game_id,
the deletion fails with FK violation (constraint added in 012 without an
ON DELETE clause, default NO ACTION). Switch to ON DELETE SET NULL so the
injury record survives (history) but no longer points at the deleted game.

Revision ID: 036
Revises: 035
Create Date: 2026-05-18

"""
from typing import Sequence, Union
from alembic import op

revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE injuries "
        "DROP CONSTRAINT IF EXISTS injuries_incurred_in_game_id_fkey"
    )
    op.execute(
        "ALTER TABLE injuries "
        "ADD CONSTRAINT injuries_incurred_in_game_id_fkey "
        "FOREIGN KEY (incurred_in_game_id) REFERENCES games(id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE injuries "
        "DROP CONSTRAINT IF EXISTS injuries_incurred_in_game_id_fkey"
    )
    op.execute(
        "ALTER TABLE injuries "
        "ADD CONSTRAINT injuries_incurred_in_game_id_fkey "
        "FOREIGN KEY (incurred_in_game_id) REFERENCES games(id)"
    )
