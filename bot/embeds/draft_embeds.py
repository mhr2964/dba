from __future__ import annotations

from typing import List

import discord


def lottery_result_embed(ordered_picks: list[dict]) -> discord.Embed:
    """
    Full lottery order. Picks 1-4 are the lottery winners; highlight if they
    diverge from expected (expected = worst team gets pick 1).
    """
    embed = discord.Embed(
        title="NBA Draft Lottery Results",
        color=discord.Color.gold(),
    )

    lines: list[str] = []
    for entry in ordered_picks:
        pick = entry["pick_number"]
        name = entry["team_name"]
        marker = " **LOTTERY WIN**" if pick <= 4 else ""
        lines.append(f"**#{pick}** — {name}{marker}")

    embed.description = "\n".join(lines)
    embed.set_footer(text="Lottery complete. Use /draft advance to begin the draft.")
    return embed


def pick_announcement(pick_number: int, team: object, player: dict) -> discord.Embed:
    position = player.get("position", "?")
    ovr = player.get("overall", "?")
    name = f"{player['first_name']} {player['last_name']}"
    team_name = getattr(team, "full_name", str(team))

    embed = discord.Embed(
        title=f"Pick #{pick_number}",
        description=f"**{team_name}** selects **{name}** ({position}, OVR {ovr})",
        color=discord.Color.orange(),
    )
    return embed


def on_the_clock_embed(team: object, pick_number: int, prospects: list[dict]) -> discord.Embed:
    team_name = getattr(team, "full_name", str(team))
    embed = discord.Embed(
        title=f"On the Clock — Pick #{pick_number}",
        description=f"**{team_name}** is now on the clock. Select a player below.",
        color=discord.Color.blue(),
    )

    top10 = prospects[:10]
    lines = [
        f"**{i + 1}.** {p['first_name']} {p['last_name']} ({p['position']}) — OVR {p['overall']}"
        for i, p in enumerate(top10)
    ]
    embed.add_field(name="Top Available Prospects", value="\n".join(lines) or "None", inline=False)
    return embed


def draft_complete_embed(selections_summary: list[dict]) -> discord.Embed:
    """
    Full draft board. selections_summary: list of {pick_number, round, team_name, player_name, position, overall}.
    First round entries are bolded.
    """
    embed = discord.Embed(
        title="Draft Complete — Full Draft Board",
        color=discord.Color.dark_green(),
    )

    round1 = [s for s in selections_summary if s.get("round") == 1]
    round2 = [s for s in selections_summary if s.get("round", 1) > 1]

    def _line(s: dict) -> str:
        return (
            f"**#{s['pick_number']}** {s['team_name']} — "
            f"{s['player_name']} ({s['position']}, OVR {s['overall']})"
        )

    def _add_chunked(label: str, picks: list[dict], chunk: int = 10) -> None:
        for i in range(0, max(len(picks), 1), chunk):
            batch = picks[i:i + chunk]
            suffix = f" ({i + 1}–{min(i + chunk, len(picks))})" if len(picks) > chunk else ""
            embed.add_field(
                name=f"{label}{suffix}",
                value="\n".join(_line(s) for s in batch) or "—",
                inline=False,
            )

    if round1:
        _add_chunked("First Round", round1)
    if round2:
        _add_chunked("Second Round", round2)

    embed.set_footer(text=f"{len(selections_summary)} total picks recorded.")
    return embed
