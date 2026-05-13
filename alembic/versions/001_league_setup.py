"""league setup

Revision ID: 001
Revises:
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE leagues (
            id                  SERIAL PRIMARY KEY,
            discord_guild_id    BIGINT UNIQUE NOT NULL,
            name                TEXT NOT NULL,
            start_season_year   INT NOT NULL,
            current_season      INT NOT NULL,
            current_phase       TEXT NOT NULL DEFAULT 'SETUP',
            phase_data          JSONB NOT NULL DEFAULT '{}',
            commissioner_user_id BIGINT NOT NULL,
            fa_day_count        INT NOT NULL DEFAULT 8,
            salary_cap          BIGINT NOT NULL DEFAULT 140000000,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at         TIMESTAMPTZ
        )
    """)

    op.execute("""
        CREATE TABLE teams (
            id                  SERIAL PRIMARY KEY,
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            nba_team_code       TEXT NOT NULL,
            name                TEXT NOT NULL,
            city                TEXT NOT NULL,
            conference          TEXT NOT NULL,
            division            TEXT NOT NULL,
            manager_user_id     BIGINT,
            cpu_mode            TEXT NOT NULL DEFAULT 'developing',
            team_offense_rating INT,
            team_defense_rating INT,
            pace                REAL,
            UNIQUE (league_id, nba_team_code)
        )
    """)
    op.execute("CREATE INDEX idx_teams_league_id ON teams (league_id)")
    op.execute("CREATE INDEX idx_teams_league_manager ON teams (league_id, manager_user_id)")

    op.execute("""
        CREATE TABLE league_channels (
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            channel_role        TEXT NOT NULL,
            discord_channel_id  BIGINT NOT NULL,
            PRIMARY KEY (league_id, channel_role)
        )
    """)

    op.execute("""
        CREATE TABLE league_roles (
            id              SERIAL PRIMARY KEY,
            league_id       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            role_type       TEXT NOT NULL,
            team_id         INT REFERENCES teams(id),
            discord_role_id BIGINT NOT NULL
        )
    """)
    op.execute("CREATE INDEX idx_league_roles_league_id ON league_roles (league_id)")

    op.execute("""
        CREATE TABLE team_manager_history (
            id          SERIAL PRIMARY KEY,
            team_id     INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            user_id     BIGINT,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            removed_at  TIMESTAMPTZ,
            assigned_by BIGINT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS team_manager_history")
    op.execute("DROP TABLE IF EXISTS league_roles")
    op.execute("DROP TABLE IF EXISTS league_channels")
    op.execute("DROP TABLE IF EXISTS teams")
    op.execute("DROP TABLE IF EXISTS leagues")
