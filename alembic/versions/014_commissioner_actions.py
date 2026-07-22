"""commissioner_actions audit log

Revision ID: 014
Revises: 013
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE commissioner_actions (
            id              SERIAL PRIMARY KEY,
            league_id       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            user_id         BIGINT NOT NULL,
            action_type     TEXT NOT NULL,
            target_ref      TEXT,
            detail          TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX idx_commissioner_actions_league "
        "ON commissioner_actions (league_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS commissioner_actions")
