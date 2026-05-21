from __future__ import annotations

import json
import logging
import os
from collections import Counter
from typing import Optional

import asyncpg

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from services.cpu_voter import VoterProfile, get_cpu_profile, score_player_for_award

log = get_logger(__name__)

_SINGLE_WINNER_AWARDS = {"mvp", "dpoy", "roy", "6moy"}
_ALL_NBA_AWARDS = {"all_nba_1", "all_nba_2", "all_nba_3"}
_ALL_STAR_AWARDS = {"all_star_east", "all_star_west"}

# All-NBA award types ordered 1st → 3rd so we can slice by index.
_ALL_NBA_ORDERED = ["all_nba_1", "all_nba_2", "all_nba_3"]


async def open_voting(league_id: int, season: int, award_type: str) -> int:
    """Opens a voting session for this award. Returns voting_id."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO award_votings (league_id, season, award_type)
        VALUES ($1, $2, $3)
        ON CONFLICT (league_id, season, award_type) DO NOTHING
        RETURNING id
        """,
        league_id,
        season,
        award_type,
    )
    if row is None:
        # Row already existed — fetch it.
        row = await pool.fetchrow(
            "SELECT id FROM award_votings WHERE league_id=$1 AND season=$2 AND award_type=$3",
            league_id,
            season,
            award_type,
        )
    return row["id"]


async def cast_vote(
    voting_id: int,
    team_id: int,
    player_id: int,
    rank: int = 1,
    user_id: Optional[int] = None,
) -> None:
    """Records a human GM vote. Raises DBAError if already voted or voting closed."""
    pool = await get_pool()

    status = await pool.fetchval(
        "SELECT status FROM award_votings WHERE id = $1", voting_id
    )
    if status is None:
        raise DBAError("Voting session not found.")
    if status != "open":
        raise DBAError("This voting session is already closed.")

    existing = await pool.fetchval(
        "SELECT id FROM award_votes WHERE voting_id = $1 AND voter_team_id = $2",
        voting_id,
        team_id,
    )
    if existing is not None:
        raise DBAError("Your team has already cast a vote in this session.")

    await pool.execute(
        """
        INSERT INTO award_votes (voting_id, voter_team_id, voter_user_id, rank, player_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        voting_id,
        team_id,
        user_id,
        rank,
        player_id,
    )


async def get_season_stats(
    pool: asyncpg.Pool, league_id: int, season: int, player_id: int
) -> dict:
    """Aggregates box scores for a player into season averages for voting."""
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*)                                                    AS games_played,
            AVG(b.points)                                               AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)                        AS rpg,
            AVG(b.assists)                                              AS apg,
            AVG(b.steals)                                               AS spg,
            AVG(b.blocks)                                               AS bpg,
            AVG(b.minutes)                                              AS mpg,
            CASE WHEN SUM(b.fga) > 0
                 THEN SUM(b.fgm)::REAL / SUM(b.fga) ELSE 0 END         AS fg_pct,
            CASE WHEN (SUM(b.fga) + 0.44 * SUM(b.fta)) > 0
                 THEN SUM(b.points)::REAL /
                      (2.0 * (SUM(b.fga) + 0.44 * SUM(b.fta)))
                 ELSE 0 END                                             AS ts_pct,
            SUM(CASE WHEN b.started THEN 1 ELSE 0 END)                 AS games_started
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1
          AND g.season    = $2
          AND b.player_id = $3
          AND g.season_type = 'regular'
        """,
        league_id,
        season,
        player_id,
    )
    if row is None or row["games_played"] == 0:
        return {
            "games_played": 0,
            "games_started": 0,
            "ppg": 0.0,
            "rpg": 0.0,
            "apg": 0.0,
            "spg": 0.0,
            "bpg": 0.0,
            "mpg": 0.0,
            "fg_pct": 0.0,
            "ts_pct": 0.0,
        }
    return dict(row)


async def _get_eligible_players(
    pool: asyncpg.Pool, league_id: int, season: int, award_type: str
) -> list[dict]:
    """
    Returns rows of {player_id, team_id, is_rookie, games_played, games_started, ...stats}.
    Eligibility rules:
      MVP/DPOY/6MOY/All-NBA/All-Star: games_played > 20
      ROY: games_played > 20 AND is_rookie = TRUE
      6MOY: games_played > 20 AND majority NOT starter
      All-NBA: games_played > 20 AND majority starter
    """
    rows = await pool.fetch(
        """
        SELECT
            p.id                                                        AS player_id,
            p.team_id,
            p.is_rookie,
            p.position,
            p.defense,
            p.birth_date,
            COUNT(b.id)                                                 AS games_played,
            SUM(CASE WHEN b.started THEN 1 ELSE 0 END)                 AS games_started,
            AVG(b.points)                                               AS ppg,
            AVG(b.rebounds_off + b.rebounds_def)                        AS rpg,
            AVG(b.assists)                                              AS apg,
            AVG(b.steals)                                               AS spg,
            AVG(b.blocks)                                               AS bpg,
            AVG(b.fouls)                                                AS fouls_per_game,
            CASE WHEN SUM(b.fga) > 0
                 THEN SUM(b.fgm)::REAL / SUM(b.fga) ELSE 0 END         AS fg_pct,
            CASE WHEN (SUM(b.fga) + 0.44 * SUM(b.fta)) > 0
                 THEN SUM(b.points)::REAL /
                      (2.0 * (SUM(b.fga) + 0.44 * SUM(b.fta)))
                 ELSE 0 END                                             AS ts_pct
        FROM players p
        JOIN game_box_scores b ON b.player_id = p.id
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1
          AND g.season    = $2
          AND g.season_type = 'regular'
          AND p.league_id = $1
        GROUP BY p.id
        HAVING COUNT(b.id) > 20
        """,
        league_id,
        season,
    )

    log.info(
        f"_get_eligible_players({award_type}): raw rows from DB = {len(rows)} "
        f"(league={league_id} season={season})"
    )

    eligible: list[dict] = []
    for r in rows:
        row_dict = dict(r)
        gp = row_dict["games_played"]
        gs = row_dict["games_started"]
        majority_starter = gs > gp / 2

        if award_type == "roy" and not row_dict["is_rookie"]:
            continue
        if award_type == "6moy" and majority_starter:
            continue
        if award_type in _ALL_NBA_AWARDS and not majority_starter:
            continue
        if award_type == "dpoy":
            # Defensive rating floor — must be a real defender, not just
            # marginal. Luka-type wings around 70 def_attr were sneaking onto
            # the ballot at the old floor of 65.
            if (row_dict.get("defense") or 0) < 75:
                continue
            # Production floor — must produce defensive impact (steals or
            # blocks), not just be rated for it. Combined spg+bpg under 1.5
            # is below DPOY-tier output.
            _spg = float(row_dict.get("spg") or 0)
            _bpg = float(row_dict.get("bpg") or 0)
            if _spg + _bpg < 1.5:
                continue

        eligible.append(row_dict)

    log.info(
        f"_get_eligible_players({award_type}): {len(eligible)} eligible after role filter. "
        f"Candidate player_ids: {[p['player_id'] for p in eligible[:10]]}"
    )
    return eligible


def _mvp_score(p: dict) -> float:
    """Canonical MVP composite for in-season race ranking.

    team_win_pct is excluded here because standings are not always available
    in the race-leader context. The scorer formula (ppg/apg/rpg/ts_pct) is
    consistent with the full voter formula which adds win_pct on top.
    """
    ppg = float(p["ppg"] or 0)
    apg = float(p["apg"] or 0)
    rpg = float(p["rpg"] or 0)
    ts_pct = float(p.get("ts_pct") or 0)
    return ppg * 1.0 + apg * 0.6 + rpg * 0.4 + ts_pct * 10


def _mvp_team_adjustments(win_pct: float, wins: int) -> float:
    """Return the team-record component of MVP scoring.

    Separated so cpu_voter and any future callers can import one source of truth.

    win_pct * 32 — wider spread between contenders and lottery teams than the
    old *20 multiplier (contender +19.5 vs lottery +7.8 instead of +12.2 / +4.9).

    Sub-.500 penalty: shaves additional points off bad-team candidates.
    Tank-team hard cap at 35: a player on a <25-win team cannot exceed this
    ceiling regardless of stats, keeping them below realistic MVP candidates
    (who sit in the 45-55 range).
    """
    base = win_pct * 32
    if win_pct < 0.500:
        base -= (0.500 - win_pct) * 25
    return base


async def get_race_leaders(
    pool,
    league_id: int,
    season: int,
    top_n: int = 5,
) -> dict[str, list[dict]]:
    """Return top-N candidates per award race (mvp, dpoy, roy, 6moy) for odds generation."""
    result = {}
    sort_keys = {
        "mvp":  _mvp_score,
        "dpoy": lambda p: float(p["bpg"] or 0) + float(p["spg"] or 0) + 0.1 * float(p["rpg"] or 0),
        "roy":  lambda p: float(p["ppg"] or 0) + 0.3 * float(p["rpg"] or 0) + 0.3 * float(p["apg"] or 0),
        "6moy": lambda p: float(p["ppg"] or 0) + 0.3 * float(p["apg"] or 0),
    }
    for award_type, sort_key in sort_keys.items():
        try:
            players = await _get_eligible_players(pool, league_id, season, award_type)
        except Exception:
            players = []
        sorted_players = sorted(players, key=sort_key, reverse=True)[:top_n]
        result[award_type] = sorted_players
    return result


async def generate_awards_race_odds(
    pool,
    league_id: int,
    season: int,
    prefetched_leaders: dict | None = None,
) -> dict | None:
    """
    Returns AI-generated odds dict shaped:
      {"mvp": [{"name": str, "pct": int}, ...], "dpoy": [...], "roy": [...], "6moy": [...]}
    or None if not enough data or Claude fails.

    prefetched_leaders: pass the result of get_race_leaders(top_n=5) to avoid a
    redundant DB round-trip when the caller already fetched it this batch tick.
    """
    import anthropic

    if prefetched_leaders is not None:
        leaders = prefetched_leaders
    else:
        leaders = await get_race_leaders(pool, league_id, season, top_n=5)

    # Gate: need at least one award with candidates having 25+ games.
    has_enough = any(
        any(p.get("games_played", 0) >= 25 for p in candidates)
        for candidates in leaders.values()
        if candidates
    )
    if not has_enough:
        return None

    # Fetch player names.
    all_player_ids = [p["player_id"] for candidates in leaders.values() for p in candidates]
    if not all_player_ids:
        return None
    name_rows = await pool.fetch(
        "SELECT id, first_name, last_name FROM players WHERE id = ANY($1)",
        all_player_ids,
    )
    names = {r["id"]: f"{r['first_name']} {r['last_name']}" for r in name_rows}

    # Fetch team records for context.
    standings = await pool.fetch(
        """
        SELECT sc.team_id, sc.wins, sc.losses, t.nba_team_code
        FROM standings_cache sc JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id = $1 AND sc.season = $2
        """,
        league_id, season,
    )
    team_record = {r["team_id"]: f"{r['nba_team_code']} {r['wins']}-{r['losses']}" for r in standings}

    # Fetch recent form: per candidate, last 20 games vs season average.
    recent_form: dict[int, dict] = {}
    for candidates in leaders.values():
        for p in candidates:
            pid = p["player_id"]
            try:
                row = await pool.fetchrow(
                    """
                    WITH recent_games AS (
                        SELECT g.id AS game_id
                        FROM games g
                        JOIN game_box_scores b2 ON b2.game_id = g.id AND b2.player_id = $3
                        WHERE g.league_id = $1 AND g.season = $2 AND g.season_type = 'regular'
                        ORDER BY g.game_index DESC
                        LIMIT 20
                    )
                    SELECT
                        AVG(b.points)                             AS ppg_recent,
                        AVG(b.rebounds_off + b.rebounds_def)      AS rpg_recent,
                        AVG(b.assists)                            AS apg_recent,
                        COUNT(b.id)                               AS gp_recent
                    FROM game_box_scores b
                    JOIN recent_games rg ON rg.game_id = b.game_id
                    WHERE b.player_id = $3
                    """,
                    league_id, season, pid,
                )
                if row and row["gp_recent"]:
                    recent_form[pid] = {
                        "ppg": round(float(row["ppg_recent"] or 0), 1),
                        "rpg": round(float(row["rpg_recent"] or 0), 1),
                        "apg": round(float(row["apg_recent"] or 0), 1),
                        "gp": int(row["gp_recent"]),
                    }
            except Exception:
                pass

    # Compute positional rank per award (rank among the award's candidates).
    award_sort_keys = {
        "mvp":  _mvp_score,
        "dpoy": lambda p: float(p["bpg"] or 0) + float(p["spg"] or 0) + 0.1 * float(p["rpg"] or 0),
        "roy":  lambda p: float(p["ppg"] or 0) + 0.3 * float(p["rpg"] or 0) + 0.3 * float(p["apg"] or 0),
        "6moy": lambda p: float(p["ppg"] or 0) + 0.3 * float(p["apg"] or 0),
    }
    positional_rank: dict[int, int] = {}
    for award_type, candidates in leaders.items():
        sort_key = award_sort_keys.get(award_type, lambda p: 0.0)
        for rank, p in enumerate(sorted(candidates, key=sort_key, reverse=True), start=1):
            positional_rank[p["player_id"]] = rank

    # Build prompt section per award.
    award_labels = {"mvp": "MVP", "dpoy": "DPOY", "roy": "ROY", "6moy": "6th Man"}
    sections = []
    empty_awards = []
    for award_type, candidates in leaders.items():
        if not candidates:
            empty_awards.append(award_type)
            continue
        label = award_labels[award_type]
        lines = [f"{label}:"]
        for p in candidates:
            pid = p["player_id"]
            pname = names.get(pid, f"Player#{pid}")
            team_str = team_record.get(p.get("team_id", 0), "")
            gp = p.get("games_played", 0)
            ppg = p.get("ppg", 0.0)
            rpg = p.get("rpg", 0.0)
            apg = p.get("apg", 0.0)
            bpg = p.get("bpg", 0.0)
            spg = p.get("spg", 0.0)
            pos_rank = positional_rank.get(pid, "?")
            # Recent form suffix.
            rf = recent_form.get(pid)
            recent_str = (
                f" | Last {rf['gp']}G: {rf['ppg']}PPG {rf['rpg']}RPG {rf['apg']}APG"
                if rf else ""
            )
            lines.append(
                f"  - #{pos_rank} {pname} ({team_str}, {gp}GP): "
                f"{ppg:.1f}PPG {rpg:.1f}RPG {apg:.1f}APG {bpg:.1f}BPG {spg:.1f}SPG"
                f"{recent_str}"
            )
        sections.append("\n".join(lines))

    if not sections:
        return None

    prompt = (
        "You are an NBA writer setting awards-race odds. For each award below, output "
        "the percentage chance each candidate wins, summing to 100% per award. "
        "MANDATORY: For MVP, the canonical ranking formula is "
        "ppg*1.0 + apg*0.6 + rpg*0.4 + team_win_pct*20 + ts_pct*10. "
        "The candidates are already sorted by this formula (positional rank #N). "
        "You must not deviate dramatically from this ordering — the player ranked #1 "
        "should have the highest win probability unless recent form strongly indicates otherwise. "
        "Weight criteria in this order: (1) canonical formula rank (mandatory anchor); "
        "(2) recent form — last 20 games trend; (3) games played / availability. "
        "Round to whole percentages.\n\n"
        + "\n\n".join(sections)
        + "\n\nReturn ONLY valid JSON, no markdown, no code fences:\n"
        '{"mvp": [{"name": "...", "pct": 35}, ...], '
        '"dpoy": [...], "roy": [...], "6moy": [...]}\n'
        "Each array sorted descending by pct. Percentages in each award sum to 100. "
        "Omit awards with no candidates."
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        # Validate shape and coerce pct to int — Claude sometimes returns floats or strings.
        if not isinstance(parsed, dict):
            return None
        valid = {}
        for k in ("mvp", "dpoy", "roy", "6moy"):
            arr = parsed.get(k, [])
            if isinstance(arr, list) and all(
                isinstance(item, dict) and "name" in item and "pct" in item
                for item in arr
            ):
                normalized = []
                for item in arr:
                    try:
                        normalized.append({"name": item["name"], "pct": int(item["pct"])})
                    except (ValueError, TypeError):
                        continue
                if normalized:
                    valid[k] = normalized
        return valid if valid else None
    except Exception as exc:
        logging.getLogger(__name__).warning(f"Awards race odds generation failed: {exc}")
        return None


async def generate_cpu_votes(
    voting_id: int, league_id: int, season: int
) -> int:
    """
    For each CPU team, pick the top player for this award based on their profile.
    Returns count of CPU votes cast.
    """
    pool = await get_pool()

    voting_row = await pool.fetchrow(
        "SELECT award_type FROM award_votings WHERE id = $1", voting_id
    )
    if voting_row is None:
        raise DBAError("Voting session not found.")
    award_type: str = voting_row["award_type"]

    cpu_teams = await pool.fetch(
        "SELECT id FROM teams WHERE league_id = $1 AND manager_user_id IS NULL",
        league_id,
    )
    if not cpu_teams:
        return 0

    eligible = await _get_eligible_players(pool, league_id, season, award_type)
    if not eligible:
        log.warning(
            f"No eligible players for award {award_type} in league {league_id} season {season}. "
            f"Verify that regular-season games have season_type='regular' in the games table."
        )
        return 0

    log.info(
        f"generate_cpu_votes({award_type}): {len(eligible)} candidates. "
        f"Top by player_id: {[p['player_id'] for p in eligible[:5]]}"
    )

    # Pre-fetch team records from standings_cache for all team_ids present.
    # Also compute conference rank (1 = best) using the same subquery pattern as team_intel.
    team_ids = list({p["team_id"] for p in eligible if p["team_id"] is not None})
    record_rows = await pool.fetch(
        """
        SELECT
            sc.team_id,
            sc.wins,
            sc.losses,
            t.conference,
            (
                SELECT COUNT(*) + 1
                FROM standings_cache sc2
                JOIN teams t2 ON t2.id = sc2.team_id
                WHERE sc2.league_id = sc.league_id
                  AND sc2.season    = sc.season
                  AND t2.conference = t.conference
                  AND sc2.wins      > sc.wins
            )::int AS conf_rank
        FROM standings_cache sc
        JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id = $1 AND sc.season = $2 AND sc.team_id = ANY($3)
        """,
        league_id,
        season,
        team_ids,
    )
    records_by_team: dict[int, dict] = {
        r["team_id"]: {
            "wins": r["wins"],
            "losses": r["losses"],
            "conf_rank": r["conf_rank"] if r["conf_rank"] is not None else 15,
        }
        for r in record_rows
    }

    # For DPOY: identify top-10 teams by fewest points allowed per game.
    # We approximate from game_box_scores: average opponent points per game.
    dpoy_def_team_ids: set[int] = set()
    if award_type == "dpoy":
        try:
            def_rows = await pool.fetch(
                """
                SELECT t.team_id,
                       AVG(opp.total_pts) AS pts_allowed_pg
                FROM (
                    SELECT home_team_id AS team_id, away_score AS total_pts, id AS game_id
                    FROM games
                    WHERE league_id = $1 AND season = $2 AND season_type = 'regular' AND status = 'simmed'
                    UNION ALL
                    SELECT away_team_id AS team_id, home_score AS total_pts, id AS game_id
                    FROM games
                    WHERE league_id = $1 AND season = $2 AND season_type = 'regular' AND status = 'simmed'
                ) opp
                JOIN standings_cache t ON t.team_id = opp.team_id AND t.league_id = $1 AND t.season = $2
                GROUP BY t.team_id
                ORDER BY pts_allowed_pg ASC
                LIMIT 10
                """,
                league_id,
                season,
            )
            dpoy_def_team_ids = {r["team_id"] for r in def_rows}
        except Exception as exc:
            log.warning(f"DPOY defensive team lookup failed: {exc}")

    already_voted = set(
        r["voter_team_id"]
        for r in await pool.fetch(
            "SELECT voter_team_id FROM award_votes WHERE voting_id = $1", voting_id
        )
    )

    count = 0
    for team_row in cpu_teams:
        team_id: int = team_row["id"]
        if team_id in already_voted:
            continue

        profile: VoterProfile = get_cpu_profile(team_id)

        best_player_id: Optional[int] = None
        best_score = float("-inf")

        for p in eligible:
            stats = {
                "ppg": float(p["ppg"] or 0),
                "rpg": float(p["rpg"] or 0),
                "apg": float(p["apg"] or 0),
                "spg": float(p["spg"] or 0),
                "bpg": float(p["bpg"] or 0),
                "fg_pct": float(p["fg_pct"] or 0),
                "ts_pct": float(p["ts_pct"] or 0),
            }
            team_rec = records_by_team.get(p["team_id"] or 0, {"wins": 0, "losses": 0})
            # Enrich player dict with DPOY-specific context so the scorer can use it.
            # birth_date, role_tag, and fouls_per_game come from the eligible query.
            p_scored = dict(p)
            p_scored["_team_def_boost"] = 3 if p.get("team_id") in dpoy_def_team_ids else 0
            p_scored["_team_losses"] = team_rec.get("losses", 0)
            p_scored["_season"] = season
            p_scored["team_conf_rank"] = team_rec.get("conf_rank", 15)
            score = score_player_for_award(p_scored, stats, team_rec, award_type, profile)
            if score > best_score:
                best_score = score
                best_player_id = p["player_id"]

        if best_player_id is None:
            continue

        await pool.execute(
            """
            INSERT INTO award_votes
                (voting_id, voter_team_id, voter_user_id, cpu_profile, rank, player_id)
            VALUES ($1, $2, NULL, $3, 1, $4)
            ON CONFLICT (voting_id, voter_team_id) DO NOTHING
            """,
            voting_id,
            team_id,
            profile.value,
            best_player_id,
        )
        count += 1

    return count


async def close_voting(voting_id: int) -> list[dict]:
    """
    Tallies all votes, inserts award_results, marks voting closed.
    Returns [{place, player_id, vote_count, vote_share}] sorted by place.
    """
    pool = await get_pool()

    voting_row = await pool.fetchrow(
        "SELECT award_type, status FROM award_votings WHERE id = $1", voting_id
    )
    if voting_row is None:
        raise DBAError("Voting session not found.")
    if voting_row["status"] == "closed":
        raise DBAError("This voting session is already closed.")

    award_type: str = voting_row["award_type"]

    vote_rows = await pool.fetch(
        "SELECT player_id, rank FROM award_votes WHERE voting_id = $1", voting_id
    )
    total_votes = len(vote_rows)

    if award_type in _ALL_NBA_AWARDS:
        # For All-NBA: tally all votes across the three award_type sessions together.
        # Each session (all_nba_1/2/3) tallies its own votes independently —
        # top 5 by vote count in each session map to their respective team.
        counts: Counter = Counter(r["player_id"] for r in vote_rows)
        ranked = counts.most_common()
        results: list[dict] = []
        for place, (player_id, vc) in enumerate(ranked[:5], start=1):
            share = vc / total_votes if total_votes else 0.0
            results.append({
                "place": place,
                "player_id": player_id,
                "vote_count": vc,
                "vote_share": share,
            })
    else:
        # Single-winner and All-Star: rank by vote count.
        counts = Counter(r["player_id"] for r in vote_rows)
        ranked = counts.most_common()
        results = []
        for place, (player_id, vc) in enumerate(ranked, start=1):
            share = vc / total_votes if total_votes else 0.0
            results.append({
                "place": place,
                "player_id": player_id,
                "vote_count": vc,
                "vote_share": share,
            })

    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in results:
                await conn.execute(
                    """
                    INSERT INTO award_results (voting_id, player_id, place, vote_count, vote_share)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    voting_id,
                    r["player_id"],
                    r["place"],
                    r["vote_count"],
                    r["vote_share"],
                )
            await conn.execute(
                "UPDATE award_votings SET status='closed', closed_at=NOW() WHERE id=$1",
                voting_id,
            )

    log.info(f"Closed voting {voting_id} ({award_type}), {total_votes} votes tallied")
    return results


async def open_all_star_voting(league_id: int, season: int) -> tuple[int, int]:
    """Opens east and west All-Star votes simultaneously. Returns (east_voting_id, west_voting_id)."""
    east_id = await open_voting(league_id, season, "all_star_east")
    west_id = await open_voting(league_id, season, "all_star_west")
    return east_id, west_id


async def get_closed_results(
    pool: asyncpg.Pool, league_id: int, season: int, award_type: str
) -> list[dict]:
    """Fetches stored results for a closed award. Used by the results command."""
    rows = await pool.fetch(
        """
        SELECT ar.place, ar.player_id, ar.vote_count, ar.vote_share
        FROM award_results ar
        JOIN award_votings av ON av.id = ar.voting_id
        WHERE av.league_id = $1
          AND av.season    = $2
          AND av.award_type = $3
        ORDER BY ar.place
        """,
        league_id,
        season,
        award_type,
    )
    return [dict(r) for r in rows]
