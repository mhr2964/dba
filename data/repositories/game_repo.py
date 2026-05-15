from __future__ import annotations

import datetime
from typing import List, Optional

import asyncpg

from core.logging import get_logger

log = get_logger(__name__)


async def insert_game(pool: asyncpg.Pool, game_data: dict) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO games (
            league_id, season, season_type, series_id, game_index,
            home_team_id, away_team_id, scheduled_date,
            status, is_user_matchup, rng_seed
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        game_data["league_id"],
        game_data["season"],
        game_data.get("season_type", "regular"),
        game_data.get("series_id"),
        game_data["game_index"],
        game_data["home_team_id"],
        game_data["away_team_id"],
        game_data["scheduled_date"],
        game_data.get("status", "scheduled"),
        game_data.get("is_user_matchup", False),
        game_data.get("rng_seed"),
    )
    return row["id"]


async def insert_game_batch(pool: asyncpg.Pool, games: List[dict]) -> None:
    """Bulk-insert a list of game dicts using COPY for efficiency."""
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO games (
                league_id, season, season_type, game_index,
                home_team_id, away_team_id, scheduled_date,
                status, is_user_matchup, rng_seed
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            [
                (
                    g["league_id"],
                    g["season"],
                    g.get("season_type", "regular"),
                    g["game_index"],
                    g["home_team_id"],
                    g["away_team_id"],
                    g["scheduled_date"],
                    g.get("status", "scheduled"),
                    g.get("is_user_matchup", False),
                    g.get("rng_seed"),
                )
                for g in games
            ],
        )


async def insert_box_scores(pool: asyncpg.Pool, game_id: int, box_lines: List[dict]) -> None:
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO game_box_scores (
                game_id, player_id, team_id, started, minutes,
                points, rebounds_off, rebounds_def, assists,
                steals, blocks, turnovers, fouls,
                fga, fgm, tpa, tpm, fta, ftm, plus_minus
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9,
                $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20
            )
            """,
            [
                (
                    game_id,
                    line["player_id"],
                    line["team_id"],
                    line["started"],
                    line["minutes"],
                    line["points"],
                    line["rebounds_off"],
                    line["rebounds_def"],
                    line["assists"],
                    line["steals"],
                    line["blocks"],
                    line["turnovers"],
                    line["fouls"],
                    line["fga"],
                    line["fgm"],
                    line["tpa"],
                    line["tpm"],
                    line["fta"],
                    line["ftm"],
                    line["plus_minus"],
                )
                for line in box_lines
            ],
        )


async def insert_team_game_stats(pool: asyncpg.Pool, game_id: int, team_id: int, stats: dict) -> None:
    await pool.execute(
        """
        INSERT INTO team_game_stats (
            game_id, team_id, minutes, points,
            rebounds_off, rebounds_def, assists,
            steals, blocks, turnovers, fouls,
            fga, fgm, tpa, tpm, fta, ftm, plus_minus
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9,
            $10, $11, $12, $13, $14, $15, $16, $17, $18
        )
        """,
        game_id,
        team_id,
        stats.get("minutes", 240.0),
        stats["points"],
        stats.get("rebounds_off", 0),
        stats.get("rebounds_def", 0),
        stats.get("assists", 0),
        stats.get("steals", 0),
        stats.get("blocks", 0),
        stats.get("turnovers", 0),
        stats.get("fouls", 0),
        stats.get("fga", 0),
        stats.get("fgm", 0),
        stats.get("tpa", 0),
        stats.get("tpm", 0),
        stats.get("fta", 0),
        stats.get("ftm", 0),
        stats.get("plus_minus", 0),
    )


async def get_next_scheduled(pool: asyncpg.Pool, league_id: int, season: int) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM games
        WHERE league_id = $1 AND season = $2 AND status = 'scheduled'
        ORDER BY game_index ASC
        LIMIT 1
        """,
        league_id,
        season,
    )
    return dict(row) if row else None


async def get_user_matchup_ahead(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    from_index: int,
) -> Optional[dict]:
    row = await pool.fetchrow(
        """
        SELECT * FROM games
        WHERE league_id = $1
          AND season = $2
          AND is_user_matchup = TRUE
          AND status = 'scheduled'
          AND game_index > $3
        ORDER BY game_index ASC
        LIMIT 1
        """,
        league_id,
        season,
        from_index,
    )
    return dict(row) if row else None


async def get_games_in_range(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    from_index: int,
    to_index: int,
) -> List[dict]:
    rows = await pool.fetch(
        """
        SELECT * FROM games
        WHERE league_id = $1
          AND season = $2
          AND game_index >= $3
          AND game_index <= $4
        ORDER BY game_index ASC
        """,
        league_id,
        season,
        from_index,
        to_index,
    )
    return [dict(r) for r in rows]


async def mark_simmed(
    pool: asyncpg.Pool,
    game_id: int,
    home_score: int,
    away_score: int,
    winner_id: int,
    seed: int,
) -> None:
    await pool.execute(
        """
        UPDATE games
        SET status = 'simmed',
            home_score = $2,
            away_score = $3,
            winner_team_id = $4,
            rng_seed = $5,
            simmed_at = NOW()
        WHERE id = $1
        """,
        game_id,
        home_score,
        away_score,
        winner_id,
        seed,
    )


async def get_current_index(pool: asyncpg.Pool, league_id: int, season: int) -> int:
    val = await pool.fetchval(
        """
        SELECT COALESCE(MAX(game_index), 0) FROM games
        WHERE league_id = $1 AND season = $2 AND status = 'simmed'
        """,
        league_id,
        season,
    )
    return val or 0


async def insert_injuries(pool: asyncpg.Pool, injuries: list[dict]) -> None:
    """Batch insert injury rows. injuries is a list of fully-formed dicts."""
    if not injuries:
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO injuries (
                league_id, season, player_id, team_id, severity,
                games_missed, incurred_in_game_id,
                start_date, return_date, affects_progression
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            [
                (
                    row["league_id"],
                    row["season"],
                    row["player_id"],
                    row["team_id"],
                    row["severity"],
                    row["games_missed"],
                    row["incurred_in_game_id"],
                    row["start_date"],
                    row["return_date"],
                    row["affects_progression"],
                )
                for row in injuries
            ],
        )


async def is_back_to_back(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    team_id: int,
    game_date,
) -> bool:
    """Returns True if team_id played a game the day before game_date."""
    if isinstance(game_date, datetime.date):
        prev_date = game_date - datetime.timedelta(days=1)
    else:
        prev_date = game_date
    row = await pool.fetchrow(
        """
        SELECT 1 FROM games
        WHERE league_id = $1 AND season = $2
          AND status = 'simmed'
          AND scheduled_date = $3
          AND (home_team_id = $4 OR away_team_id = $4)
        """,
        league_id,
        season,
        prev_date,
        team_id,
    )
    return row is not None


_NOTABLE_STREAK_LENGTHS = frozenset({5, 10, 15})


async def update_standings(pool: asyncpg.Pool, league_id: int, season: int, game_result: dict) -> dict:
    """
    game_result keys: game_id, home_team_id, away_team_id, winner_team_id,
                      home_conference, away_conference, home_score, away_score,
                      is_home_win (bool)
    Updates standings_cache for both teams. last_10 is a rolling window tracked
    via (wins + losses) mod 10 — we increment the appropriate bucket and decrement
    the other if the window is full (10 games played).

    Returns dict with key 'notable_streak' -> (team_id, streak_length) | None.
    """
    home_win = game_result["winner_team_id"] == game_result["home_team_id"]
    same_conf = game_result["home_conference"] == game_result["away_conference"]

    notable_streak: Optional[tuple[int, int]] = None

    async with pool.acquire() as conn:
        async with conn.transaction():
            for is_home in (True, False):
                team_id = game_result["home_team_id"] if is_home else game_result["away_team_id"]
                won = home_win if is_home else not home_win
                conference = game_result["home_conference"] if is_home else game_result["away_conference"]
                division = game_result["home_division"] if is_home else game_result["away_division"]

                row = await conn.fetchrow(
                    "SELECT * FROM standings_cache WHERE league_id = $1 AND season = $2 AND team_id = $3",
                    league_id,
                    season,
                    team_id,
                )

                if row is None:
                    await conn.execute(
                        """
                        INSERT INTO standings_cache
                            (league_id, season, team_id, conference, division)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        league_id,
                        season,
                        team_id,
                        conference,
                        division,
                    )
                    row = await conn.fetchrow(
                        "SELECT * FROM standings_cache WHERE league_id = $1 AND season = $2 AND team_id = $3",
                        league_id,
                        season,
                        team_id,
                    )

                games_played = row["wins"] + row["losses"]
                l10_wins = row["last_10_wins"]
                l10_losses = row["last_10_losses"]

                if games_played >= 10:
                    # Window is full — the oldest game falls off. We can't know
                    # what the oldest result was without a results buffer, so we
                    # approximate by decrementing the majority bucket by 1 when
                    # window overflows. This keeps the sum at 10.
                    if l10_wins >= l10_losses:
                        l10_wins = max(0, l10_wins - 1)
                    else:
                        l10_losses = max(0, l10_losses - 1)

                if won:
                    l10_wins += 1
                else:
                    l10_losses += 1

                current_win_streak = row["win_streak"]
                current_loss_streak = row["loss_streak"]
                if won:
                    new_win_streak = current_win_streak + 1
                    new_loss_streak = 0
                    if new_win_streak in _NOTABLE_STREAK_LENGTHS:
                        notable_streak = (team_id, new_win_streak)
                else:
                    new_win_streak = 0
                    new_loss_streak = current_loss_streak + 1

                await conn.execute(
                    """
                    UPDATE standings_cache SET
                        wins         = wins         + $4,
                        losses       = losses       + $5,
                        conf_wins    = conf_wins    + $6,
                        conf_losses  = conf_losses  + $7,
                        home_wins    = home_wins    + $8,
                        home_losses  = home_losses  + $9,
                        last_10_wins   = $10,
                        last_10_losses = $11,
                        win_streak     = $12,
                        loss_streak    = $13,
                        last_updated = NOW()
                    WHERE league_id = $1 AND season = $2 AND team_id = $3
                    """,
                    league_id,
                    season,
                    team_id,
                    1 if won else 0,
                    0 if won else 1,
                    (1 if won else 0) if same_conf else 0,
                    (0 if won else 1) if same_conf else 0,
                    (1 if won else 0) if is_home else 0,
                    (0 if won else 1) if is_home else 0,
                    l10_wins,
                    l10_losses,
                    new_win_streak,
                    new_loss_streak,
                )

    return {"notable_streak": notable_streak}


async def get_standings(pool: asyncpg.Pool, league_id: int, season: int) -> List[dict]:
    rows = await pool.fetch(
        """
        SELECT sc.*, t.nba_team_code, t.city, t.name AS team_name,
               CASE WHEN (sc.wins + sc.losses) > 0
                    THEN sc.wins::float / (sc.wins + sc.losses)
                    ELSE 0.0 END AS win_pct
        FROM standings_cache sc
        JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id = $1 AND sc.season = $2
        ORDER BY t.conference ASC,
                 CASE WHEN (sc.wins + sc.losses) > 0
                      THEN sc.wins::float / (sc.wins + sc.losses)
                      ELSE 0.0 END DESC,
                 sc.wins DESC
        """,
        league_id,
        season,
    )
    return [dict(r) for r in rows]


async def get_ready_teams(pool: asyncpg.Pool, league_id: int) -> List[int]:
    rows = await pool.fetch(
        "SELECT team_id FROM ready_status WHERE league_id = $1 AND is_ready = TRUE",
        league_id,
    )
    return [r["team_id"] for r in rows]


async def set_ready(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    user_id: int,
    is_ready: bool,
) -> None:
    await pool.execute(
        """
        INSERT INTO ready_status (league_id, team_id, user_id, is_ready, last_updated)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (league_id, team_id)
        DO UPDATE SET is_ready = $4, user_id = $3, last_updated = NOW()
        """,
        league_id,
        team_id,
        user_id,
        is_ready,
    )


async def reset_ready(pool: asyncpg.Pool, league_id: int) -> None:
    await pool.execute(
        "UPDATE ready_status SET is_ready = FALSE, last_updated = NOW() WHERE league_id = $1",
        league_id,
    )


async def get_game_by_index(
    pool: asyncpg.Pool,
    league_id: int,
    season: int,
    game_index: int,
) -> Optional[dict]:
    """Return game row enriched with home/away team codes and full names."""
    row = await pool.fetchrow(
        """
        SELECT
            g.id,
            g.game_index,
            g.scheduled_date,
            g.status,
            g.home_score,
            g.away_score,
            g.home_team_id,
            g.away_team_id,
            ht.nba_team_code  AS home_code,
            ht.city || ' ' || ht.name AS home_full_name,
            at.nba_team_code  AS away_code,
            at.city || ' ' || at.name AS away_full_name
        FROM games g
        JOIN teams ht ON ht.id = g.home_team_id
        JOIN teams at ON at.id = g.away_team_id
        WHERE g.league_id = $1
          AND g.season = $2
          AND g.game_index = $3
        """,
        league_id,
        season,
        game_index,
    )
    return dict(row) if row else None


async def get_box_scores_for_game(
    pool: asyncpg.Pool,
    game_id: int,
) -> list[dict]:
    """
    Return all player box score rows for a game (minutes > 0).
    Ordered starters first (by lineup slot), then bench by points DESC.
    Each row includes first_name, last_name, started, slot (nullable),
    and all stat columns from game_box_scores.
    """
    rows = await pool.fetch(
        """
        SELECT
            b.*,
            p.first_name,
            p.last_name,
            l.slot
        FROM game_box_scores b
        JOIN players p ON p.id = b.player_id
        LEFT JOIN lineups l
            ON l.player_id = b.player_id
           AND l.team_id   = b.team_id
           AND l.league_id = (
               SELECT league_id FROM games WHERE id = $1
           )
        WHERE b.game_id = $1
          AND b.minutes > 0
        ORDER BY
            b.started DESC,
            COALESCE(l.slot, 999) ASC,
            b.points DESC
        """,
        game_id,
    )
    return [dict(r) for r in rows]


async def all_regular_season_games_complete(pool: asyncpg.Pool, league_id: int, season: int) -> bool:
    """Returns True if every regular season game is simmed (none remain scheduled)."""
    remaining = await pool.fetchval(
        """SELECT COUNT(*) FROM games
           WHERE league_id=$1 AND season=$2
             AND season_type='regular' AND status='scheduled'""",
        league_id,
        season,
    )
    return remaining == 0


async def get_total_regular_season_games(pool: asyncpg.Pool, league_id: int, season: int) -> int:
    """Return the total number of regular season games scheduled for this league/season."""
    result = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM games
        WHERE league_id = $1 AND season = $2 AND season_type = 'regular'
        """,
        league_id,
        season,
    )
    return int(result or 0)


async def get_deadline_game_index(pool: asyncpg.Pool, league_id: int, season: int) -> Optional[int]:
    """
    Return the game_index that marks the trade deadline (~67% through the season).
    Uses the median of game_indices above the halfway point to approximate
    the two-thirds mark without requiring a percentile aggregate extension.
    """
    result = await pool.fetchval(
        """
        SELECT MIN(game_index)
        FROM (
            SELECT game_index
            FROM games
            WHERE league_id = $1 AND season = $2 AND season_type = 'regular'
            ORDER BY game_index
            OFFSET (
                SELECT (COUNT(*) * 2 / 3)::int
                FROM games
                WHERE league_id = $1 AND season = $2 AND season_type = 'regular'
            )
            LIMIT 1
        ) sub
        """,
        league_id,
        season,
    )
    return int(result) if result is not None else None
