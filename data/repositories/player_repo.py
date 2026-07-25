from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, List, Optional

import asyncpg

# Columns that may be used as a stat leaderboard sort key.
# Allowlist prevents SQL injection when the column name is interpolated.
_STAT_ALLOWLIST = frozenset({
    "overall",
    "speed",
    "shooting_2pt",
    "shooting_3pt",
    "shooting_mid",
    "finishing",
    "playmaking",
    "defense",
    "rebounding",
    "iq",
})

# Career leaderboard stats — controls which columns may be interpolated in
# get_all_time_leaders. Averages require a minimum games threshold; totals do not.
_CAREER_STAT_ALLOWLIST = frozenset({
    "ppg",
    "rpg",
    "apg",
    "spg",
    "bpg",
    "fg_pct",
    "pts_total",
})

_CAREER_AVG_STATS = frozenset({"ppg", "rpg", "apg", "spg", "bpg", "fg_pct"})


@dataclass
class Player:
    id: int
    league_id: int
    external_id: Optional[str]
    first_name: str
    last_name: str
    position: str
    height_in: Optional[int]
    weight_lb: Optional[int]
    birth_date: Optional[datetime.date]
    years_pro: int
    is_rookie: bool
    team_id: Optional[int]
    roster_status: str
    overall: int
    speed: int
    shooting_2pt: int
    shooting_3pt: int
    shooting_mid: int
    finishing: int
    playmaking: int
    defense: int
    rebounding: int
    iq: int
    potential: int
    peak_age_start: int
    peak_age_end: int
    loyalty: int
    money_drive: int
    win_drive: int
    market_pref: str
    star_leverage: int
    # Tendency and role fields — default 50 (or 20 for post) matching DB column defaults
    tendency_3pt: int = 50
    tendency_drive: int = 50
    tendency_mid: int = 50
    tendency_post: int = 20
    tendency_pass: int = 50
    usage_weight: int = 50
    clutch_rating: int = 50
    defensive_effort: int = 50
    # Per-stat tendencies derived from BDL actuals; 50 = position-average baseline
    blk_tendency: int = 50
    stl_tendency: int = 50
    ast_tendency: int = 50
    reb_tendency: int = 50
    # Defensive profile derived from split interior/perimeter scores (migration 039).
    # NULL until backfill_overall_from_defense.py has run.
    defensive_archetype: Optional[str] = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass
class Contract:
    id: int
    league_id: int
    player_id: int
    team_id: Optional[int]
    salary: int
    years_remaining: int
    total_years: int
    contract_type: str
    signed_in_season: int
    is_active: bool
    terminated_reason: Optional[str]


def _player_from_record(r: asyncpg.Record) -> Player:
    keys = r.keys() if hasattr(r, "keys") else {}
    return Player(
        id=r["id"],
        league_id=r["league_id"],
        external_id=r["external_id"],
        first_name=r["first_name"],
        last_name=r["last_name"],
        position=r["position"],
        height_in=r["height_in"],
        weight_lb=r["weight_lb"],
        birth_date=r["birth_date"],
        years_pro=r["years_pro"],
        is_rookie=r["is_rookie"],
        team_id=r["team_id"],
        roster_status=r["roster_status"],
        overall=r["overall"],
        speed=r["speed"],
        shooting_2pt=r["shooting_2pt"],
        shooting_3pt=r["shooting_3pt"],
        shooting_mid=r["shooting_mid"],
        finishing=r["finishing"],
        playmaking=r["playmaking"],
        defense=r["defense"],
        rebounding=r["rebounding"],
        iq=r["iq"],
        potential=r["potential"],
        peak_age_start=r["peak_age_start"],
        peak_age_end=r["peak_age_end"],
        loyalty=r["loyalty"],
        money_drive=r["money_drive"],
        win_drive=r["win_drive"],
        market_pref=r["market_pref"],
        star_leverage=r["star_leverage"],
        tendency_3pt=r["tendency_3pt"] if "tendency_3pt" in keys else 50,
        tendency_drive=r["tendency_drive"] if "tendency_drive" in keys else 50,
        tendency_mid=r["tendency_mid"] if "tendency_mid" in keys else 50,
        tendency_post=r["tendency_post"] if "tendency_post" in keys else 20,
        tendency_pass=r["tendency_pass"] if "tendency_pass" in keys else 50,
        usage_weight=r["usage_weight"] if "usage_weight" in keys else 50,
        clutch_rating=r["clutch_rating"] if "clutch_rating" in keys else 50,
        defensive_effort=r["defensive_effort"] if "defensive_effort" in keys else 50,
        blk_tendency=r["blk_tendency"] if "blk_tendency" in keys else 50,
        stl_tendency=r["stl_tendency"] if "stl_tendency" in keys else 50,
        ast_tendency=r["ast_tendency"] if "ast_tendency" in keys else 50,
        reb_tendency=r["reb_tendency"] if "reb_tendency" in keys else 50,
        defensive_archetype=r["defensive_archetype"] if "defensive_archetype" in keys else None,
    )


def _contract_from_record(r: asyncpg.Record) -> Contract:
    return Contract(
        id=r["id"],
        league_id=r["league_id"],
        player_id=r["player_id"],
        team_id=r["team_id"],
        salary=r["salary"],
        years_remaining=r["years_remaining"],
        total_years=r["total_years"],
        contract_type=r["contract_type"],
        signed_in_season=r["signed_in_season"],
        is_active=r["is_active"],
        terminated_reason=r["terminated_reason"],
    )


async def get_roster(
    pool: asyncpg.Pool, league_id: int, team_id: int
) -> List[Player]:
    rows = await pool.fetch(
        """
        SELECT * FROM players
        WHERE league_id = $1 AND team_id = $2
        ORDER BY overall DESC
        """,
        league_id,
        team_id,
    )
    return [_player_from_record(r) for r in rows]


async def get_by_id(
    pool: asyncpg.Pool, player_id: int
) -> Optional[Player]:
    row = await pool.fetchrow(
        "SELECT * FROM players WHERE id = $1",
        player_id,
    )
    return _player_from_record(row) if row else None


async def get_leaders(
    pool: asyncpg.Pool, league_id: int, stat: str, limit: int = 10
) -> List[tuple[Player, Any]]:
    if stat not in _STAT_ALLOWLIST:
        raise ValueError(f"Invalid stat '{stat}'. Must be one of: {sorted(_STAT_ALLOWLIST)}")
    rows = await pool.fetch(
        f"""
        SELECT *, {stat} AS _stat_value
        FROM players
        WHERE league_id = $1 AND roster_status = 'active'
        ORDER BY {stat} DESC
        LIMIT $2
        """,
        league_id,
        limit,
    )
    return [(_player_from_record(r), r["_stat_value"]) for r in rows]


async def search_by_name(
    pool: asyncpg.Pool, league_id: int, query: str
) -> List[Player]:
    rows = await pool.fetch(
        """
        SELECT * FROM players
        WHERE league_id = $1
          AND unaccent(first_name || ' ' || last_name) ILIKE unaccent($2)
        ORDER BY overall DESC
        LIMIT 25
        """,
        league_id,
        f"%{query}%",
    )
    return [_player_from_record(r) for r in rows]


async def get_lineup(
    pool: asyncpg.Pool, league_id: int, team_id: int
) -> List[tuple[Player, bool, int]]:
    rows = await pool.fetch(
        """
        SELECT p.*, l.is_starter, l.slot
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2
        ORDER BY l.slot
        """,
        league_id,
        team_id,
    )
    return [(_player_from_record(r), r["is_starter"], r["slot"]) for r in rows]


async def get_active_contract(
    pool: asyncpg.Pool, player_id: int
) -> Optional[Contract]:
    row = await pool.fetchrow(
        """
        SELECT * FROM contracts
        WHERE player_id = $1 AND is_active = TRUE
        LIMIT 1
        """,
        player_id,
    )
    return _contract_from_record(row) if row else None


async def get_active_roster_position_counts(
    pool: asyncpg.Pool, league_id: int, team_ids: list[int]
) -> dict[int, dict[str, int]]:
    """Batched active-roster position counts for multiple teams in one query.

    FA2: CPU free-agent targeting needs each team's current position depth to
    weight need vs. surplus (services/fa_needs.py::_position_need_multiplier).
    Batched across all team_ids in a single GROUP BY query, matching the
    "no per-team loops" convention team_intel.py's build_team_intel documents.

    Returns {team_id: {position: count}}. Teams with zero active players at a
    position simply have no key for it — callers should use .get(pos, 0).
    """
    if not team_ids:
        return {}
    rows = await pool.fetch(
        """
        SELECT team_id, position, COUNT(*) AS cnt
        FROM players
        WHERE league_id = $1
          AND team_id = ANY($2::int[])
          AND roster_status = 'active'
        GROUP BY team_id, position
        """,
        league_id,
        team_ids,
    )
    result: dict[int, dict[str, int]] = {tid: {} for tid in team_ids}
    for r in rows:
        result[r["team_id"]][r["position"]] = int(r["cnt"])
    return result


async def get_team_cap_usage(
    pool: asyncpg.Pool, league_id: int, team_id: int
) -> int:
    result = await pool.fetchval(
        """
        SELECT COALESCE(SUM(salary), 0)
        FROM contracts
        WHERE league_id = $1 AND team_id = $2 AND is_active = TRUE
        """,
        league_id,
        team_id,
    )
    return int(result)


async def get_season_stats_for_player(
    pool: asyncpg.Pool, player_id: int, league_id: int, season: int
) -> dict:
    """Per-game averages for one player in one season, from game_box_scores."""
    row = await pool.fetchrow(
        """
        SELECT
            g.season,
            COUNT(b.id)                              AS games_played,
            AVG(b.points)                            AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)     AS rpg,
            AVG(b.assists)                           AS apg,
            AVG(b.steals)                            AS spg,
            AVG(b.blocks)                            AS bpg,
            CASE WHEN SUM(b.fga) > 0
                THEN SUM(b.fgm)::FLOAT / SUM(b.fga) * 100
                ELSE NULL END                        AS fg_pct,
            CASE WHEN SUM(b.tpa) > 0
                THEN SUM(b.tpm)::FLOAT / SUM(b.tpa) * 100
                ELSE NULL END                        AS three_pct
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE b.player_id = $1
          AND g.league_id = $2
          AND g.season    = $3
        GROUP BY g.season
        """,
        player_id,
        league_id,
        season,
    )
    return dict(row) if row else {}


async def get_career_stats(
    pool: asyncpg.Pool, player_id: int, league_id: int
) -> dict:
    """
    Aggregates game_box_scores across ALL seasons for this player.

    Returns seasons_played, games_played, career totals/averages,
    career_high_pts, and a per-season breakdown list.
    """
    totals_row = await pool.fetchrow(
        """
        SELECT
            COUNT(DISTINCT g.season)                 AS seasons_played,
            COUNT(b.id)                              AS games_played,
            COALESCE(SUM(b.points), 0)               AS total_points,
            AVG(b.points)                            AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)     AS rpg,
            AVG(b.assists)                           AS apg,
            AVG(b.steals)                            AS spg,
            AVG(b.blocks)                            AS bpg,
            CASE WHEN SUM(b.fga) > 0
                THEN SUM(b.fgm)::FLOAT / SUM(b.fga) * 100
                ELSE NULL END                        AS fg_pct,
            CASE WHEN SUM(b.tpa) > 0
                THEN SUM(b.tpm)::FLOAT / SUM(b.tpa) * 100
                ELSE NULL END                        AS three_pct,
            MAX(b.points)                            AS career_high_pts
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE b.player_id = $1
          AND g.league_id = $2
        """,
        player_id,
        league_id,
    )

    season_rows = await pool.fetch(
        """
        SELECT
            g.season,
            COUNT(b.id)                              AS games_played,
            AVG(b.points)                            AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)     AS rpg,
            AVG(b.assists)                           AS apg,
            CASE WHEN SUM(b.fga) > 0
                THEN SUM(b.fgm)::FLOAT / SUM(b.fga) * 100
                ELSE NULL END                        AS fg_pct
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE b.player_id = $1
          AND g.league_id = $2
        GROUP BY g.season
        ORDER BY g.season
        """,
        player_id,
        league_id,
    )

    if totals_row is None or totals_row["games_played"] == 0:
        return {
            "seasons_played": 0,
            "games_played": 0,
            "total_points": 0,
            "ppg": 0.0,
            "rpg": 0.0,
            "apg": 0.0,
            "spg": 0.0,
            "bpg": 0.0,
            "fg_pct": None,
            "three_pct": None,
            "career_high_pts": 0,
            "seasons": [],
        }

    def _f(v: Any) -> float:
        return float(v) if v is not None else 0.0

    return {
        "seasons_played": int(totals_row["seasons_played"]),
        "games_played": int(totals_row["games_played"]),
        "total_points": int(totals_row["total_points"]),
        "ppg": _f(totals_row["ppg"]),
        "rpg": _f(totals_row["rpg"]),
        "apg": _f(totals_row["apg"]),
        "spg": _f(totals_row["spg"]),
        "bpg": _f(totals_row["bpg"]),
        "fg_pct": _f(totals_row["fg_pct"]) if totals_row["fg_pct"] is not None else None,
        "three_pct": _f(totals_row["three_pct"]) if totals_row["three_pct"] is not None else None,
        "career_high_pts": int(totals_row["career_high_pts"]),
        "seasons": [dict(r) for r in season_rows],
    }


async def get_all_time_leaders(
    pool: asyncpg.Pool, league_id: int, stat: str, limit: int = 10
) -> list[dict]:
    """
    Career leaders across all seasons.

    stat must be in _CAREER_STAT_ALLOWLIST. Averages enforce a 100-game minimum
    so single-season hot streaks don't dominate; totals have no floor.
    """
    if stat not in _CAREER_STAT_ALLOWLIST:
        raise ValueError(
            f"Invalid career stat '{stat}'. Must be one of: {sorted(_CAREER_STAT_ALLOWLIST)}"
        )

    avg_exprs: dict[str, str] = {
        "ppg": "AVG(b.points)",
        "rpg": "AVG(b.rebounds_off + b.rebounds_def)",
        "apg": "AVG(b.assists)",
        "spg": "AVG(b.steals)",
        "bpg": "AVG(b.blocks)",
        "fg_pct": (
            "CASE WHEN SUM(b.fga) > 0 "
            "THEN SUM(b.fgm)::FLOAT / SUM(b.fga) * 100 ELSE NULL END"
        ),
        "pts_total": "SUM(b.points)",
    }
    value_expr = avg_exprs[stat]

    if stat in _CAREER_AVG_STATS:
        having_clause = "HAVING COUNT(b.id) >= 100"
    else:
        having_clause = ""

    rows = await pool.fetch(
        f"""
        SELECT
            p.id                                     AS player_id,
            p.first_name,
            p.last_name,
            COUNT(DISTINCT g.season)                 AS seasons_played,
            COUNT(b.id)                              AS games_played,
            {value_expr}                             AS value
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        JOIN players p ON p.id = b.player_id
        WHERE g.league_id = $1
        GROUP BY p.id, p.first_name, p.last_name
        {having_clause}
        ORDER BY value DESC NULLS LAST
        LIMIT $2
        """,
        league_id,
        limit,
    )
    return [dict(r) for r in rows]


async def get_league_all_time_records(
    pool: asyncpg.Pool, league_id: int
) -> dict:
    """
    Notable all-time league records drawn from history_seasons, players,
    career box score aggregates, and season_records.
    """
    champ_row = await pool.fetchrow(
        """
        SELECT t.city || ' ' || t.name AS team_name, COUNT(*) AS count
        FROM history_seasons hs
        JOIN teams t ON t.id = hs.champion_team_id
        WHERE hs.league_id = $1 AND hs.champion_team_id IS NOT NULL
        GROUP BY t.id, t.city, t.name
        ORDER BY count DESC
        LIMIT 1
        """,
        league_id,
    )

    mvp_row = await pool.fetchrow(
        """
        SELECT p.first_name || ' ' || p.last_name AS player_name, COUNT(*) AS count
        FROM history_seasons hs
        JOIN players p ON p.id = hs.mvp_player_id
        WHERE hs.league_id = $1 AND hs.mvp_player_id IS NOT NULL
        GROUP BY p.id, p.first_name, p.last_name
        ORDER BY count DESC
        LIMIT 1
        """,
        league_id,
    )

    pts_row = await pool.fetchrow(
        """
        SELECT p.first_name || ' ' || p.last_name AS player_name,
               SUM(b.points)                       AS points
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        JOIN players p ON p.id = b.player_id
        WHERE g.league_id = $1
        GROUP BY p.id, p.first_name, p.last_name
        ORDER BY points DESC
        LIMIT 1
        """,
        league_id,
    )

    ovr_row = await pool.fetchrow(
        """
        SELECT first_name || ' ' || last_name AS player_name, overall AS ovr
        FROM players
        WHERE league_id = $1
        ORDER BY overall DESC
        LIMIT 1
        """,
        league_id,
    )

    streak_row = await pool.fetchrow(
        """
        SELECT t.city || ' ' || t.name AS team_name,
               sr.value::INT            AS streak,
               sr.season
        FROM season_records sr
        JOIN teams t ON t.id = sr.team_id
        WHERE sr.league_id = $1 AND sr.record_type = 'win_streak'
        ORDER BY sr.value DESC
        LIMIT 1
        """,
        league_id,
    )

    return {
        "most_championships": dict(champ_row) if champ_row else None,
        "most_mvps": dict(mvp_row) if mvp_row else None,
        "most_points_career": dict(pts_row) if pts_row else None,
        "highest_ovr_ever": dict(ovr_row) if ovr_row else None,
        "longest_win_streak": dict(streak_row) if streak_row else None,
    }
