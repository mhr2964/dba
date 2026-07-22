from __future__ import annotations

import datetime
from typing import Optional

from data.repositories import league_repo, player_repo, team_repo
from services import trade_context_builder


# ---------------------------------------------------------------------------
# Pure player-attribute helpers
# ---------------------------------------------------------------------------

def _player_age(player: player_repo.Player) -> Optional[int]:
    """Return player's age in whole years, or None if birth_date is missing.

    Callers must guard against None before using for age-based comparisons.
    Returning None (not 28) avoids silently biasing age-bucketing logic for
    players whose birth_date was never loaded.
    """
    if player.birth_date:
        today = datetime.date.today()
        age = today.year - player.birth_date.year
        if (today.month, today.day) < (player.birth_date.month, player.birth_date.day):
            age -= 1
        return age
    return None


def _player_age_from_row(row) -> int:
    """Return player age from a raw DB row dict.  Defaults to 28 when birth_date is NULL."""
    birth = row["birth_date"]
    if birth:
        today = datetime.date.today()
        age = today.year - birth.year
        if (today.month, today.day) < (birth.month, birth.day):
            age -= 1
        return age
    return 28


def _default_posture(team: team_repo.Team) -> dict:
    """Fallback posture dict used when _compute_team_posture fails or is absent.

    All callers that look up ``postures.get(team.id) or ...`` use this shape,
    so centralising it avoids the inline literal being defined four times with
    the risk of fields drifting out of sync.
    """
    return {
        "mode": team.cpu_mode or "developing",
        "urgency": "comfortable",
        "avg_age": 27.0,
        "projected_wins": None,
        "conf_rank": None,
        "games_remaining": 82,
        "near_threshold": None,
    }


def is_cornerstone(team: team_repo.Team, player: player_repo.Player, roster: list[player_repo.Player]) -> bool:
    """
    Return True when this player is untouchable for their team.

    A player is a cornerstone if ANY of:
    - OVR >= 92 always (superstar; untouchable in all modes).
    - OVR >= 88 AND they are the team's #1 player by OVR (top-of-roster centerpiece).
    - OVR >= 86 AND they are the team's #1 AND the team's mode is "contending" or
      "comfortable" (your one star matters most when you're trying to win now).

    Exception: rebuilding/tanking teams only protect the first case (true OVR 92+
    superstars).  A genuine tear-down will move almost everyone else.
    """
    ovr = player.overall
    mode = team.cpu_mode or "default"
    is_rebuilding = mode in ("rebuilding", "soft_rebuild")

    # OVR 92+ is always untouchable, every mode.
    if ovr >= 92:
        return True

    # For rebuilding/soft_rebuild teams only the 92+ case above applies.
    if is_rebuilding:
        return False

    # Identify the team's #1 player by OVR.
    if roster:
        top_player = max(roster, key=lambda p: p.overall)
        is_top_player = (player.id == top_player.id)
    else:
        is_top_player = False

    # OVR 88-91 — untouchable if this player is the team's centerpiece.
    if ovr >= 88 and is_top_player:
        return True

    # OVR 86-87 — untouchable only if team is actively trying to win.
    if ovr >= 86 and is_top_player and mode in ("contending", "play_in_fringe"):
        return True

    return False


# ---------------------------------------------------------------------------
# Posture computation
# ---------------------------------------------------------------------------

async def _compute_team_posture(
    pool,
    league: league_repo.League,
    team_id: int,
) -> dict:
    """
    Dynamically determine a team's trade posture from current season data.

    Returns a dict with:
      mode          - 'rebuilding' | 'developing' | 'contending'
      projected_wins - projected full-season wins (None if < 10 games played)
      avg_age       - average age of top-8 lineup players
      conf_rank     - 1-based rank within conference (1 = best), None if no standings yet
      urgency       - 'tanking' | 'comfortable' | 'pushing' | 'desperate'
      games_remaining - games left in regular season
    """
    # Current record + conference from standings + teams join
    row = await pool.fetchrow(
        """
        SELECT sc.wins, sc.losses, t.conference
        FROM standings_cache sc
        JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id = $1 AND sc.team_id = $2 AND sc.season = $3
        """,
        league.id, team_id, league.current_season,
    )
    wins = row["wins"] if row else 0
    losses = row["losses"] if row else 0
    conference = row["conference"] if row else None
    games_played = wins + losses

    projected_wins: int | None = None
    if games_played >= 10:
        projected_wins = round((wins / games_played) * 82)

    # Conference rank: how many conference peers have strictly more wins?
    conf_rank: int | None = None
    if conference:
        rank_val = await pool.fetchval(
            """
            SELECT COUNT(*) + 1
            FROM standings_cache sc2
            JOIN teams t2 ON t2.id = sc2.team_id
            WHERE sc2.league_id = $1 AND sc2.season = $2
              AND t2.conference = $3
              AND sc2.wins > $4
            """,
            league.id, league.current_season, conference, wins,
        )
        conf_rank = int(rank_val) if rank_val is not None else None

    # Roster average age — top-8 slots (starters + primary bench)
    age_rows = await pool.fetch(
        """
        SELECT EXTRACT(YEAR FROM AGE(p.birth_date))::int AS age
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2
          AND p.birth_date IS NOT NULL
        ORDER BY l.slot ASC
        LIMIT 8
        """,
        league.id, team_id,
    )
    ages = [r["age"] for r in age_rows if r["age"] is not None]
    avg_age = sum(ages) / len(ages) if ages else 27.0

    # Star count (OVR >= 85) and plan goal — feed into posture floor logic.
    star_count_val = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM lineups l
        JOIN players p ON p.id = l.player_id
        WHERE l.league_id = $1 AND l.team_id = $2 AND p.overall >= 85
        """,
        league.id, team_id,
    )
    star_count = int(star_count_val or 0)

    plan_goal_row = await pool.fetchrow(
        """
        SELECT goal FROM franchise_plans
        WHERE league_id = $1 AND team_id = $2 AND season = $3
        """,
        league.id, team_id, league.current_season,
    )
    plan_goal = plan_goal_row["goal"] if plan_goal_row else None

    # --- Derive mode (5-bucket system) via shared trade_context_builder function ---
    # Using trade_context_builder.compute_team_mode ensures propose-side and accept-side
    # always agree on mode — no divergence between _compute_team_posture and
    # trade_service._cpu_evaluate.
    in_top4 = conf_rank is not None and conf_rank <= 4
    in_top6 = conf_rank is not None and conf_rank <= 6
    in_top10 = conf_rank is not None and conf_rank <= 10
    mode = trade_context_builder.compute_team_mode(
        projected_wins, avg_age, conf_rank,
        star_count=star_count, plan_goal=plan_goal,
    )

    # --- Derive urgency ---
    games_remaining = max(0, 82 - games_played)
    if mode in ("rebuilding", "soft_rebuild"):
        urgency = "tanking"
    elif mode == "contending":
        if in_top4:
            urgency = "comfortable" if (games_remaining > 20 or conf_rank <= 2) else "pushing"
        elif in_top6:
            urgency = "comfortable" if games_remaining > 20 else "pushing"
        else:
            urgency = "pushing"
    elif mode == "play_in_fringe":
        if in_top10 and games_remaining < 20:
            urgency = "desperate"   # bubble team in final stretch
        else:
            urgency = "pushing"
    else:
        # developing
        urgency = "comfortable"

    # --- Compute near_threshold annotation ---
    near_threshold: str | None = None
    if projected_wins is not None:
        if mode == "contending":
            # How close to dropping to play_in_fringe?
            if projected_wins < 55:
                drop = projected_wins - 49
                near_threshold = f"would be play_in_fringe if {drop} fewer projected wins"
        elif mode == "play_in_fringe":
            # How close to becoming contending vs dropping to developing?
            gap_up = 50 - projected_wins
            gap_down = projected_wins - 39
            if gap_up <= 5:
                near_threshold = f"would be contending with {gap_up} more projected wins"
            elif gap_down <= 5:
                near_threshold = f"would be developing with {gap_down} fewer projected wins"
        elif mode == "developing":
            # Close to soft_rebuild?
            if projected_wins <= 38 and avg_age >= 25.0:
                near_threshold = "would be soft_rebuild if avg_age older or wins drop"
        elif mode == "soft_rebuild":
            if avg_age < 27.5:
                near_threshold = "would be rebuilding if avg_age 2 yrs younger"
        elif mode == "rebuilding":
            if projected_wins >= 22:
                near_threshold = f"would be soft_rebuild with {projected_wins - 25} more wins"

    return {
        "mode": mode,
        "projected_wins": projected_wins,
        "avg_age": avg_age,
        "conf_rank": conf_rank,
        "urgency": urgency,
        "games_remaining": games_remaining,
        "near_threshold": near_threshold,
    }
