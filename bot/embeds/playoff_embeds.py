from __future__ import annotations

from typing import Dict, List

import discord

from data.repositories.series_repo import Series
from data.repositories.team_repo import Team

_ROUND_LABELS: Dict[str, str] = {
    "play_in_east": "Eastern Play-In",
    "play_in_west": "Western Play-In",
    "r1_east": "East First Round",
    "r1_west": "West First Round",
    "r2_east": "East Second Round",
    "r2_west": "West Second Round",
    "conference_finals_east": "Eastern Conference Finals",
    "conference_finals_west": "Western Conference Finals",
    "nba_finals": "NBA Finals",
}

_ROUND_ORDER = [
    "r1_east",
    "r1_west",
    "r2_east",
    "r2_west",
    "conference_finals_east",
    "conference_finals_west",
    "nba_finals",
]


def _team_code(teams_by_id: Dict[int, Team], team_id: int) -> str:
    team = teams_by_id.get(team_id)
    return team.nba_team_code if team else str(team_id)


def _series_line(series: Series, teams_by_id: Dict[int, Team]) -> str:
    high = _team_code(teams_by_id, series.high_seed_team_id)
    low = _team_code(teams_by_id, series.low_seed_team_id)
    if series.status == "complete" and series.winner_team_id:
        winner = _team_code(teams_by_id, series.winner_team_id)
        loser_wins = series.wins_low if series.winner_team_id == series.high_seed_team_id else series.wins_high
        winner_wins = max(series.wins_high, series.wins_low)
        return f"**{winner}** def. {high if winner != high else low} ({winner_wins}–{loser_wins})"
    return f"{high} **{series.wins_high}** – **{series.wins_low}** {low}  `ID:{series.id}`"


def _matchup_str(series: Series, teams_by_id: Dict[int, Team]) -> str:
    """Fixed-width matchup line: 'HIGH (W-L) vs LOW' or 'WINNER def. LOSER (W-L)'."""
    high = _team_code(teams_by_id, series.high_seed_team_id)
    low = _team_code(teams_by_id, series.low_seed_team_id)
    if series.status == "complete" and series.winner_team_id:
        winner_code = _team_code(teams_by_id, series.winner_team_id)
        loser_code = high if winner_code == low else low
        w_wins = max(series.wins_high, series.wins_low)
        l_wins = min(series.wins_high, series.wins_low)
        return f"*** {winner_code} def {loser_code} {w_wins}-{l_wins} ***"
    return f"  {high} ({series.wins_high}-{series.wins_low}) vs {low}"


def _render_bracket_text(series_list: List[Series], teams_by_id: Dict[int, Team]) -> str:
    """
    Compact fixed-width bracket rendered as a Discord code block.
    East conference rounds on the left, West on the right, Finals in the middle.
    Winners are shown in ALL-CAPS with asterisks (markdown doesn't render in code blocks).
    """
    by_round: Dict[str, List[Series]] = {}
    for s in series_list:
        by_round.setdefault(s.round, []).append(s)

    east_rounds = ["r1_east", "r2_east", "conference_finals_east"]
    west_rounds = ["r1_west", "r2_west", "conference_finals_west"]

    lines: List[str] = ["```"]
    lines.append(f"{'EAST':^40}  {'WEST':^40}")
    lines.append("-" * 82)

    max_rounds = max(len(east_rounds), len(west_rounds))
    for i in range(max_rounds):
        east_key = east_rounds[i] if i < len(east_rounds) else None
        west_key = west_rounds[i] if i < len(west_rounds) else None

        east_label = _ROUND_LABELS.get(east_key, east_key or "") if east_key else ""
        west_label = _ROUND_LABELS.get(west_key, west_key or "") if west_key else ""
        lines.append(f"  {east_label:<38}  {west_label:<38}")

        east_series = by_round.get(east_key, []) if east_key else []
        west_series = by_round.get(west_key, []) if west_key else []
        n = max(len(east_series), len(west_series))
        for j in range(n):
            e_str = _matchup_str(east_series[j], teams_by_id) if j < len(east_series) else ""
            w_str = _matchup_str(west_series[j], teams_by_id) if j < len(west_series) else ""
            lines.append(f"  {e_str:<38}  {w_str:<38}")
        lines.append("")

    if "nba_finals" in by_round:
        lines.append(f"{'NBA FINALS':^82}")
        for s in by_round["nba_finals"]:
            lines.append(f"  {_matchup_str(s, teams_by_id):^78}  ")

    lines.append("```")
    return "\n".join(lines)


def bracket_embed(series_list: List[Series], teams_by_id: Dict[int, Team]) -> discord.Embed:
    embed = discord.Embed(title="Playoff Bracket", color=discord.Color.gold())

    by_round: Dict[str, List[Series]] = {}
    for s in series_list:
        by_round.setdefault(s.round, []).append(s)

    for round_name in _ROUND_ORDER:
        if round_name not in by_round:
            continue
        label = _ROUND_LABELS.get(round_name, round_name)
        lines = [_series_line(s, teams_by_id) for s in by_round[round_name]]
        embed.add_field(name=label, value="\n".join(lines), inline=False)

    if series_list:
        bracket_text = _render_bracket_text(series_list, teams_by_id)
        embed.add_field(name="Bracket", value=bracket_text, inline=False)

    return embed


def series_embed(series: Series, high_team: Team, low_team: Team) -> discord.Embed:
    round_label = _ROUND_LABELS.get(series.round, series.round)
    clinch = (series.games_needed // 2) + 1

    if series.status == "complete" and series.winner_team_id:
        winner = high_team if series.winner_team_id == high_team.id else low_team
        color = discord.Color.green()
        description = f"{winner.full_name} wins the series!"
    else:
        color = discord.Color.blurple()
        description = f"First to {clinch} wins"

    embed = discord.Embed(
        title=f"{round_label}: {high_team.nba_team_code} vs {low_team.nba_team_code}",
        description=description,
        color=color,
    )
    embed.add_field(
        name=high_team.nba_team_code,
        value=str(series.wins_high),
        inline=True,
    )
    embed.add_field(
        name=low_team.nba_team_code,
        value=str(series.wins_low),
        inline=True,
    )

    if series.status == "active":
        next_game = series.wins_high + series.wins_low + 1
        embed.set_footer(text=f"Next: Game {next_game} of series")

    return embed


def playin_embed(playin_series: List[Series], teams_by_id: Dict[int, Team]) -> discord.Embed:
    embed = discord.Embed(title="Play-In Tournament", color=discord.Color.orange())

    east = [s for s in playin_series if s.round == "play_in_east"]
    west = [s for s in playin_series if s.round == "play_in_west"]

    def _fmt(series_list: List[Series]) -> str:
        if not series_list:
            return "Not started"
        return "\n".join(_series_line(s, teams_by_id) for s in series_list)

    embed.add_field(name="Eastern Play-In", value=_fmt(east), inline=False)
    embed.add_field(name="Western Play-In", value=_fmt(west), inline=False)
    return embed


def champion_embed(team: Team) -> discord.Embed:
    embed = discord.Embed(
        title="NBA Champion",
        description=f"The **{team.full_name}** are NBA Champions!",
        color=0xFFD700,
    )
    embed.set_footer(text="Congratulations to the champions and their fans.")
    return embed
