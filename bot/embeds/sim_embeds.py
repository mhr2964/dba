from __future__ import annotations

from typing import List, Optional

import discord


def game_recap(
    game: dict,
    home_team,
    away_team,
    home_score: int,
    away_score: int,
) -> discord.Embed:
    home_name = home_team.full_name if home_team else str(game.get("home_team_id"))
    away_name = away_team.full_name if away_team else str(game.get("away_team_id"))
    winner = home_name if home_score >= away_score else away_name
    color = discord.Color.green() if home_score >= away_score else discord.Color.red()

    embed = discord.Embed(
        title=f"{away_name} @ {home_name}",
        description=f"**{away_score} – {home_score}** | Winner: {winner}",
        color=color,
    )
    embed.set_footer(text=f"Game #{game.get('game_index', '?')} | {game.get('scheduled_date', '')}")
    return embed


def batch_recap(games_results: List[dict], title: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    lines = []
    for r in games_results:
        game = r["game"]
        home_team = r["home_team"]
        away_team = r["away_team"]
        result = r["result"]
        home_name = home_team.nba_team_code if home_team else "???"
        away_name = away_team.nba_team_code if away_team else "???"
        lines.append(
            f"`{away_name}` @ `{home_name}`:  **{result['away_score']}–{result['home_score']}**"
        )
    embed.description = "\n".join(lines) if lines else "No games."
    return embed


def matchup_alert(
    game: dict,
    home_team,
    away_team,
    home_manager: Optional[discord.Member],
    away_manager: Optional[discord.Member],
) -> discord.Embed:
    home_name = home_team.full_name if home_team else str(game.get("home_team_id"))
    away_name = away_team.full_name if away_team else str(game.get("away_team_id"))
    home_mention = home_manager.mention if home_manager else "(no manager)"
    away_mention = away_manager.mention if away_manager else "(no manager)"

    embed = discord.Embed(
        title="Upcoming User Matchup",
        description=f"**{away_name}** @ **{home_name}**",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Home Manager", value=home_mention, inline=True)
    embed.add_field(name="Away Manager", value=away_mention, inline=True)
    embed.add_field(
        name="Game",
        value=f"#{game.get('game_index', '?')} — {game.get('scheduled_date', '')}",
        inline=False,
    )
    embed.add_field(
        name="What to do",
        value="Both managers must use `/ready` before the commissioner can advance past this game.",
        inline=False,
    )
    return embed


def _championship_odds(standings_rows: List[dict]) -> dict[int, float]:
    """
    Compute per-team championship odds as a percentage (summing to ~100%).

    Formula: raw_score = win_rate^2 * seed_factor
    Seed factor is assigned per conference seed: 1->1.4, 2->1.2, 3-4->1.0, 5-6->0.7, 7-8->0.3.
    Teams outside the top 8 per conference receive seed_factor=0.1.
    Scores are normalized across all 30 teams so the total is 100%.
    """
    _SEED_FACTORS = {1: 1.4, 2: 1.2, 3: 1.0, 4: 1.0, 5: 0.7, 6: 0.7, 7: 0.3, 8: 0.3}

    east = sorted(
        [r for r in standings_rows if r["conference"] == "East"],
        key=lambda r: (-r["wins"], r["losses"]),
    )
    west = sorted(
        [r for r in standings_rows if r["conference"] == "West"],
        key=lambda r: (-r["wins"], r["losses"]),
    )

    raw: dict[int, float] = {}
    for conf_rows in (east, west):
        for seed, row in enumerate(conf_rows, start=1):
            games = row["wins"] + row["losses"]
            win_rate = row["wins"] / games if games > 0 else 0.0
            seed_factor = _SEED_FACTORS.get(seed, 0.1)
            raw[row["team_id"]] = (win_rate ** 2) * seed_factor

    total = sum(raw.values()) or 1.0
    return {team_id: (score / total) * 100.0 for team_id, score in raw.items()}


def standings_embed(standings_rows: List[dict], teams_by_id: dict) -> discord.Embed:
    embed = discord.Embed(title="League Standings", color=discord.Color.orange())

    odds = _championship_odds(standings_rows)

    east = [r for r in standings_rows if r["conference"] == "East"]
    west = [r for r in standings_rows if r["conference"] == "West"]

    def _fmt(rows: List[dict]) -> str:
        lines = []
        for i, r in enumerate(rows, start=1):
            team = teams_by_id.get(r["team_id"])
            name = team.nba_team_code if team else str(r["team_id"])
            pct = odds.get(r["team_id"], 0.0)
            lines.append(f"`{i:2}.` **{name}**  {r['wins']}–{r['losses']}  `{pct:.1f}%`")
        return "\n".join(lines) or "No data"

    embed.add_field(name="Eastern Conference", value=_fmt(east), inline=True)
    embed.add_field(name="Western Conference", value=_fmt(west), inline=True)
    embed.set_footer(text="% = estimated championship odds")
    return embed


def user_matchup_warning(matchup_list: List[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="Warning: User Matchups in Range",
        description=(
            "The following user vs. user games fall within the requested sim range. "
            "Use `/sim rivalry` to stop before each, or pass `force:True` to skip them."
        ),
        color=discord.Color.yellow(),
    )
    lines = [
        f"Game #{m.get('game_index', '?')} — {m.get('scheduled_date', '')} "
        f"(home: {m.get('home_team_id')} vs away: {m.get('away_team_id')})"
        for m in matchup_list
    ]
    embed.add_field(name="Affected Games", value="\n".join(lines) or "None", inline=False)
    return embed
