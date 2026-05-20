"""player_roles table and teams.coach_philosophy column

player_roles stores the CPU-derived (or human-overridden) offensive/defensive
role assigned to each rostered player for a given league/team/season.  The role
drives touch_share and FGA distribution in the sim engine (Phase 2).

coach_philosophy on teams encodes the CPU manager's decision heuristic.  All
managers default to 'tendency_respecter' for Phase 1; Phase 3 will add variance.

Revision ID: 040
Revises: 039
Create Date: 2026-05-19
"""
from typing import Sequence, Union
from alembic import op

revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE player_roles (
            id              BIGSERIAL PRIMARY KEY,
            league_id       INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id         INTEGER NOT NULL REFERENCES teams(id)   ON DELETE CASCADE,
            season          INTEGER NOT NULL,
            player_id       INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            role            TEXT NOT NULL,
            touch_share     NUMERIC(5,4) NOT NULL,
            rationale       TEXT,
            assigned_by     TEXT NOT NULL DEFAULT 'cpu',
            locked          BOOLEAN NOT NULL DEFAULT FALSE,
            assigned_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (league_id, team_id, season, player_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_player_roles_team_season
            ON player_roles (league_id, team_id, season)
        """
    )
    op.execute(
        """
        ALTER TABLE teams
            ADD COLUMN coach_philosophy TEXT NOT NULL DEFAULT 'tendency_respecter'
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE teams DROP COLUMN IF EXISTS coach_philosophy")
    op.execute("DROP TABLE IF EXISTS player_roles")
