from __future__ import annotations

from typing import Any

import discord

from data.repositories.league_repo import League


def season_started_embed(
    league: League,
    game_count: int,
    first_games: list[dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Season {league.current_season} Has Begun!",
        description=f"{game_count} games scheduled.",
        color=discord.Color.green(),
    )
    embed.add_field(name="League", value=league.name, inline=True)
    embed.add_field(name="Phase", value=league.current_phase, inline=True)

    if first_games:
        lines = []
        for g in first_games:
            home_code = g.get("home_code", str(g.get("home_team_id", "?")))
            away_code = g.get("away_code", str(g.get("away_team_id", "?")))
            user_marker = " [USER]" if g.get("is_user_matchup") else ""
            lines.append(
                f"Game #{g['game_index']} — {g['scheduled_date']}  "
                f"`{away_code}` @ `{home_code}`{user_marker}"
            )
        embed.add_field(name="First 5 Games", value="\n".join(lines), inline=False)

    return embed


def season_info_embed(
    league: League,
    games_played: int,
    total_games: int,
    east_top3: list[dict[str, Any]],
    west_top3: list[dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Season {league.current_season} — Overview",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Phase", value=league.current_phase, inline=True)
    embed.add_field(
        name="Games",
        value=f"{games_played} / {total_games} played",
        inline=True,
    )

    def _standings_block(rows: list[dict[str, Any]]) -> str:
        lines = []
        for i, r in enumerate(rows, start=1):
            abbr = r.get("nba_team_code") or r.get("team_name") or f"Team {r.get('team_id', '?')}"
            wins = r.get("wins", 0)
            losses = r.get("losses", 0)
            pct = wins / (wins + losses) if (wins + losses) > 0 else 0.0
            lines.append(f"{i}. **{abbr}** {wins}–{losses} ({pct:.3f})")
        return "\n".join(lines) if lines else "No data"

    embed.add_field(name="East Top 3", value=_standings_block(east_top3), inline=False)
    embed.add_field(name="West Top 3", value=_standings_block(west_top3), inline=False)
    return embed


def phase_status_embed(
    league: League,
    available_commands: list[str],
    blockers: list[str],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{league.name} — Phase Status",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Current Phase", value=f"`{league.current_phase}`", inline=True)
    embed.add_field(name="Season", value=str(league.current_season), inline=True)

    if available_commands:
        cmd_text = "\n".join(f"`/{c.replace('_', ' ')}`" for c in sorted(available_commands))
    else:
        cmd_text = "No commands available in this phase."
    embed.add_field(name="Available Commands", value=cmd_text, inline=False)

    if blockers:
        embed.add_field(name="Blockers", value="\n".join(blockers), inline=False)
        embed.color = discord.Color.yellow()

    return embed
