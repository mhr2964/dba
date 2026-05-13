"""games, box scores, standings cache, ready status

Revision ID: 003
Revises: 002
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE games (
            id                  SERIAL PRIMARY KEY,
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season              INT NOT NULL,
            season_type         TEXT NOT NULL DEFAULT 'regular',
            series_id           INT,
            game_index          INT NOT NULL,
            home_team_id        INT NOT NULL REFERENCES teams(id),
            away_team_id        INT NOT NULL REFERENCES teams(id),
            scheduled_date      DATE NOT NULL,
            status              TEXT NOT NULL DEFAULT 'scheduled',
            home_score          INT,
            away_score          INT,
            winner_team_id      INT REFERENCES teams(id),
            is_user_matchup     BOOL NOT NULL DEFAULT FALSE,
            simmed_at           TIMESTAMPTZ,
            rng_seed            BIGINT
        )
    """)
    op.execute("CREATE INDEX idx_games_league_season_status ON games (league_id, season, status)")
    op.execute("CREATE INDEX idx_games_league_season_index ON games (league_id, season, game_index)")
    op.execute(
        "CREATE INDEX idx_games_league_season_user_status "
        "ON games (league_id, season, is_user_matchup, status)"
    )

    op.execute("""
        CREATE TABLE game_box_scores (
            id              SERIAL PRIMARY KEY,
            game_id         INT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            player_id       INT NOT NULL REFERENCES players(id),
            team_id         INT NOT NULL REFERENCES teams(id),
            started         BOOL NOT NULL DEFAULT FALSE,
            minutes         REAL NOT NULL DEFAULT 0,
            points          INT NOT NULL DEFAULT 0,
            rebounds_off    INT NOT NULL DEFAULT 0,
            rebounds_def    INT NOT NULL DEFAULT 0,
            assists         INT NOT NULL DEFAULT 0,
            steals          INT NOT NULL DEFAULT 0,
            blocks          INT NOT NULL DEFAULT 0,
            turnovers       INT NOT NULL DEFAULT 0,
            fouls           INT NOT NULL DEFAULT 0,
            fga             INT NOT NULL DEFAULT 0,
            fgm             INT NOT NULL DEFAULT 0,
            tpa             INT NOT NULL DEFAULT 0,
            tpm             INT NOT NULL DEFAULT 0,
            fta             INT NOT NULL DEFAULT 0,
            ftm             INT NOT NULL DEFAULT 0,
            plus_minus      INT NOT NULL DEFAULT 0
        )
    """)
    op.execute("CREATE INDEX idx_box_scores_game ON game_box_scores (game_id)")
    op.execute("CREATE INDEX idx_box_scores_player ON game_box_scores (player_id)")

    op.execute("""
        CREATE TABLE team_game_stats (
            game_id         INT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            team_id         INT NOT NULL REFERENCES teams(id),
            minutes         REAL NOT NULL DEFAULT 0,
            points          INT NOT NULL DEFAULT 0,
            rebounds_off    INT NOT NULL DEFAULT 0,
            rebounds_def    INT NOT NULL DEFAULT 0,
            assists         INT NOT NULL DEFAULT 0,
            steals          INT NOT NULL DEFAULT 0,
            blocks          INT NOT NULL DEFAULT 0,
            turnovers       INT NOT NULL DEFAULT 0,
            fouls           INT NOT NULL DEFAULT 0,
            fga             INT NOT NULL DEFAULT 0,
            fgm             INT NOT NULL DEFAULT 0,
            tpa             INT NOT NULL DEFAULT 0,
            tpm             INT NOT NULL DEFAULT 0,
            fta             INT NOT NULL DEFAULT 0,
            ftm             INT NOT NULL DEFAULT 0,
            plus_minus      INT NOT NULL DEFAULT 0,
            PRIMARY KEY (game_id, team_id)
        )
    """)

    op.execute("""
        CREATE TABLE standings_cache (
            league_id       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season          INT NOT NULL,
            team_id         INT NOT NULL REFERENCES teams(id),
            wins            INT NOT NULL DEFAULT 0,
            losses          INT NOT NULL DEFAULT 0,
            conference      TEXT NOT NULL,
            division        TEXT NOT NULL,
            conf_wins       INT NOT NULL DEFAULT 0,
            conf_losses     INT NOT NULL DEFAULT 0,
            home_wins       INT NOT NULL DEFAULT 0,
            home_losses     INT NOT NULL DEFAULT 0,
            last_10_wins    INT NOT NULL DEFAULT 0,
            last_10_losses  INT NOT NULL DEFAULT 0,
            last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (league_id, season, team_id)
        )
    """)

    op.execute("""
        CREATE TABLE ready_status (
            league_id       INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id         INT NOT NULL REFERENCES teams(id),
            user_id         BIGINT NOT NULL,
            is_ready        BOOL NOT NULL DEFAULT FALSE,
            last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (league_id, team_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ready_status")
    op.execute("DROP TABLE IF EXISTS standings_cache")
    op.execute("DROP TABLE IF EXISTS team_game_stats")
    op.execute("DROP TABLE IF EXISTS game_box_scores")
    op.execute("DROP TABLE IF EXISTS games")
