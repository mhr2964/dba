"""trades, trade_assets, draft_picks

Revision ID: 004
Revises: 003
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE draft_picks (
            id                  SERIAL PRIMARY KEY,
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season              INT NOT NULL,
            round               SMALLINT NOT NULL,
            original_team_id    INT NOT NULL REFERENCES teams(id),
            current_team_id     INT NOT NULL REFERENCES teams(id),
            pick_number         SMALLINT,
            used_for_player_id  INT REFERENCES players(id)
        )
    """)
    op.execute("CREATE INDEX idx_draft_picks_league_season ON draft_picks (league_id, season)")
    op.execute("CREATE INDEX idx_draft_picks_league_current_team ON draft_picks (league_id, current_team_id)")

    op.execute("""
        CREATE TABLE trades (
            id                      SERIAL PRIMARY KEY,
            league_id               INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season                  INT NOT NULL,
            proposer_team_id        INT NOT NULL REFERENCES teams(id),
            counterparty_team_id    INT NOT NULL REFERENCES teams(id),
            status                  TEXT NOT NULL DEFAULT 'pending_counterparty',
            cpu_evaluator_score     REAL,
            cpu_rationale           TEXT,
            commissioner_action_by  BIGINT,
            commissioner_reason     TEXT,
            proposed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at             TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX idx_trades_league_status ON trades (league_id, status)")
    op.execute("CREATE INDEX idx_trades_league_season ON trades (league_id, season)")

    op.execute("""
        CREATE TABLE trade_assets (
            id              SERIAL PRIMARY KEY,
            trade_id        INT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
            from_team_id    INT NOT NULL REFERENCES teams(id),
            to_team_id      INT NOT NULL REFERENCES teams(id),
            asset_type      TEXT NOT NULL,
            player_id       INT REFERENCES players(id),
            pick_id         INT
        )
    """)
    op.execute("CREATE INDEX idx_trade_assets_trade ON trade_assets (trade_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trade_assets")
    op.execute("DROP TABLE IF EXISTS trades")
    op.execute("DROP TABLE IF EXISTS draft_picks")
