from __future__ import annotations

from typing import List, Optional

import discord

from data.repositories.league_repo import League


def status_embed(
    league: League,
    teams_total: int,
    teams_human: int,
    pending_trades: int,
    ready_count: int,
    human_count: int,
    next_matchup_str: Optional[str],
    east_leader: Optional[str],
    west_leader: Optional[str],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"League Status — {league.name}",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Phase", value=league.current_phase, inline=True)
    embed.add_field(name="Season", value=str(league.current_season), inline=True)
    embed.add_field(name="Teams Claimed", value=f"{teams_human}/{teams_total}", inline=True)

    trade_status = f"{pending_trades} pending" if pending_trades else "None"
    embed.add_field(name="Pending Trades", value=trade_status, inline=True)

    if human_count > 0:
        embed.add_field(
            name="Managers Ready",
            value=f"{ready_count}/{human_count}",
            inline=True,
        )

    embed.add_field(
        name="Next User Matchup",
        value=next_matchup_str or "None scheduled",
        inline=True,
    )

    standings_lines = []
    if east_leader:
        standings_lines.append(f"East: {east_leader}")
    if west_leader:
        standings_lines.append(f"West: {west_leader}")
    if standings_lines:
        embed.add_field(
            name="Conference Leaders",
            value="\n".join(standings_lines),
            inline=False,
        )

    return embed


def power_rankings_embed(ranked_teams: List[dict]) -> discord.Embed:
    """
    ranked_teams: list of dicts with keys:
        rank, team_code, wins, losses, last_10_wins, last_10_losses, score, trend
    trend: '↑' | '↓' | '→'
    """
    embed = discord.Embed(title="Power Rankings", color=discord.Color.gold())

    lines = []
    for entry in ranked_teams:
        record = f"{entry['wins']}–{entry['losses']}"
        l10_w = entry["last_10_wins"]
        l10_l = entry["last_10_losses"]
        l10 = f"L10: {l10_w}–{l10_l}"
        trend = entry.get("trend", "→")
        lines.append(
            f"`{entry['rank']:2}.` {trend} **{entry['team_code']}**  {record}  {l10}"
        )

    embed.description = "\n".join(lines) if lines else "No standings data yet."
    return embed


def h2h_embed(
    team_a: str,
    team_b: str,
    wins_a: int,
    wins_b: int,
    recent_games: List[dict],
) -> discord.Embed:
    """
    recent_games: list of dicts with keys:
        scheduled_date, home_code, away_code, home_score, away_score
    """
    embed = discord.Embed(
        title=f"Head-to-Head: {team_a} vs {team_b}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name=team_a, value=f"{wins_a} wins", inline=True)
    embed.add_field(name=team_b, value=f"{wins_b} wins", inline=True)

    if recent_games:
        lines = []
        for g in recent_games[:5]:
            lines.append(
                f"{g['scheduled_date']} — "
                f"`{g['away_code']}` {g['away_score']} @ "
                f"`{g['home_code']}` {g['home_score']}"
            )
        embed.add_field(
            name="Last 5 Matchups",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(name="Last 5 Matchups", value="No completed games.", inline=False)

    return embed


def injury_report_embed(
    injuries: List[dict],
    players_by_id: dict,
    teams_by_id: dict,
) -> discord.Embed:
    """
    injuries: rows from the injuries table (dicts)
    players_by_id: {player_id: {"full_name": str}}
    teams_by_id: {team_id: {"code": str}}
    """
    embed = discord.Embed(title="Injury Report", color=discord.Color.red())

    if not injuries:
        embed.description = "No active injuries."
        return embed

    by_severity: dict[str, list[str]] = {}
    for row in injuries:
        player = players_by_id.get(row["player_id"])
        team = teams_by_id.get(row.get("team_id"))
        player_name = player["full_name"] if player else f"Player #{row['player_id']}"
        team_code = team["code"] if team else "UNK"
        games_missed = row.get("games_missed", 0)
        line = f"**{player_name}** ({team_code}) — {games_missed} gms missed"
        severity = row.get("severity", "Unknown")
        by_severity.setdefault(severity, []).append(line)

    for severity, lines in by_severity.items():
        embed.add_field(
            name=f"Severity: {severity}",
            value="\n".join(lines),
            inline=False,
        )

    return embed


def help_embed(
    current_phase: str,
    available_groups: List[str],
    unavailable_groups: List[str],
) -> discord.Embed:
    embed = discord.Embed(
        title="DBA Command Help",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Current Phase", value=f"`{current_phase}`", inline=False)

    if available_groups:
        available_str = ", ".join(f"`/{g}`" for g in available_groups)
        embed.add_field(name="Available Commands", value=available_str, inline=False)

    if unavailable_groups:
        unavailable_str = ", ".join(f"`/{g}`" for g in unavailable_groups)
        embed.add_field(name="Not Available This Phase", value=unavailable_str, inline=False)

    return embed


def audit_embed(actions: List[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="Commissioner Audit Log",
        color=discord.Color.dark_orange(),
    )
    if not actions:
        embed.description = "No commissioner actions recorded yet."
        return embed

    lines = []
    for row in actions:
        ts = row["created_at"].strftime("%m/%d %H:%M")
        target = f" → {row['target_ref']}" if row.get("target_ref") else ""
        detail = f" ({row['detail']})" if row.get("detail") else ""
        lines.append(f"`{ts}` **{row['action_type']}**{target}{detail}")

    embed.description = "\n".join(lines)
    return embed


def trade_grade_embed(
    trade,
    grade_a: str,
    grade_b: str,
    team_a: str,
    team_b: str,
    rationale: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Trade #{trade.id} — Grades",
        color=discord.Color.green(),
    )
    embed.add_field(name=team_a, value=f"**{grade_a}**", inline=True)
    embed.add_field(name=team_b, value=f"**{grade_b}**", inline=True)
    embed.add_field(name="Analysis", value=rationale, inline=False)
    return embed
