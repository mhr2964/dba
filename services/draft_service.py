from __future__ import annotations

import random
from typing import Optional

import asyncpg
import discord

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import draft_repo, league_repo, team_repo
from services import notifier_service

log = get_logger(__name__)

# 2024 NBA lottery odds by standing rank (1 = worst record).
# 14 teams; indices 0-13 correspond to ranks 1-14.
_LOTTERY_ODDS = [
    14.0, 14.0, 14.0, 12.5, 10.5, 9.0, 7.5,
    6.0, 4.5, 3.0, 2.0, 1.5, 1.0, 0.5,
]


async def run_lottery(league_id: int, season: int) -> list[dict]:
    """
    Weighted lottery for picks 1-4 among non-playoff teams.
    Pick order is determined by weighted draw (same team cannot win twice).
    Remaining lottery teams (picks 5-14) fill in reverse record order.
    Playoff teams assigned picks 15-30 (worst eliminated earliest = pick 15, champion = pick 30).
    Updates draft_picks.pick_number for all teams in this season.
    Returns ordered list of {pick_number, team_id, team_name}.
    """
    pool = await get_pool()

    draft = await draft_repo.get_draft(pool, league_id, season)
    if not draft:
        raise DBAError("No draft found for this season. Create the draft first.")
    if draft.status not in ("pending_lottery",):
        raise DBAError(f"Lottery already run (status: {draft.status}).")

    # Pull standings — use wins DESC within conference to approximate.
    # Teams with no game records default to 0 wins (e.g. first season).
    rows = await pool.fetch(
        """
        SELECT t.id AS team_id, t.name, t.city,
               COALESCE(SUM(CASE WHEN g.home_team_id = t.id AND g.home_score > g.away_score THEN 1
                                 WHEN g.away_team_id = t.id AND g.away_score > g.home_score THEN 1
                                 ELSE 0 END), 0) AS wins,
               COALESCE(SUM(CASE WHEN g.home_team_id = t.id OR g.away_team_id = t.id THEN 1 ELSE 0 END), 0) AS games
        FROM teams t
        LEFT JOIN games g ON (g.home_team_id = t.id OR g.away_team_id = t.id)
            AND g.league_id = $1 AND g.season = $2 AND g.status = 'final'
        WHERE t.league_id = $1
        GROUP BY t.id, t.name, t.city
        ORDER BY wins ASC, games DESC
        """,
        league_id,
        season,
    )

    if not rows:
        raise DBAError("No teams found in this league.")

    all_teams = [dict(r) for r in rows]
    total_teams = len(all_teams)

    # Determine playoff cutoff: top 8 per conference or top 16 overall.
    # Without conference-level query here, approximate as top 16 by wins.
    playoff_cutoff = min(16, total_teams // 2) if total_teams >= 4 else 0

    # Sort worst to best for lottery eligibility.
    lottery_teams = all_teams[:total_teams - playoff_cutoff]  # bottom teams
    playoff_teams = all_teams[total_teams - playoff_cutoff:]  # top teams (best last)

    # Cap lottery at 14 teams (NBA rule).
    lottery_teams = lottery_teams[:14]
    n_lottery = len(lottery_teams)

    # Weighted draw for picks 1-4.
    odds = _LOTTERY_ODDS[:n_lottery]
    lottery_order: list[dict] = []
    remaining_pool = list(range(n_lottery))

    for _pick in range(min(4, n_lottery)):
        available_odds = [odds[i] for i in remaining_pool]
        chosen_idx = random.choices(remaining_pool, weights=available_odds, k=1)[0]
        lottery_order.append(lottery_teams[chosen_idx])
        remaining_pool.remove(chosen_idx)

    # Remaining lottery teams fill picks 5-14 in reverse win order (worst remaining = next).
    for idx in remaining_pool:
        lottery_order.append(lottery_teams[idx])

    # Playoff teams: worst eliminated = pick 15, champion = last.
    # They arrive sorted worst to best, so reverse to get worst-eliminated first.
    playoff_order = list(playoff_teams)  # already worst to best among playoff teams

    ordered = lottery_order + playoff_order
    result: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.transaction():
            for pick_num, team in enumerate(ordered, start=1):
                await conn.execute(
                    """
                    UPDATE draft_picks
                    SET pick_number = $1
                    WHERE league_id = $2 AND season = $3 AND current_team_id = $4
                      AND round = 1
                    """,
                    pick_num,
                    league_id,
                    season,
                    team["team_id"],
                )
                result.append({
                    "pick_number": pick_num,
                    "team_id": team["team_id"],
                    "team_name": f"{team['city']} {team['name']}",
                })

            await conn.execute(
                "UPDATE drafts SET status = 'lottery_done', lottery_completed_at = NOW() WHERE id = $1",
                draft.id,
            )

    log.info(f"Lottery complete for league {league_id} season {season}")
    return result


def _on_the_clock_embed(
    team: team_repo.Team,
    pick_number: int,
    round_number: int,
    prospects: list[dict],
) -> discord.Embed:
    embed = discord.Embed(
        title="You're On The Clock!",
        description=(
            f"**{team.full_name}** — Round {round_number}, Pick #{pick_number}\n"
            "Use `/draft advance` in the server to make your selection."
        ),
        color=discord.Color.gold(),
    )
    top_prospects = prospects[:5]
    if top_prospects:
        lines = [
            f"{i + 1}. **{p.get('first_name', '')} {p.get('last_name', '')}** "
            f"({p.get('position', '?')}) OVR {p.get('overall', '?')}"
            for i, p in enumerate(top_prospects)
        ]
        embed.add_field(name="Top Available Prospects", value="\n".join(lines), inline=False)
    return embed


async def advance_pick(
    league_id: int,
    season: int,
    guild: discord.Guild,
    bot: Optional[discord.Client] = None,
) -> dict:
    """
    Resolves CPU picks sequentially until a human team is on the clock or the draft ends.
    Returns:
      {"status": "human_on_clock", "team": Team, "pick_number": int, "prospects": [...]}
      {"status": "complete"}
    CPU picks are auto-selected and announced to #league-news.
    When a human team is on the clock and bot is provided, DMs the manager.
    """
    pool = await get_pool()

    draft = await draft_repo.get_draft(pool, league_id, season)
    if not draft:
        raise DBAError("No draft found for this season.")
    if draft.status == "complete":
        return {"status": "complete"}
    if draft.status == "pending_lottery":
        raise DBAError("Run the lottery before advancing the draft.")

    # Mark in_progress on first advance call.
    if draft.status == "lottery_done":
        await draft_repo.update_draft_status(pool, draft.id, "in_progress")
        draft = await draft_repo.get_draft(pool, league_id, season)

    news_channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    news_channel: Optional[discord.TextChannel] = None
    if news_channel_id:
        news_channel = guild.get_channel(news_channel_id)

    while True:
        pick_number = draft.current_pick_number
        total_picks = await pool.fetchval(
            "SELECT COUNT(*) FROM draft_picks WHERE league_id = $1 AND season = $2",
            league_id,
            season,
        )
        if pick_number > total_picks:
            await draft_repo.update_draft_status(pool, draft.id, "complete")
            return {"status": "complete"}

        round_number = ((pick_number - 1) // 30) + 1
        on_clock_team_id = await draft_repo.get_on_the_clock_team(pool, draft.id, pick_number)
        if on_clock_team_id is None:
            log.warning(f"No team mapped to pick {pick_number} in draft {draft.id}, stopping.")
            return {"status": "complete"}

        team = await team_repo.get_by_id(pool, on_clock_team_id)
        if team is None:
            raise DBAError(f"Team {on_clock_team_id} not found.")

        prospects = await draft_repo.get_available_prospects(pool, league_id, season)
        if not prospects:
            await draft_repo.update_draft_status(pool, draft.id, "complete")
            return {"status": "complete"}

        if team.manager_user_id is not None:
            if bot is not None:
                embed = _on_the_clock_embed(team, pick_number, round_number, prospects)
                await notifier_service.send_dm(
                    bot,
                    league_id,
                    team.manager_user_id,
                    embed=embed,
                    fallback_message=(
                        f"You're on the clock! Pick #{pick_number} in the draft. "
                        "Use `/draft advance` to make your selection."
                    ),
                )
            return {
                "status": "human_on_clock",
                "team": team,
                "pick_number": pick_number,
                "round": round_number,
                "prospects": prospects[:20],
            }

        # CPU team — auto-select best prospect with positional need weighting.
        best = _cpu_select(prospects, team)
        selection = await draft_repo.record_selection(
            pool,
            draft_id=draft.id,
            pick_number=pick_number,
            round=round_number,
            team_id=on_clock_team_id,
            player_id=best["id"],
        )
        await pool.execute(
            "UPDATE draft_picks SET used_for_player_id = $1 WHERE league_id = $2 AND season = $3 AND pick_number = $4",
            best["id"],
            league_id,
            season,
            pick_number,
        )
        await pool.execute(
            "UPDATE players SET team_id = $1, roster_status = 'active' WHERE id = $2",
            on_clock_team_id,
            best["id"],
        )
        await _assign_rookie_contract(pool, league_id, on_clock_team_id, best["id"], pick_number, season)

        log.info(f"CPU pick: {team.full_name} selects player {best['id']} at pick {pick_number}")

        if news_channel:
            from bot.embeds.draft_embeds import pick_announcement
            embed = pick_announcement(pick_number, team, best)
            await news_channel.send(embed=embed)

        next_pick = pick_number + 1
        await draft_repo.update_draft_status(pool, draft.id, "in_progress", current_pick=next_pick)
        draft = await draft_repo.get_draft(pool, league_id, season)


def _cpu_select(prospects: list[dict], team: object) -> dict:
    """
    Selects the best available prospect for a CPU team.
    Best OVR wins with ±2 noise to add slight unpredictability.
    Roster-need weighting is not implemented here; add if team roster data is available at call site.
    """
    scored = []
    for p in prospects:
        score = p["overall"] + random.uniform(-2.0, 2.0)
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


async def make_pick(league_id: int, season: int, team_id: int, player_id: int) -> dict:
    """
    Records a human team's selection. Assigns rookie scale contract. Returns pick result dict.
    """
    pool = await get_pool()

    draft = await draft_repo.get_draft(pool, league_id, season)
    if not draft:
        raise DBAError("No active draft for this season.")
    if draft.status != "in_progress":
        raise DBAError(f"Draft is not in progress (status: {draft.status}).")

    pick_number = draft.current_pick_number
    on_clock = await draft_repo.get_on_the_clock_team(pool, draft.id, pick_number)
    if on_clock != team_id:
        raise DBAError("It's not your team's pick.")

    # Verify player is still available.
    prospects = await draft_repo.get_available_prospects(pool, league_id, season)
    if not any(p["id"] == player_id for p in prospects):
        raise DBAError("That player is no longer available.")

    round_number = ((pick_number - 1) // 30) + 1
    selection = await draft_repo.record_selection(
        pool,
        draft_id=draft.id,
        pick_number=pick_number,
        round=round_number,
        team_id=team_id,
        player_id=player_id,
    )
    await pool.execute(
        "UPDATE draft_picks SET used_for_player_id = $1 WHERE league_id = $2 AND season = $3 AND pick_number = $4",
        player_id,
        league_id,
        season,
        pick_number,
    )
    await pool.execute(
        "UPDATE players SET team_id = $1, roster_status = 'active' WHERE id = $2",
        team_id,
        player_id,
    )
    await _assign_rookie_contract(pool, league_id, team_id, player_id, pick_number, season)

    next_pick = pick_number + 1
    await draft_repo.update_draft_status(pool, draft.id, "in_progress", current_pick=next_pick)

    player_row = next((p for p in prospects if p["id"] == player_id), None)
    log.info(f"Human pick: team {team_id} selects player {player_id} at pick {pick_number}")
    return {
        "pick_number": pick_number,
        "round": round_number,
        "team_id": team_id,
        "player": player_row,
    }


async def ensure_draft_class(league_id: int, class_year: int) -> int:
    """
    If no prospects exist for this league/year, generate a synthetic class.
    Returns total count of available prospects.
    """
    pool = await get_pool()

    count = await pool.fetchval(
        "SELECT COUNT(*) FROM draft_classes WHERE league_id = $1 AND class_year = $2",
        league_id,
        class_year,
    )
    if count and count > 0:
        return int(count)

    from services.draft_class_generator import generate_draft_class
    prospects = generate_draft_class(class_year)
    inserted = await draft_repo.seed_draft_class(pool, league_id, class_year, prospects)
    log.info(f"Generated {inserted} synthetic prospects for league {league_id} class year {class_year}")
    return inserted


async def _assign_rookie_contract(
    pool: asyncpg.Pool,
    league_id: int,
    team_id: int,
    player_id: int,
    pick_number: int,
    season: int,
) -> None:
    """Looks up rookie scale salary for pick_number and creates a 4-year rookie scale contract."""
    capped_pick = min(pick_number, 30)
    salary = await pool.fetchval(
        "SELECT year_1_salary FROM rookie_scale WHERE pick_number = $1",
        capped_pick,
    )
    if salary is None:
        # Second-round picks not in rookie_scale — use minimum.
        salary = 1_100_000

    await pool.execute(
        """
        INSERT INTO contracts
            (league_id, player_id, team_id, salary, years_remaining, total_years,
             contract_type, signed_in_season, is_active)
        VALUES ($1, $2, $3, $4, 4, 4, 'rookie_scale', $5, TRUE)
        """,
        league_id,
        player_id,
        team_id,
        int(salary),
        season,
    )
