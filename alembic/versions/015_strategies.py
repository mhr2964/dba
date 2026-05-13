"""team_strategies, player_minutes, contract_extensions

Revision ID: 015
Revises: 014
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE team_strategies (
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id             INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            offensive_pace      TEXT NOT NULL DEFAULT 'balanced',
            offensive_scheme    TEXT NOT NULL DEFAULT 'balanced',
            defensive_scheme    TEXT NOT NULL DEFAULT 'man_to_man',
            defensive_intensity TEXT NOT NULL DEFAULT 'normal',
            star_usage          INT NOT NULL DEFAULT 50,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (league_id, team_id)
        )
    """)

    op.execute("""
        CREATE TABLE player_minutes (
            league_id       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id         INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            player_id       INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            target_minutes  SMALLINT NOT NULL DEFAULT 0,
            PRIMARY KEY (league_id, team_id, player_id)
        )
    """)

    op.execute("""
        CREATE TABLE contract_extensions (
            id                      SERIAL PRIMARY KEY,
            league_id               INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id               INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            team_id                 INT NOT NULL REFERENCES teams(id),
            new_salary              BIGINT NOT NULL,
            new_years               INT NOT NULL,
            signed_in_season        INT NOT NULL,
            activates_after_season  INT NOT NULL,
            UNIQUE (league_id, player_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS contract_extensions")
    op.execute("DROP TABLE IF EXISTS player_minutes")
    op.execute("DROP TABLE IF EXISTS team_strategies")
