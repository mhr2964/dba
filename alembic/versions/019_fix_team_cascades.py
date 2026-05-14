"""Add ON DELETE CASCADE/SET NULL to all bare REFERENCES teams(id) FKs.

When a league is deleted, leagues→teams cascades but Postgres may process that
cascade before finishing the leagues→games cascade, leaving game_box_scores and
other tables still referencing the teams that are being deleted.  Fix: give every
bare teams(id) FK a delete rule so the cascade order no longer matters.

Revision ID: 019
Revises: 018
Create Date: 2026-05-13
"""
from typing import Sequence, Union
from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fix(table: str, col: str, nullable: bool) -> None:
    rule = "SET NULL" if nullable else "CASCADE"
    constraint = f"{table}_{col}_fkey"
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{constraint}"')
    op.execute(
        f'ALTER TABLE {table} ADD CONSTRAINT "{constraint}" '
        f"FOREIGN KEY ({col}) REFERENCES teams(id) ON DELETE {rule}"
    )


def upgrade() -> None:
    # --- games ---
    _fix("games", "home_team_id",   nullable=False)
    _fix("games", "away_team_id",   nullable=False)
    _fix("games", "winner_team_id", nullable=True)

    # --- game_box_scores / team_game_stats / standings_cache / ready_status ---
    _fix("game_box_scores",  "team_id", nullable=False)
    _fix("team_game_stats",  "team_id", nullable=False)
    _fix("standings_cache",  "team_id", nullable=False)
    _fix("ready_status",     "team_id", nullable=False)

    # --- players / contracts ---
    _fix("players",   "team_id", nullable=True)
    _fix("contracts", "team_id", nullable=True)

    # --- draft_picks ---
    _fix("draft_picks", "original_team_id", nullable=False)
    _fix("draft_picks", "current_team_id",  nullable=False)

    # --- trades / trade_assets ---
    _fix("trades", "proposer_team_id",     nullable=False)
    _fix("trades", "counterparty_team_id", nullable=False)
    _fix("trade_assets", "from_team_id",   nullable=False)
    _fix("trade_assets", "to_team_id",     nullable=False)

    # --- series (playoffs) ---
    _fix("series", "high_seed_team_id", nullable=False)
    _fix("series", "low_seed_team_id",  nullable=False)
    _fix("series", "winner_team_id",    nullable=True)

    # --- awards ---
    _fix("award_votes", "voter_team_id", nullable=False)

    # --- fa_offers ---
    _fix("fa_offers", "team_id", nullable=False)

    # --- history_seasons ---
    _fix("history_seasons", "champion_team_id",                nullable=True)
    _fix("history_seasons", "regular_season_wins_leader_id",   nullable=True)
    _fix("history_seasons", "regular_season_losses_leader_id", nullable=True)

    # --- season_records / injuries ---
    _fix("season_records", "team_id", nullable=True)
    _fix("injuries",       "team_id", nullable=True)

    # --- contract_extensions ---
    _fix("contract_extensions", "team_id", nullable=False)

    # --- league_roles ---
    _fix("league_roles", "team_id", nullable=True)


def downgrade() -> None:
    # Restore bare FKs (no delete rule) — reverses upgrade in the same order.
    tables_cols_nullable = [
        ("games",               "home_team_id",                   False),
        ("games",               "away_team_id",                   False),
        ("games",               "winner_team_id",                 True),
        ("game_box_scores",     "team_id",                        False),
        ("team_game_stats",     "team_id",                        False),
        ("standings_cache",     "team_id",                        False),
        ("ready_status",        "team_id",                        False),
        ("players",             "team_id",                        True),
        ("contracts",           "team_id",                        True),
        ("draft_picks",         "original_team_id",               False),
        ("draft_picks",         "current_team_id",                False),
        ("trades",              "proposer_team_id",               False),
        ("trades",              "counterparty_team_id",           False),
        ("trade_assets",        "from_team_id",                   False),
        ("trade_assets",        "to_team_id",                     False),
        ("series",              "high_seed_team_id",              False),
        ("series",              "low_seed_team_id",               False),
        ("series",              "winner_team_id",                 True),
        ("award_votes",         "voter_team_id",                  False),
        ("fa_offers",           "team_id",                        False),
        ("history_seasons",     "champion_team_id",               True),
        ("history_seasons",     "regular_season_wins_leader_id",  True),
        ("history_seasons",     "regular_season_losses_leader_id",True),
        ("season_records",      "team_id",                        True),
        ("injuries",            "team_id",                        True),
        ("contract_extensions", "team_id",                        False),
        ("league_roles",        "team_id",                        True),
    ]
    for table, col, _ in tables_cols_nullable:
        constraint = f"{table}_{col}_fkey"
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{constraint}"')
        op.execute(
            f'ALTER TABLE {table} ADD CONSTRAINT "{constraint}" '
            f"FOREIGN KEY ({col}) REFERENCES teams(id)"
        )
