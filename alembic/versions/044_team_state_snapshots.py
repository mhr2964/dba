"""team_state_snapshots table — captures team posture/plan/philosophy at batch flush.

Enables the league digest to compute mode-change and plan-pivot DIFFS between
batches rather than just showing the current state.  One row per team per batch
flush; idempotent via UNIQUE + ON CONFLICT DO UPDATE so crash-and-retry is safe.

Revision ID: 044
Revises: 043
Create Date: 2026-05-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "044"
down_revision: Union[str, None] = "043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE team_state_snapshots (
            id              BIGSERIAL PRIMARY KEY,
            league_id       INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id         INTEGER NOT NULL REFERENCES teams(id)   ON DELETE CASCADE,
            season          INTEGER NOT NULL,
            sim_batch_index INTEGER NOT NULL,
            snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            -- Posture data
            mode            TEXT,
            urgency         TEXT,
            projected_wins  INTEGER,
            conf_rank       INTEGER,
            avg_age         NUMERIC(4,1),

            -- Plan + philosophy at snapshot time
            plan_goal       TEXT,
            plan_horizon    INTEGER,
            philosophy      TEXT,

            UNIQUE (league_id, team_id, season, sim_batch_index)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_tss_league_season_batch
            ON team_state_snapshots(league_id, season, sim_batch_index)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tss_league_season_batch")
    op.execute("DROP TABLE IF EXISTS team_state_snapshots")
