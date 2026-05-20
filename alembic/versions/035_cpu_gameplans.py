"""CPU gameplans table and team_strategies coach_mode/transition/bench columns.

Revision ID: 035
Revises: 034
Create Date: 2026-05-17
"""
from typing import Sequence, Union
from alembic import op

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE game_cpu_gameplans (
            game_id             INT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            team_id             INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            source              TEXT NOT NULL,
            offensive_pace      TEXT NOT NULL,
            offensive_scheme    TEXT NOT NULL,
            defensive_scheme    TEXT NOT NULL,
            defensive_intensity TEXT NOT NULL,
            star_usage          INT NOT NULL,
            player_directives   JSONB NOT NULL DEFAULT '{}',
            rationale           TEXT NOT NULL DEFAULT '',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (game_id, team_id)
        )
    """)
    op.execute("CREATE INDEX ON game_cpu_gameplans (team_id)")

    op.execute(
        "ALTER TABLE team_strategies ADD COLUMN IF NOT EXISTS coach_mode TEXT NOT NULL DEFAULT 'manual' "
        "CHECK (coach_mode IN ('manual', 'auto'))"
    )
    op.execute(
        "ALTER TABLE team_strategies ADD COLUMN IF NOT EXISTS transition_aggression TEXT NOT NULL DEFAULT 'balanced' "
        "CHECK (transition_aggression IN ('crash', 'balanced', 'retreat'))"
    )
    op.execute(
        "ALTER TABLE team_strategies ADD COLUMN IF NOT EXISTS bench_leash TEXT NOT NULL DEFAULT 'normal' "
        "CHECK (bench_leash IN ('short', 'normal', 'long'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE team_strategies DROP COLUMN IF EXISTS bench_leash")
    op.execute("ALTER TABLE team_strategies DROP COLUMN IF EXISTS transition_aggression")
    op.execute("ALTER TABLE team_strategies DROP COLUMN IF EXISTS coach_mode")
    op.execute("DROP TABLE IF EXISTS game_cpu_gameplans")
