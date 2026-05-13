from __future__ import annotations

import asyncpg

from core.logging import get_logger
from data.repositories import hof_repo

log = get_logger(__name__)

# Induction thresholds — any one is sufficient.
_CHAMPIONSHIPS_THRESHOLD = 3
_ELITE_SEASONS_OVR = 85
_ELITE_SEASONS_COUNT = 5
_MVP_VOTES_THRESHOLD = 8
_VETERAN_YEARS = 15
_VETERAN_PEAK_OVR = 80


async def check_and_induct(
    pool: asyncpg.Pool, league_id: int, season: int
) -> list[dict]:
    """
    Called at end of rollover. Evaluates recently retired players and long-tenured
    active players for Hall of Fame eligibility. Returns list of inducted player dicts
    (with player data merged) suitable for announcement.

    Criteria (any one sufficient):
    - 3+ championships
    - 5+ seasons with OVR >= 85 (tracked via history_seasons/award_results peak approximation)
    - 8+ MVP award votes across career
    - years_pro >= 15 with peak OVR >= 80
    """
    candidates = await pool.fetch(
        """
        SELECT p.id,
               p.first_name,
               p.last_name,
               p.years_pro,
               p.overall,
               p.roster_status,
               p.league_id
        FROM players p
        WHERE p.league_id = $1
          AND p.roster_status IN ('retired', 'free_agent', 'active')
          AND p.years_pro >= 1
        """,
        league_id,
    )

    inducted: list[dict] = []

    for row in candidates:
        player_id: int = row["id"]

        # Skip if already in the HOF.
        if await hof_repo.get_inducted(pool, league_id, player_id):
            continue

        years_pro: int = row["years_pro"]
        overall: int = row["overall"]

        # Championships: count seasons where the champion's roster included this player.
        career_championships: int = await _count_championships(pool, league_id, player_id)

        # MVP votes: total award_results rows for this player across all mvp votings.
        career_mvp_votes: int = await _count_mvp_votes(pool, league_id, player_id)

        # Peak OVR: best overall recorded in history or current value.
        career_overall_peak: int = overall  # current overall is a reasonable proxy

        # Seasons with OVR >= 85: approximated via contracts signed_in_season count
        # when player OVR was >= 85 — we use current OVR as a lower bound since OVR
        # can only be estimated from the live value without a full history table.
        elite_seasons: int = await _count_elite_seasons(pool, league_id, player_id)

        reason = _evaluate(
            years_pro=years_pro,
            overall=overall,
            career_championships=career_championships,
            career_mvp_votes=career_mvp_votes,
            elite_seasons=elite_seasons,
        )
        if reason is None:
            continue

        record = await hof_repo.induct(
            pool,
            league_id=league_id,
            player_id=player_id,
            inducted_season=season,
            career_seasons=years_pro,
            career_championships=career_championships,
            career_mvp_votes=career_mvp_votes,
            career_overall_peak=career_overall_peak,
            induction_reason=reason,
        )

        inducted.append(
            {
                "player_id": player_id,
                "player_name": f"{row['first_name']} {row['last_name']}",
                "record": record,
            }
        )
        log.info(
            "HOF induction: player %d (%s %s) in league %d season %d — %s",
            player_id,
            row["first_name"],
            row["last_name"],
            league_id,
            season,
            reason,
        )

    return inducted


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _evaluate(
    years_pro: int,
    overall: int,
    career_championships: int,
    career_mvp_votes: int,
    elite_seasons: int,
) -> str | None:
    """Returns the induction reason string, or None if not eligible."""
    if career_championships >= _CHAMPIONSHIPS_THRESHOLD:
        return f"{career_championships}-time champion"
    if elite_seasons >= _ELITE_SEASONS_COUNT:
        return f"{elite_seasons} elite seasons (OVR {_ELITE_SEASONS_OVR}+)"
    if career_mvp_votes >= _MVP_VOTES_THRESHOLD:
        return f"{career_mvp_votes} career MVP votes"
    if years_pro >= _VETERAN_YEARS and overall >= _VETERAN_PEAK_OVR:
        return f"{years_pro}-year career veteran (peak OVR {overall})"
    return None


async def _count_championships(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> int:
    """
    Count seasons where the player's team won the NBA Finals.
    Uses history_seasons.champion_team_id matched against the player's contract
    team_id in that signed_in_season.
    """
    result = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM history_seasons hs
        JOIN contracts c
            ON c.league_id = hs.league_id
           AND c.team_id   = hs.champion_team_id
           AND c.player_id = $2
           AND c.signed_in_season <= hs.season
           AND (c.signed_in_season + c.total_years) > hs.season
        WHERE hs.league_id = $1
          AND hs.champion_team_id IS NOT NULL
        """,
        league_id,
        player_id,
    )
    return int(result or 0)


async def _count_mvp_votes(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> int:
    """Count total award_results rows for this player in MVP votings."""
    result = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM award_results ar
        JOIN award_votings av ON av.id = ar.voting_id
        WHERE av.league_id  = $1
          AND ar.player_id  = $2
          AND av.award_type = 'mvp'
        """,
        league_id,
        player_id,
    )
    return int(result or 0)


async def _count_elite_seasons(
    pool: asyncpg.Pool, league_id: int, player_id: int
) -> int:
    """
    Estimate seasons the player had OVR >= 85.
    Uses the count of distinct signed_in_season values on their contracts as a
    proxy for active seasons — without a per-season OVR snapshot table this is
    the closest available approximation. If current OVR >= 85, we count all
    contract seasons; otherwise we return 0 (conservative).
    """
    if await pool.fetchval(
        "SELECT overall FROM players WHERE id = $1", player_id
    ) < _ELITE_SEASONS_OVR:
        return 0

    result = await pool.fetchval(
        """
        SELECT COUNT(DISTINCT signed_in_season)
        FROM contracts
        WHERE player_id  = $1
          AND league_id  = $2
        """,
        player_id,
        league_id,
    )
    return int(result or 0)
