"""player tendency columns and player_directives table

Revision ID: 018
Revises: 017
Create Date: 2026-05-13

"""
from typing import Sequence, Union
from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- tendency columns on players ---
    # All INT with NOT NULL + DEFAULT so existing rows are backfilled immediately;
    # no lock beyond the ALTER itself on Postgres (safe for typical sim-league sizes).
    op.execute("ALTER TABLE players ADD COLUMN tendency_3pt INT NOT NULL DEFAULT 50")
    op.execute("ALTER TABLE players ADD COLUMN tendency_drive INT NOT NULL DEFAULT 50")
    op.execute("ALTER TABLE players ADD COLUMN tendency_mid INT NOT NULL DEFAULT 50")
    op.execute("ALTER TABLE players ADD COLUMN tendency_post INT NOT NULL DEFAULT 20")
    op.execute("ALTER TABLE players ADD COLUMN tendency_pass INT NOT NULL DEFAULT 50")
    op.execute("ALTER TABLE players ADD COLUMN usage_weight INT NOT NULL DEFAULT 50")
    op.execute("ALTER TABLE players ADD COLUMN clutch_rating INT NOT NULL DEFAULT 50")
    op.execute("ALTER TABLE players ADD COLUMN defensive_effort INT NOT NULL DEFAULT 50")

    # --- player_directives table ---
    # One directive row per (league, player); allows per-player coach instructions
    # that override sim defaults without touching the players core record.
    # ON DELETE CASCADE on both FKs: directives are meaningless without the player
    # or league, so clean removal is correct here.
    op.execute("""
        CREATE TABLE player_directives (
            id           SERIAL PRIMARY KEY,
            league_id    INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id    INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            shot_diet    TEXT NOT NULL DEFAULT 'auto',
            usage_mode   TEXT NOT NULL DEFAULT 'normal',
            defense_mode TEXT NOT NULL DEFAULT 'standard',
            role_mode    TEXT NOT NULL DEFAULT 'scorer',
            clutch_mode  TEXT NOT NULL DEFAULT 'normal',
            set_by       BIGINT,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (league_id, player_id)
        )
    """)
    op.execute("CREATE INDEX idx_player_directives_league ON player_directives (league_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS player_directives")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS tendency_3pt")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS tendency_drive")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS tendency_mid")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS tendency_post")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS tendency_pass")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS usage_weight")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS clutch_rating")
    op.execute("ALTER TABLE players DROP COLUMN IF EXISTS defensive_effort")
