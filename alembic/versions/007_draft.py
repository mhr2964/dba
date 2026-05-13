"""draft_classes, drafts, draft_selections

Revision ID: 007
Revises: 006
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE draft_classes (
            id              SERIAL PRIMARY KEY,
            league_id       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            class_year      INT NOT NULL,
            player_id       INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            mock_rank       SMALLINT,
            is_generated    BOOL NOT NULL DEFAULT FALSE,
            source          TEXT,
            UNIQUE(league_id, class_year, player_id)
        )
    """)
    op.execute("CREATE INDEX idx_draft_classes_league_year ON draft_classes (league_id, class_year)")

    op.execute("""
        CREATE TABLE drafts (
            id                      SERIAL PRIMARY KEY,
            league_id               INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season                  INT NOT NULL,
            lottery_completed_at    TIMESTAMPTZ,
            current_pick_number     SMALLINT NOT NULL DEFAULT 1,
            status                  TEXT NOT NULL DEFAULT 'pending_lottery',
            UNIQUE(league_id, season)
        )
    """)

    op.execute("""
        CREATE TABLE draft_selections (
            id              SERIAL PRIMARY KEY,
            draft_id        INT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
            pick_number     SMALLINT NOT NULL,
            round           SMALLINT NOT NULL,
            team_id         INT NOT NULL REFERENCES teams(id),
            player_id       INT NOT NULL REFERENCES players(id),
            pick_asset_id   INT,
            selected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(draft_id, pick_number)
        )
    """)
    op.execute("CREATE INDEX idx_draft_selections_draft ON draft_selections (draft_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS draft_selections")
    op.execute("DROP TABLE IF EXISTS drafts")
    op.execute("DROP TABLE IF EXISTS draft_classes")
