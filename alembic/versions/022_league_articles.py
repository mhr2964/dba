"""Add league_articles table for columnist/article archive.

Stores AI-generated (or human-written) articles keyed to a league and season.
GIN index on subject_team_ids supports efficient "articles mentioning this team"
queries without a join table.  posted_message_id is nullable — it is backfilled
after the Discord message is actually sent.

Revision ID: 022
Revises: 021
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE league_articles (
            id                  SERIAL PRIMARY KEY,
            league_id           INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season              INTEGER NOT NULL,
            persona_id          TEXT NOT NULL,
            category            TEXT NOT NULL,
            headline            TEXT NOT NULL,
            body                TEXT NOT NULL,
            subject_team_ids    INTEGER[] NOT NULL DEFAULT '{}',
            subject_player_ids  INTEGER[] NOT NULL DEFAULT '{}',
            posted_channel_role TEXT,
            posted_message_id   BIGINT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX idx_articles_league_persona "
        "ON league_articles (league_id, persona_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_articles_league_team "
        "ON league_articles USING GIN (subject_team_ids)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_articles_league_team")
    op.execute("DROP INDEX IF EXISTS idx_articles_league_persona")
    op.execute("DROP TABLE IF EXISTS league_articles")
