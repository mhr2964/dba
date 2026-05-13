from __future__ import annotations

import discord

_DAILY_OFFER_LIMIT = 3  # mirrors fa_service.DAILY_OFFER_LIMIT; update both if changed


def fa_day_open_embed(fa_state: dict, day: int, league: object) -> discord.Embed:
    league_name = getattr(league, "name", "League")
    total = fa_state.get("total_days", 8)
    embed = discord.Embed(
        title=f"Free Agency Day {day} of {total} — {league_name}",
        description=(
            f"Offers are now open for day {day}. "
            f"Each team may submit up to **{_DAILY_OFFER_LIMIT} offers** today."
        ),
        color=discord.Color.green(),
    )
    embed.set_footer(text="Use /fa offer <player_id> <salary> <years> to submit an offer.")
    return embed


def fa_responses_embed(decisions: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="Free Agency — Player Responses",
        color=discord.Color.orange(),
    )

    lines: list[str] = []
    for d in decisions:
        player = d.get("player_name", "Unknown")
        dec = d.get("decision", "?")
        team = d.get("team_name", "Unknown")

        if dec == "signed":
            lines.append(f"**{player}** signed with **{team}**")
        elif dec == "countered":
            lines.append(f"**{player}** countered **{team}** — check #transactions")
        elif dec == "waiting":
            lines.append(f"**{player}** is waiting — still considering offers from **{team}**")
        elif dec == "declined":
            lines.append(f"**{player}** declined **{team}**")
        else:
            lines.append(f"**{player}** — {dec} (team: {team})")

    embed.description = "\n".join(lines) if lines else "No player responses to report."
    return embed


def fa_closed_embed(signed_count: int, waived_count: int, retired_count: int) -> discord.Embed:
    embed = discord.Embed(
        title="Free Agency Closed",
        color=discord.Color.dark_gray(),
    )
    embed.add_field(name="Signed", value=str(signed_count), inline=True)
    embed.add_field(name="Waived", value=str(waived_count), inline=True)
    embed.add_field(name="Retired", value=str(retired_count), inline=True)
    embed.set_footer(text="Unsigned players with OVR >= 65 have been placed on waivers.")
    return embed


def counter_offer_embed(player: dict, counter: dict, team: dict) -> discord.Embed:
    player_name = player.get("name", "Unknown")
    player_ovr = player.get("overall", "?")
    salary = counter.get("salary_per_year", 0)
    years = counter.get("years", 1)
    team_name = team.get("name", "Unknown")

    embed = discord.Embed(
        title=f"Counter Offer — {player_name}",
        description=(
            f"**{player_name}** (OVR {player_ovr}) has countered your offer.\n\n"
            f"**Counter:** ${salary:,}/yr × {years} yr\n"
            f"**Team:** {team_name}"
        ),
        color=discord.Color.yellow(),
    )
    embed.set_footer(
        text="Use /fa counter-accept <counter_id> or /fa counter-decline <counter_id> to respond."
    )
    return embed
