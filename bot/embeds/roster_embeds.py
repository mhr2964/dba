from __future__ import annotations

from typing import Dict, List, Optional

import discord

from data.repositories.player_repo import Contract, Player

# Slot index → positional label for the starting five display.
_STARTER_LABELS = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}


def roster_embed(
    team: object,
    players: List[Player],
    contracts_by_player_id: Dict[int, Contract],
    cap_summary: dict,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{team['city']} {team['name']} — Roster",
        color=discord.Color.orange(),
    )

    sorted_players = sorted(players, key=lambda p: p.overall, reverse=True)
    starters = sorted_players[:5]
    bench = sorted_players[5:]

    def player_line(p: Player) -> str:
        contract: Optional[Contract] = contracts_by_player_id.get(p.id)
        if contract:
            sal = f"${contract.salary:,.0f}"
            yrs = f"{contract.years_remaining}yr"
        else:
            sal = "N/A"
            yrs = "—"
        return f"`{p.position}` {p.full_name} | OVR {p.overall} | {sal} / {yrs}"

    if starters:
        embed.add_field(
            name="Starters",
            value="\n".join(player_line(p) for p in starters),
            inline=False,
        )

    if bench:
        embed.add_field(
            name="Bench",
            value="\n".join(player_line(p) for p in bench),
            inline=False,
        )

    used = cap_summary["used"]
    cap = cap_summary["cap"]
    remaining = cap_summary["remaining"]
    embed.set_footer(
        text=f"Cap: ${used:,.0f} / ${cap:,.0f} (${remaining:,.0f} remaining)"
    )
    return embed


def lineup_embed(team: object, lineup_rows: List[tuple[Player, bool, int]]) -> discord.Embed:
    embed = discord.Embed(
        title=f"{team['city']} {team['name']} — Starting Lineup",
        color=discord.Color.orange(),
    )

    starters = [(p, slot) for p, is_starter, slot in lineup_rows if is_starter]
    bench = [(p, slot) for p, is_starter, slot in lineup_rows if not is_starter]

    if starters:
        lines = []
        for p, slot in starters:
            label = _STARTER_LABELS.get(slot, f"S{slot}")
            lines.append(f"**{label}** — {p.full_name} | OVR {p.overall}")
        embed.add_field(name="Starting Five", value="\n".join(lines), inline=False)

    if bench:
        lines = []
        for p, slot in bench:
            lines.append(f"{slot}. {p.full_name} (`{p.position}`) | OVR {p.overall}")
        embed.add_field(name="Bench", value="\n".join(lines), inline=False)

    return embed
