"""dm_outbox

Revision ID: 011
Revises: 010
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE dm_outbox (
            id                   SERIAL PRIMARY KEY,
            league_id            INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            user_id              BIGINT NOT NULL,
            embed_data           JSONB NOT NULL,
            fallback_channel_role TEXT NOT NULL DEFAULT 'league-news',
            fallback_message     TEXT NOT NULL,
            attempts             SMALLINT NOT NULL DEFAULT 0,
            last_error           TEXT,
            delivered            BOOL NOT NULL DEFAULT FALSE,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX idx_dm_outbox_retry ON dm_outbox (league_id, delivered, attempts)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dm_outbox")
