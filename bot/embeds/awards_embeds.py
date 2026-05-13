from __future__ import annotations

from typing import Dict, List

import discord

_AWARD_LABELS: dict[str, str] = {
    "mvp": "Most Valuable Player",
    "dpoy": "Defensive Player of the Year",
    "roy": "Rookie of the Year",
    "6moy": "Sixth Man of the Year",
    "all_nba_1": "All-NBA First Team",
    "all_nba_2": "All-NBA Second Team",
    "all_nba_3": "All-NBA Third Team",
    "all_star_east": "All-Star Eastern Conference",
    "all_star_west": "All-Star Western Conference",
}

_AWARD_COLOR: dict[str, discord.Color] = {
    "mvp": discord.Color.gold(),
    "dpoy": discord.Color.blue(),
    "roy": discord.Color.green(),
    "6moy": discord.Color.purple(),
    "all_nba_1": discord.Color.gold(),
    "all_nba_2": discord.Color.light_gray(),
    "all_nba_3": discord.Color.orange(),
    "all_star_east": discord.Color.red(),
    "all_star_west": discord.Color.blue(),
}


def _player_label(player_id: int, players_by_id: Dict[int, object]) -> str:
    p = players_by_id.get(player_id)
    if p is None:
        return f"Player #{player_id}"
    return getattr(p, "full_name", f"Player #{player_id}")


def voting_open_embed(
    voting: dict,
    award_type: str,
    top_candidates: List[dict],
) -> discord.Embed:
    """
    voting: dict with keys id, league_id, season, award_type, status, opened_at
    top_candidates: list of {player_id, name, ppg, rpg, apg, ...} for up to 5 candidates
    """
    label = _AWARD_LABELS.get(award_type, award_type.upper())
    color = _AWARD_COLOR.get(award_type, discord.Color.orange())

    embed = discord.Embed(
        title=f"Voting Open — {label}",
        description=f"Season {voting['season']} | Use `/awards vote` to cast your ballot.",
        color=color,
    )

    if top_candidates:
        lines: list[str] = []
        for c in top_candidates[:5]:
            name = c.get("name", f"Player #{c.get('player_id', '?')}")
            ppg = c.get("ppg", 0.0)
            rpg = c.get("rpg", 0.0)
            apg = c.get("apg", 0.0)
            lines.append(f"**{name}** — {ppg:.1f} PPG / {rpg:.1f} RPG / {apg:.1f} APG")
        embed.add_field(name="Top Candidates", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Voting ID: {voting['id']}")
    return embed


def award_result_embed(
    results: List[dict],
    award_type: str,
    players_by_id: Dict[int, object],
) -> discord.Embed:
    """
    results: [{place, player_id, vote_count, vote_share}] sorted by place
    Shows winner plus runner-up breakdown for top 3.
    """
    label = _AWARD_LABELS.get(award_type, award_type.upper())
    color = _AWARD_COLOR.get(award_type, discord.Color.orange())

    winner = results[0] if results else None
    winner_name = _player_label(winner["player_id"], players_by_id) if winner else "No votes cast"

    embed = discord.Embed(
        title=f"{label} — Winner",
        description=f"**{winner_name}**",
        color=color,
    )

    if len(results) > 1:
        lines: list[str] = []
        for r in results[:3]:
            name = _player_label(r["player_id"], players_by_id)
            share_pct = r["vote_share"] * 100
            lines.append(
                f"{r['place']}. {name} — {r['vote_count']} votes ({share_pct:.1f}%)"
            )
        embed.add_field(name="Vote Breakdown", value="\n".join(lines), inline=False)

    return embed


def all_nba_embed(
    first_team: List[dict],
    second_team: List[dict],
    third_team: List[dict],
    players_by_id: Dict[int, object],
) -> discord.Embed:
    """
    Each team is a list of {place, player_id, vote_count, vote_share}.
    """
    embed = discord.Embed(
        title="All-NBA Teams",
        color=discord.Color.gold(),
    )

    def team_lines(team: List[dict]) -> str:
        if not team:
            return "No selections"
        return "\n".join(
            _player_label(r["player_id"], players_by_id) for r in team
        )

    embed.add_field(name="First Team", value=team_lines(first_team), inline=True)
    embed.add_field(name="Second Team", value=team_lines(second_team), inline=True)
    embed.add_field(name="Third Team", value=team_lines(third_team), inline=True)
    return embed


def all_star_embed(
    east_team: List[dict],
    west_team: List[dict],
    players_by_id: Dict[int, object],
) -> discord.Embed:
    """
    east_team / west_team: [{place, player_id, ...}] sorted by place.
    """
    embed = discord.Embed(
        title="NBA All-Star Game Rosters",
        color=discord.Color.orange(),
    )

    def roster_lines(team: List[dict]) -> str:
        if not team:
            return "No selections"
        return "\n".join(
            f"{i + 1}. {_player_label(r['player_id'], players_by_id)}"
            for i, r in enumerate(team)
        )

    embed.add_field(
        name="Eastern Conference",
        value=roster_lines(east_team),
        inline=True,
    )
    embed.add_field(
        name="Western Conference",
        value=roster_lines(west_team),
        inline=True,
    )
    return embed
