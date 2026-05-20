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
        return f"`{p.position}` {p.full_name} | OVR {p.overall} | {sal} / {yrs} · ID: {p.id}"

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


def roster_embed_with_roles(
    team: object,
    players: List[Player],
    contracts_by_player_id: Dict[int, Contract],
    cap_summary: dict,
    roles_by_player_id: Dict[int, dict],
) -> discord.Embed:
    """Roster embed extended to show role + lock icon alongside each player.

    Each player line:
      POS  Name  OVR 90  primary_initiator (24%)  🔒 [human]
    Players with no player_roles row omit the role column entirely.
    """
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

        role_row = roles_by_player_id.get(p.id)
        if role_row:
            role_name = (role_row.get("role") or "?").replace("_", " ")
            share_pct = int(round((role_row.get("touch_share") or 0) * 100))
            lock_icon = " \U0001f512" if role_row.get("locked") else ""
            assigned_by = role_row.get("assigned_by") or "cpu"
            by_label = "human" if str(assigned_by).startswith("human:") else "cpu"
            role_str = f"  {role_name} ({share_pct}%){lock_icon} [{by_label}]"
        else:
            role_str = ""

        return (
            f"`{p.position}` {p.full_name} | OVR {p.overall} | {sal} / {yrs}{role_str}"
            f" · ID: {p.id}"
        )

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
