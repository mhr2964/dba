"""franchise_plans table for long-term CPU team strategy

Each CPU team gets one plan per season that captures goal (win_now / transition /
rebuild / tank), player categorisation (core / flex / surplus), and asset targets.
The plan is a level above the per-round cpu_mode: it represents the multi-year
direction and will drive trade strategy in Phase 2+.

Revision ID: 037
Revises: 036
Create Date: 2026-05-19

"""
from typing import Sequence, Union
from alembic import op

revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE franchise_plans (
            id                   SERIAL PRIMARY KEY,
            league_id            INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id              INT NOT NULL REFERENCES teams(id)   ON DELETE CASCADE,
            season               INT NOT NULL,
            goal                 TEXT NOT NULL
                                     CHECK (goal IN ('win_now','transition','rebuild','tank')),
            horizon_seasons      INT NOT NULL DEFAULT 2,
            core_player_ids      JSONB NOT NULL DEFAULT '[]',
            flex_player_ids      JSONB NOT NULL DEFAULT '[]',
            surplus_player_ids   JSONB NOT NULL DEFAULT '[]',
            asset_targets        JSONB NOT NULL DEFAULT '[]',
            rationale            TEXT NOT NULL DEFAULT '',
            derived_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            derived_from_record  JSONB DEFAULT '{}',
            UNIQUE (league_id, team_id, season)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_franchise_plans_league_season_team
            ON franchise_plans (league_id, season, team_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS franchise_plans")
