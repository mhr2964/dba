"""award_votings, award_votes, award_results

Revision ID: 006
Revises: 005
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE award_votings (
            id          SERIAL PRIMARY KEY,
            league_id   INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season      INT NOT NULL,
            award_type  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            opened_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            closed_at   TIMESTAMPTZ,
            UNIQUE (league_id, season, award_type)
        )
    """)

    op.execute("""
        CREATE TABLE award_votes (
            id              SERIAL PRIMARY KEY,
            voting_id       INT NOT NULL REFERENCES award_votings(id) ON DELETE CASCADE,
            voter_team_id   INT NOT NULL REFERENCES teams(id),
            voter_user_id   BIGINT,
            cpu_profile     TEXT,
            rank            SMALLINT NOT NULL DEFAULT 1,
            player_id       INT NOT NULL REFERENCES players(id),
            UNIQUE (voting_id, voter_team_id)
        )
    """)

    op.execute("""
        CREATE TABLE award_results (
            id          SERIAL PRIMARY KEY,
            voting_id   INT NOT NULL REFERENCES award_votings(id),
            player_id   INT NOT NULL REFERENCES players(id),
            place       SMALLINT NOT NULL DEFAULT 1,
            vote_count  INT NOT NULL DEFAULT 0,
            vote_share  REAL NOT NULL DEFAULT 0
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS award_results")
    op.execute("DROP TABLE IF EXISTS award_votes")
    op.execute("DROP TABLE IF EXISTS award_votings")
