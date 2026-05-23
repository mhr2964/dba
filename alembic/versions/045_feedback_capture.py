"""bot_message_log + feedback_replies tables — capture Discord-reply feedback with full league context.

Anchors every non-ephemeral bot post to its league/sim/entity context so a reply
in Discord can be joined back to "what the bot said about which team at which
sim date" — no scraping required. Reply records reference posts by message_id
(Discord's stable identifier) rather than the internal serial id so a reply
event handler can find its target with a single indexed lookup.

Revision ID: 045
Revises: 044
Create Date: 2026-05-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "045"
down_revision: Union[str, None] = "044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE bot_message_log (
            id                  BIGSERIAL PRIMARY KEY,
            message_id          BIGINT NOT NULL UNIQUE,
            channel_id          BIGINT NOT NULL,
            guild_id            BIGINT NOT NULL,
            league_id           INTEGER REFERENCES leagues(id) ON DELETE CASCADE,
            kind                TEXT NOT NULL,
            game_index          INTEGER,
            sim_date            DATE,
            season              INTEGER,
            subject_team_ids    INTEGER[] NOT NULL DEFAULT '{}',
            subject_player_ids  INTEGER[] NOT NULL DEFAULT '{}',
            subject_trade_id    INTEGER,
            context_blob        JSONB NOT NULL DEFAULT '{}'::jsonb,
            content_preview     TEXT,
            posted_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_bot_msg_message_id ON bot_message_log(message_id)"
    )
    op.execute(
        "CREATE INDEX idx_bot_msg_league_kind "
        "ON bot_message_log(league_id, kind, posted_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_bot_msg_subject_team "
        "ON bot_message_log USING GIN (subject_team_ids)"
    )

    op.execute(
        """
        CREATE TABLE feedback_replies (
            id                  BIGSERIAL PRIMARY KEY,
            bot_message_id      BIGINT NOT NULL REFERENCES bot_message_log(message_id) ON DELETE CASCADE,
            reply_message_id    BIGINT NOT NULL UNIQUE,
            author_id           BIGINT NOT NULL,
            author_name         TEXT NOT NULL,
            reply_text          TEXT NOT NULL,
            attachments         JSONB NOT NULL DEFAULT '[]'::jsonb,
            session_log_path    TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_feedback_bot_msg ON feedback_replies(bot_message_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_feedback_bot_msg")
    op.execute("DROP TABLE IF EXISTS feedback_replies")
    op.execute("DROP INDEX IF EXISTS idx_bot_msg_subject_team")
    op.execute("DROP INDEX IF EXISTS idx_bot_msg_league_kind")
    op.execute("DROP INDEX IF EXISTS idx_bot_msg_message_id")
    op.execute("DROP TABLE IF EXISTS bot_message_log")
