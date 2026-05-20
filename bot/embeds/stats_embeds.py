from __future__ import annotations

from typing import Any

import discord

from data.repositories.player_repo import Contract, Player
from services.player_style_service import style_tags as _style_tags, tendencies_block as _tendencies_block


_STAT_DISPLAY: dict[str, str] = {
    "ppg": "PPG",
    "rpg": "RPG",
    "apg": "APG",
    "spg": "SPG",
    "bpg": "BPG",
    "fg_pct": "FG%",
    "fg3_pct": "3P%",
    "mpg": "MPG",
    "ts_pct": "TS%",
    "usg_pct": "USG%",
    "per": "PER",
}

_PCT_COLS = frozenset({"fg_pct", "fg3_pct", "ts_pct", "usg_pct"})


def _fmt_stat(col: str, value: float | None) -> str:
    if value is None:
        return "—"
    if col in _PCT_COLS:
        return f"{value:.1f}%"
    return f"{value:.1f}"


def leaders_embed(stat_name: str, players_with_stats: list[tuple[Any, float]]) -> discord.Embed:
    """Numbered top-10 list for one stat category."""
    label = _STAT_DISPLAY.get(stat_name, stat_name.upper())
    embed = discord.Embed(
        title=f"League Leaders — {label}",
        color=discord.Color.gold(),
    )
    lines: list[str] = []
    for i, (player, value) in enumerate(players_with_stats, start=1):
        name = getattr(player, "full_name", str(player))
        position = getattr(player, "position", "?")
        lines.append(f"**{i}.** {name} ({position}) — {_fmt_stat(stat_name, value)}")
    embed.description = "\n".join(lines) if lines else "No data available."
    return embed


def player_profile_embed(
    player: Player,
    team_name: str,
    contract: Contract | None,
    season_stats: dict[str, Any] | None,
    recent_games: list[dict[str, Any]],
    nba_profile: dict | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=player.full_name,
        color=discord.Color.blue(),
    )
    embed.add_field(name="Position", value=player.position, inline=True)
    embed.add_field(name="Team", value=team_name, inline=True)
    embed.add_field(name="OVR", value=str(player.overall), inline=True)

    if season_stats:
        avg_line = (
            f"PPG {_fmt_stat('ppg', season_stats.get('ppg'))} | "
            f"RPG {_fmt_stat('rpg', season_stats.get('rpg'))} | "
            f"APG {_fmt_stat('apg', season_stats.get('apg'))} | "
            f"SPG {_fmt_stat('spg', season_stats.get('spg'))} | "
            f"BPG {_fmt_stat('bpg', season_stats.get('bpg'))} | "
            f"FG {_fmt_stat('fg_pct', season_stats.get('fg_pct'))} | "
            f"3P {_fmt_stat('fg3_pct', season_stats.get('fg3_pct'))} | "
            f"MPG {_fmt_stat('mpg', season_stats.get('mpg'))}"
        )
        embed.add_field(name="Season Averages", value=avg_line, inline=False)

        adv_line = (
            f"TS% {_fmt_stat('ts_pct', season_stats.get('ts_pct'))} | "
            f"USG% {_fmt_stat('usg_pct', season_stats.get('usg_pct'))} | "
            f"PER {_fmt_stat('per', season_stats.get('per'))}"
        )
        embed.add_field(name="Advanced", value=adv_line, inline=False)

    if contract:
        contract_line = (
            f"${contract.salary:,}/yr — {contract.years_remaining}yr remaining "
            f"({contract.contract_type})"
        )
        embed.add_field(name="Contract", value=contract_line, inline=False)

    if nba_profile:
        embed.add_field(name="Playing Style", value=_style_tags(nba_profile), inline=False)
        embed.add_field(name="Tendencies", value=_tendencies_block(nba_profile), inline=False)

    if recent_games:
        rows = ["```", f"{'Date':<12} {'PTS':>4} {'REB':>4} {'AST':>4} {'STL':>4} {'BLK':>4}"]
        for g in recent_games[:5]:
            date_str = str(g.get("game_date", ""))[:10]
            rows.append(
                f"{date_str:<12} "
                f"{g.get('points', 0):>4} "
                f"{g.get('rebounds', 0):>4} "
                f"{g.get('assists', 0):>4} "
                f"{g.get('steals', 0):>4} "
                f"{g.get('blocks', 0):>4}"
            )
        rows.append("```")
        embed.add_field(name="Last 5 Games", value="\n".join(rows), inline=False)

    return embed


def career_stats_embed(
    player: Player, career: dict, current_team_name: str, nba_profile: dict | None = None
) -> discord.Embed:
    """Career totals, averages, career high, and compact per-season table."""
    embed = discord.Embed(
        title=f"{player.full_name} — Career Stats",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Position", value=player.position, inline=True)
    embed.add_field(name="Team", value=current_team_name, inline=True)
    embed.add_field(name="OVR", value=str(player.overall), inline=True)

    avg_line = (
        f"PPG {_fmt_stat('ppg', career.get('ppg'))} | "
        f"RPG {_fmt_stat('rpg', career.get('rpg'))} | "
        f"APG {_fmt_stat('apg', career.get('apg'))} | "
        f"SPG {_fmt_stat('spg', career.get('spg'))} | "
        f"BPG {_fmt_stat('bpg', career.get('bpg'))} | "
        f"FG {_fmt_stat('fg_pct', career.get('fg_pct'))}"
    )
    embed.add_field(name="Career Averages", value=avg_line, inline=False)

    totals_line = (
        f"GP {career.get('games_played', 0)} | "
        f"Seasons {career.get('seasons_played', 0)} | "
        f"Total Pts {career.get('total_points', 0)} | "
        f"Career High {career.get('career_high_pts', 0)} pts"
    )
    embed.add_field(name="Career Totals", value=totals_line, inline=False)

    seasons: list[dict] = career.get("seasons", [])
    if seasons:
        header = f"{'Ssn':<6} {'GP':>4} {'PPG':>5} {'RPG':>5} {'APG':>5}"
        rows = ["```", header]
        for s in seasons:
            rows.append(
                f"{str(s.get('season', '?')):<6} "
                f"{int(s.get('games_played', 0)):>4} "
                f"{float(s.get('ppg') or 0):>5.1f} "
                f"{float(s.get('rpg') or 0):>5.1f} "
                f"{float(s.get('apg') or 0):>5.1f}"
            )
        rows.append("```")
        embed.add_field(name="Season History", value="\n".join(rows), inline=False)

    if nba_profile:
        embed.add_field(name="Playing Style", value=_style_tags(nba_profile), inline=False)
        embed.add_field(name="Tendencies", value=_tendencies_block(nba_profile), inline=False)

    return embed


def all_time_leaders_embed(
    stat_name: str, leaders: list[dict]
) -> discord.Embed:
    """Numbered career leaderboard for one stat."""
    _CAREER_LABELS: dict[str, str] = {
        "ppg": "PPG (Career)",
        "rpg": "RPG (Career)",
        "apg": "APG (Career)",
        "spg": "SPG (Career)",
        "bpg": "BPG (Career)",
        "fg_pct": "FG% (Career)",
        "pts_total": "Total Points",
    }
    label = _CAREER_LABELS.get(stat_name, stat_name.upper())
    embed = discord.Embed(
        title=f"All-Time Leaders — {label}",
        color=discord.Color.gold(),
    )
    lines: list[str] = []
    for i, row in enumerate(leaders, start=1):
        name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
        value = row.get("value")
        if value is None:
            val_str = "—"
        elif stat_name in ("fg_pct",):
            val_str = f"{float(value):.1f}%"
        elif stat_name == "pts_total":
            val_str = f"{int(value):,}"
        else:
            val_str = f"{float(value):.1f}"
        gp = row.get("games_played", 0)
        lines.append(f"**{i}.** {name} — {val_str} ({gp} GP)")
    embed.description = "\n".join(lines) if lines else "No data available."
    return embed


def all_time_records_embed(records: dict) -> discord.Embed:
    """Rich embed showing notable all-time league records."""
    embed = discord.Embed(
        title="All-Time League Records",
        color=discord.Color.gold(),
    )

    champ = records.get("most_championships")
    embed.add_field(
        name="Most Championships",
        value=(
            f"{champ['team_name']} ({champ['count']}x)"
            if champ
            else "N/A"
        ),
        inline=True,
    )

    mvp = records.get("most_mvps")
    embed.add_field(
        name="Most MVPs",
        value=(
            f"{mvp['player_name']} ({mvp['count']}x)"
            if mvp
            else "N/A"
        ),
        inline=True,
    )

    pts = records.get("most_points_career")
    embed.add_field(
        name="Most Career Points",
        value=(
            f"{pts['player_name']} ({int(pts['points']):,} pts)"
            if pts
            else "N/A"
        ),
        inline=True,
    )

    ovr = records.get("highest_ovr_ever")
    embed.add_field(
        name="Highest OVR (Current)",
        value=(
            f"{ovr['player_name']} ({ovr['ovr']} OVR)"
            if ovr
            else "N/A"
        ),
        inline=True,
    )

    streak = records.get("longest_win_streak")
    embed.add_field(
        name="Longest Win Streak",
        value=(
            f"{streak['team_name']} — {streak['streak']}W (Season {streak['season']})"
            if streak
            else "N/A"
        ),
        inline=True,
    )

    return embed


def compare_embed(
    player_a: Player,
    stats_a: dict[str, Any] | None,
    player_b: Player,
    stats_b: dict[str, Any] | None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{player_a.full_name} vs {player_b.full_name}",
        color=discord.Color.purple(),
    )

    def _ovr_line(p: Player) -> str:
        return f"OVR {p.overall} | {p.position}"

    embed.add_field(name=player_a.full_name, value=_ovr_line(player_a), inline=True)
    embed.add_field(name="​", value="​", inline=True)
    embed.add_field(name=player_b.full_name, value=_ovr_line(player_b), inline=True)

    stat_cols = [("ppg", "PPG"), ("rpg", "RPG"), ("apg", "APG"), ("spg", "SPG"), ("bpg", "BPG"), ("fg_pct", "FG%")]
    for col, label in stat_cols:
        val_a = _fmt_stat(col, (stats_a or {}).get(col))
        val_b = _fmt_stat(col, (stats_b or {}).get(col))
        embed.add_field(name=f"{label} — {player_a.last_name}", value=val_a, inline=True)
        embed.add_field(name="​", value="​", inline=True)
        embed.add_field(name=f"{label} — {player_b.last_name}", value=val_b, inline=True)

    return embed
