"""players, contracts, lineups, rookie_scale

Revision ID: 002
Revises: 001
Create Date: 2026-05-12

"""
from typing import Sequence, Union
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE players (
            id                  SERIAL PRIMARY KEY,
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            external_id         TEXT,
            first_name          TEXT NOT NULL,
            last_name           TEXT NOT NULL,
            position            TEXT NOT NULL,
            height_in           INT,
            weight_lb           INT,
            birth_date          DATE,
            years_pro           INT NOT NULL DEFAULT 0,
            is_rookie           BOOL NOT NULL DEFAULT FALSE,
            team_id             INT REFERENCES teams(id),
            roster_status       TEXT NOT NULL DEFAULT 'active',
            overall             INT NOT NULL,
            speed               INT NOT NULL,
            shooting_2pt        INT NOT NULL,
            shooting_3pt        INT NOT NULL,
            shooting_mid        INT NOT NULL,
            finishing           INT NOT NULL,
            playmaking          INT NOT NULL,
            defense             INT NOT NULL,
            rebounding          INT NOT NULL,
            iq                  INT NOT NULL,
            potential           INT NOT NULL,
            peak_age_start      INT NOT NULL,
            peak_age_end        INT NOT NULL,
            loyalty             INT NOT NULL,
            money_drive         INT NOT NULL,
            win_drive           INT NOT NULL,
            market_pref         TEXT NOT NULL DEFAULT 'neutral',
            star_leverage       INT NOT NULL DEFAULT 30
        )
    """)
    op.execute("CREATE INDEX idx_players_league_team ON players (league_id, team_id)")
    op.execute("CREATE INDEX idx_players_league_status ON players (league_id, roster_status)")
    op.execute("CREATE INDEX idx_players_league_overall ON players (league_id, overall DESC)")

    op.execute("""
        CREATE TABLE contracts (
            id                  SERIAL PRIMARY KEY,
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            player_id           INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            team_id             INT REFERENCES teams(id),
            salary              BIGINT NOT NULL,
            years_remaining     INT NOT NULL,
            total_years         INT NOT NULL,
            contract_type       TEXT NOT NULL,
            signed_in_season    INT NOT NULL,
            is_active           BOOL NOT NULL DEFAULT TRUE,
            terminated_reason   TEXT
        )
    """)
    op.execute(
        "CREATE INDEX idx_contracts_league_team_active "
        "ON contracts (league_id, team_id, is_active)"
    )

    op.execute("""
        CREATE TABLE lineups (
            league_id           INT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            team_id             INT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            is_starter          BOOL NOT NULL,
            slot                SMALLINT NOT NULL,
            player_id           INT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
            set_by              BIGINT,
            PRIMARY KEY (league_id, team_id, slot)
        )
    """)

    op.execute("""
        CREATE TABLE rookie_scale (
            pick_number         SMALLINT NOT NULL,
            year_1_salary       BIGINT NOT NULL,
            year_2_salary       BIGINT NOT NULL,
            year_3_salary       BIGINT NOT NULL,
            year_4_salary       BIGINT NOT NULL,
            PRIMARY KEY (pick_number)
        )
    """)

    # 2024-25 NBA rookie scale approximations (in dollars).
    # Pick 1 ~$10.3M, pick 30 ~$2.0M, interpolated linearly.
    op.execute("""
        INSERT INTO rookie_scale (pick_number, year_1_salary, year_2_salary, year_3_salary, year_4_salary)
        VALUES
            (1,  10306508, 10821833, 11337157, 14858652),
            (2,   9700911,  10185957, 10671002, 13925000),
            (3,   9145524,  9602800, 10060076, 13105500),
            (4,   8633147,  9064804,  9496462, 12330000),
            (5,   8160117,  8568123,  8976128, 11600000),
            (6,   7722917,  8109063,  8495209, 10910000),
            (7,   7319127,  7685083,  8051039, 10265000),
            (8,   6945399,  7292669,  7639939,  9662000),
            (9,   6599445,  6929417,  7259389,  9097000),
            (10,  6279079,  6593033,  6906986,  8571000),
            (11,  5982158,  6281266,  6580374,  8080000),
            (12,  5706614,  5991945,  6277275,  7622000),
            (13,  5450437,  5722959,  5995481,  7195000),
            (14,  5212638,  5473270,  5733902,  6796000),
            (15,  4992269,  5241882,  5491496,  6425000),
            (16,  4787395,  5026765,  5266134,  6078000),
            (17,  4597110,  4826966,  5056821,  5756000),
            (18,  4420537,  4641564,  4862590,  5455000),
            (19,  4256826,  4469667,  4682508,  5174000),
            (20,  4105149,  4310406,  4515664,  4912000),
            (21,  3964702,  4162937,  4361172,  4668000),
            (22,  3834700,  4026435,  4218171,  4441000),
            (23,  3714376,  3900095,  4085814,  4228000),
            (24,  3602982,  3782131,  3961281,  4029000),
            (25,  3499789,  3672778,  3845768,  3842000),
            (26,  3404091,  3571296,  3738500,  3667000),
            (27,  3315200,  3476960,  3638720,  3503000),
            (28,  3232448,  3394070,  3555693,  3348000),
            (29,  3155188,  3312947,  3470707,  3203000),
            (30,  2082678,  2186812,  2290946,  2000000)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lineups")
    op.execute("DROP TABLE IF EXISTS contracts")
    op.execute("DROP TABLE IF EXISTS players")
    op.execute("DROP TABLE IF EXISTS rookie_scale")
