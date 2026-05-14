from __future__ import annotations

from typing import Optional

import discord

_PACE_LABELS: dict[str, str] = {
    "slow": "Slow (-6 pace, grind it out)",
    "balanced": "Balanced (no change)",
    "fast": "Fast (+5 pace)",
    "run_and_gun": "Run & Gun (+10 pace, +1.5 TOV)",
}

_OFFENSE_LABELS: dict[str, str] = {
    "isolation": "Isolation (+3% PPP, +25% star usage, -5% 3-rate)",
    "ball_movement": "Ball Movement (+5% PPP, -15% star usage, +5% 3-rate)",
    "three_heavy": "Three Heavy (-3% PPP, +18% 3-rate, high variance)",
    "inside_out": "Inside Out (-8% 3-rate, finishing bonus in PPP)",
    "balanced": "Balanced (no adjustments)",
}

_DEFENSE_LABELS: dict[str, str] = {
    "man_to_man": "Man-to-Man (no adjustment)",
    "zone": "Zone (+3% defensive PPP, opponent -8% 3-rate)",
    "press": "Press (opponent +2.5 TOV, +3 shared pace)",
    "switch_all": "Switch All (+5% defensive PPP, opponent +5% 3-rate)",
}

_INTENSITY_LABELS: dict[str, str] = {
    "conservative": "Conservative (-1.5 fouls, -3% defensive PPP)",
    "normal": "Normal (no adjustment)",
    "aggressive": "Aggressive (+1.5 fouls, +4% defensive PPP, opponent +0.8 TOV)",
}


def strategy_embed(team: object, strategy: dict, modifiers: dict) -> discord.Embed:
    team_name = getattr(team, "full_name", None) or f"{team['city']} {team['name']}"  # type: ignore[index]
    embed = discord.Embed(
        title=f"{team_name} — Team Strategy",
        color=discord.Color.blue(),
    )

    embed.add_field(
        name="Offensive Pace",
        value=_PACE_LABELS.get(strategy.get("offensive_pace", "balanced"), strategy.get("offensive_pace", "balanced")),
        inline=False,
    )
    embed.add_field(
        name="Offensive Scheme",
        value=_OFFENSE_LABELS.get(strategy.get("offensive_scheme", "balanced"), strategy.get("offensive_scheme", "balanced")),
        inline=False,
    )
    embed.add_field(
        name="Defensive Scheme",
        value=_DEFENSE_LABELS.get(strategy.get("defensive_scheme", "man_to_man"), strategy.get("defensive_scheme", "man_to_man")),
        inline=False,
    )
    embed.add_field(
        name="Defensive Intensity",
        value=_INTENSITY_LABELS.get(strategy.get("defensive_intensity", "normal"), strategy.get("defensive_intensity", "normal")),
        inline=False,
    )
    embed.add_field(
        name="Star Usage",
        value=f"{strategy.get('star_usage', 50)}/100 → {modifiers.get('star_usage_mult', 1.0):.2f}x scoring weight for top-2 players",
        inline=False,
    )

    pace_adj = modifiers.get("pace_adjustment", 0.0)
    ppp_off = (modifiers.get("ppp_offense_mult", 1.0) - 1.0) * 100
    ppp_def = (modifiers.get("ppp_defense_mult", 1.0) - 1.0) * 100
    three_adj = modifiers.get("three_rate_adj", 0.0) * 100
    tov_adj = modifiers.get("turnover_adj", 0.0)
    foul_adj = modifiers.get("foul_adj", 0.0)

    summary_lines = [
        f"Pace: {pace_adj:+.0f} possessions/game",
        f"Offense PPP: {ppp_off:+.1f}%",
        f"Defense PPP (vs opponent): {ppp_def:+.1f}%",
        f"3-point rate: {three_adj:+.1f}%",
        f"Turnovers: {tov_adj:+.1f}/game",
        f"Fouls: {foul_adj:+.1f}/game",
    ]
    embed.add_field(name="Net Sim Modifiers", value="\n".join(summary_lines), inline=False)

    return embed


def minutes_embed(team: object, players: list[dict], minutes_plan: dict[int, float]) -> discord.Embed:
    team_name = getattr(team, "full_name", None) or f"{team['city']} {team['name']}"  # type: ignore[index]
    embed = discord.Embed(
        title=f"{team_name} — Minutes Plan",
        color=discord.Color.teal(),
    )

    rows: list[str] = []
    target_total = 0
    for p in sorted(players, key=lambda x: x.get("overall", 0), reverse=True):
        pid = p["id"]
        target = p.get("_target_minutes", 0)
        planned = minutes_plan.get(pid, 0.0)
        target_label = str(target) if target > 0 else "Auto"
        target_total += target
        name = p.get("full_name") or f"{p.get('first_name', '?')} {p.get('last_name', '')}".strip()
        rows.append(
            f"`{p.get('position', '??'):2}` **{name}** "
            f"OVR {p.get('overall', '?')} — Target: {target_label} | Planned: {planned:.1f}"
        )

    embed.description = "\n".join(rows) if rows else "No players found."
    total_planned = sum(minutes_plan.values())
    embed.set_footer(
        text=f"Target total: {target_total} min | Planned total: {total_planned:.1f} min (normalized to 240)"
    )
    return embed


def extension_embed(player: dict, extension: dict, team: object) -> discord.Embed:
    team_name = getattr(team, "full_name", None) or str(team)
    player_name = player.get("full_name") or f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
    embed = discord.Embed(
        title=f"Contract Extension — {player_name}",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Team", value=team_name, inline=True)
    embed.add_field(name="Salary", value=f"${extension['new_salary']:,}/yr", inline=True)
    embed.add_field(name="Years", value=str(extension["new_years"]), inline=True)
    embed.add_field(
        name="Activates After Season",
        value=str(extension["activates_after_season"]),
        inline=True,
    )
    embed.add_field(name="Signed In Season", value=str(extension["signed_in_season"]), inline=True)
    embed.set_footer(text="Extension will activate when the current contract expires.")
    return embed


def extension_list_embed(extensions: list[dict], players_by_id: dict[int, dict]) -> discord.Embed:
    embed = discord.Embed(
        title="Pending Contract Extensions",
        color=discord.Color.gold(),
    )
    if not extensions:
        embed.description = "No pending extensions."
        return embed

    lines: list[str] = []
    for ext in extensions:
        player = players_by_id.get(ext["player_id"], {})
        name = player.get("full_name") or f"Player #{ext['player_id']}"
        lines.append(
            f"**{name}** — ${ext['new_salary']:,}/yr × {ext['new_years']}yr "
            f"(activates after season {ext['activates_after_season']})"
        )
    embed.description = "\n".join(lines)
    return embed
