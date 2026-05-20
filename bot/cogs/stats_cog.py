from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds.stats_embeds import (
    all_time_leaders_embed,
    all_time_records_embed,
    career_stats_embed,
    compare_embed,
    leaders_embed,
    player_profile_embed,
)
from bot.embeds.info_embeds import h2h_embed, power_rankings_embed
from bot.embeds.intel_embeds import power_rankings_posture_embed
from bot.ui.stats_views import LeaderboardView, _fetch_season_leaders
from core.errors import DBAError, safe_defer
from core.logging import get_logger
from data.db import get_pool
from data.repositories import player_repo, team_repo
from services import league_service, team_intel
from services.player_style_service import load_profile as _load_nba_profile

log = get_logger(__name__)


_CATEGORY_CHOICES = [
    app_commands.Choice(name="Points", value="ppg"),
    app_commands.Choice(name="Rebounds", value="rpg"),
    app_commands.Choice(name="Assists", value="apg"),
    app_commands.Choice(name="Steals", value="spg"),
    app_commands.Choice(name="Blocks", value="bpg"),
]


async def _season_stats_for_player(pool, player_id: int) -> dict | None:
    """Return per-game averages for a player from game_box_scores, or None if no games."""
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*)                                 AS games_played,
            AVG(points)                              AS ppg,
            AVG(rebounds_off + rebounds_def)         AS rpg,
            AVG(assists)                             AS apg,
            AVG(steals)                              AS spg,
            AVG(blocks)                              AS bpg,
            AVG(minutes)                             AS mpg,
            CASE WHEN SUM(fga) > 0
                THEN SUM(fgm)::FLOAT / SUM(fga) * 100
                ELSE NULL END                        AS fg_pct,
            CASE WHEN SUM(tpa) > 0
                THEN SUM(tpm)::FLOAT / SUM(tpa) * 100
                ELSE NULL END                        AS fg3_pct,
            -- True Shooting %: PTS / (2 * (FGA + 0.44 * FTA)) * 100
            CASE WHEN (SUM(fga) + 0.44 * SUM(fta)) > 0
                THEN SUM(points)::FLOAT / (2.0 * (SUM(fga) + 0.44 * SUM(fta))) * 100
                ELSE NULL END                        AS ts_pct,
            -- Simplified USG%: (FGA + 0.44*FTA + TOV) / MP * 48
            CASE WHEN SUM(minutes) > 0
                THEN (SUM(fga) + 0.44 * SUM(fta) + SUM(turnovers))::FLOAT / SUM(minutes) * 48
                ELSE NULL END                        AS usg_pct,
            -- Simplified PER: (PTS + REB + AST + STL + BLK - (FGA-FGM) - (FTA-FTM) - TOV) / GP
            CASE WHEN COUNT(*) > 0
                THEN (
                    SUM(points)
                    + SUM(rebounds_off + rebounds_def)
                    + SUM(assists)
                    + SUM(steals)
                    + SUM(blocks)
                    - SUM(fga - fgm)
                    - SUM(fta - ftm)
                    - SUM(turnovers)
                )::FLOAT / COUNT(*)
                ELSE NULL END                        AS per
        FROM game_box_scores
        WHERE player_id = $1
        """,
        player_id,
    )
    if row is None or row["ppg"] is None:
        return None
    return dict(row)


async def _recent_games_for_player(pool, player_id: int, n: int = 5) -> list[dict]:
    """Return last N game lines for a player, newest first.

    Orders by game_id (insertion order) rather than scheduled_date so that
    playoff games — which used to receive real-world wall-clock dates — don't
    sort above earlier regular-season games incorrectly.
    """
    rows = await pool.fetch(
        """
        SELECT
            g.scheduled_date            AS game_date,
            b.points,
            b.rebounds_off + b.rebounds_def AS rebounds,
            b.assists,
            b.steals,
            b.blocks
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE b.player_id = $1
        ORDER BY g.id DESC
        LIMIT $2
        """,
        player_id,
        n,
    )
    return [dict(r) for r in rows]


async def _resolve_player(
    pool, league_id: int, name_or_id: str
) -> player_repo.Player:
    """
    Resolve a player by numeric ID or name search within the league.
    Raises DBAError if nothing found or ambiguous.
    """
    if name_or_id.isdigit():
        player = await player_repo.get_by_id(pool, int(name_or_id))
        if player and player.league_id == league_id:
            return player
        raise DBAError(f"No player found with ID {name_or_id}.")

    matches = await player_repo.search_by_name(pool, league_id, name_or_id)
    if not matches:
        raise DBAError(f"No player found matching **{name_or_id}**.")
    if len(matches) > 1:
        names = ", ".join(p.full_name for p in matches[:5])
        raise DBAError(f"Multiple players matched: {names}. Be more specific.")
    return matches[0]


def compute_power_rankings(standings_rows: list[dict], teams_by_id: dict) -> list[dict]:
    """
    Return teams sorted by power score descending.
    score = (wins / max(1, wins+losses)) * 0.6 + (last_10_wins / 10) * 0.4

    trend arrow compares power rank to pure win-pct rank:
      ↑ = power rank better (lower number) than record rank
      ↓ = power rank worse than record rank
      → = same
    """
    enriched = []
    for row in standings_rows:
        games = row["wins"] + row["losses"]
        win_pct = row["wins"] / max(1, games)
        l10_wins = row.get("last_10_wins", 0)
        score = win_pct * 0.6 + (l10_wins / 10) * 0.4
        enriched.append({**row, "score": score, "win_pct": win_pct})

    by_power = sorted(enriched, key=lambda r: r["score"], reverse=True)
    by_record = sorted(enriched, key=lambda r: r["win_pct"], reverse=True)
    record_rank = {r["team_id"]: i for i, r in enumerate(by_record, start=1)}

    result = []
    for power_rank, row in enumerate(by_power, start=1):
        rec_rank = record_rank[row["team_id"]]
        if power_rank < rec_rank:
            trend = "↑"
        elif power_rank > rec_rank:
            trend = "↓"
        else:
            trend = "→"

        team = teams_by_id.get(row["team_id"])
        result.append({
            "rank": power_rank,
            "team_code": team.nba_team_code if team else str(row["team_id"]),
            "wins": row["wins"],
            "losses": row["losses"],
            "last_10_wins": row.get("last_10_wins", 0),
            "last_10_losses": row.get("last_10_losses", 0),
            "score": row["score"],
            "trend": trend,
        })
    return result


def compute_power_rankings_posture(
    standings_rows: list[dict],
    teams_by_id: dict,
    intel_by_team: dict,
    avg_ovr_by_team: dict,
    mode: str,
) -> list[dict]:
    """Return posture-aware ranked list for the extended /power-rankings command.

    Args:
        standings_rows: Raw rows from standings_cache.
        teams_by_id:    {team_id: Team} from team_repo.get_all.
        intel_by_team:  {team_id: {posture: dict}} from build_team_intel.
        avg_ovr_by_team:{team_id: float} team average OVR from players table.
        mode:           "record" | "expected" | "talent"

    Sort key:
        record   — power score (win% × 0.6 + L10 × 0.4)  [existing behavior]
        expected — posture.projected_wins DESC (None → 0)
        talent   — avg_ovr_by_team DESC (None → 0)
    """
    enriched = []
    for row in standings_rows:
        games = row["wins"] + row["losses"]
        win_pct = row["wins"] / max(1, games)
        l10_wins = row.get("last_10_wins", 0)
        score = win_pct * 0.6 + (l10_wins / 10) * 0.4

        tid = row["team_id"]
        posture = (intel_by_team.get(tid) or {}).get("posture")
        avg_ovr = avg_ovr_by_team.get(tid)

        enriched.append({
            **dict(row),
            "score": score,
            "win_pct": win_pct,
            "posture": posture,
            "avg_ovr": avg_ovr,
        })

    if mode == "expected":
        sorted_list = sorted(
            enriched,
            key=lambda r: (r.get("posture") or {}).get("projected_wins") or 0,
            reverse=True,
        )
    elif mode == "talent":
        sorted_list = sorted(
            enriched,
            key=lambda r: r.get("avg_ovr") or 0,
            reverse=True,
        )
    else:
        # "record" — existing power-score ordering
        sorted_list = sorted(enriched, key=lambda r: r["score"], reverse=True)

    result = []
    for rank, row in enumerate(sorted_list, start=1):
        team = teams_by_id.get(row["team_id"])
        result.append({
            "rank": rank,
            "team_code": team.nba_team_code if team else str(row["team_id"]),
            "wins": row["wins"],
            "losses": row["losses"],
            "last_10_wins": row.get("last_10_wins", 0),
            "last_10_losses": row.get("last_10_losses", 0),
            "score": row["score"],
            "trend": "→",  # legacy field; posture badge replaces it in the new embed
            "posture": row.get("posture"),
            "avg_ovr": row.get("avg_ovr"),
        })
    return result


def count_h2h_wins(games: list[dict], team_a_id: int, team_b_id: int) -> tuple[int, int]:
    """Return (wins_a, wins_b) from a list of simmed game dicts."""
    wins_a = 0
    wins_b = 0
    for g in games:
        winner = g.get("winner_team_id")
        if winner == team_a_id:
            wins_a += 1
        elif winner == team_b_id:
            wins_b += 1
    return wins_a, wins_b


class StatsGroup(app_commands.Group, name="stats", description="League statistics"):

    @app_commands.command(name="leaders", description="Show league leaders for a stat category")
    @app_commands.describe(
        category="Stat category to rank by",
        all_time="Show career leaders across all seasons instead of current season",
    )
    @app_commands.choices(category=_CATEGORY_CHOICES)
    async def leaders(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] = None,
        all_time: bool = False,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        stat_col = category.value if category else "ppg"
        pool = await get_pool()

        if all_time:
            try:
                leaders_data = await player_repo.get_all_time_leaders(pool, league.id, stat_col)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            embed = all_time_leaders_embed(stat_col, leaders_data)
            await interaction.followup.send(embed=embed)
        else:
            top_players = await _fetch_season_leaders(pool, league.id, stat_col)
            embed = leaders_embed(stat_col, top_players)
            view = LeaderboardView(league_id=league.id, current_category=stat_col)
            await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="player", description="View a player's profile and stats")
    @app_commands.describe(
        name_or_id="Player name or numeric ID",
        career="Show career stats across all seasons instead of current season",
    )
    async def player(
        self,
        interaction: discord.Interaction,
        name_or_id: str,
        career: bool = False,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        pool = await get_pool()

        try:
            p = await _resolve_player(pool, league.id, name_or_id)
        except DBAError as exc:
            await interaction.followup.send(exc.message, ephemeral=True)
            return

        team_name = "Free Agent"
        if p.team_id:
            row = await pool.fetchrow("SELECT city, name FROM teams WHERE id = $1", p.team_id)
            if row:
                team_name = f"{row['city']} {row['name']}"

        nba_profile = _load_nba_profile(f"{p.first_name} {p.last_name}", league.current_season)

        if career:
            career_data = await player_repo.get_career_stats(pool, p.id, league.id)
            embed = career_stats_embed(p, career_data, team_name, nba_profile=nba_profile)
        else:
            contract = await player_repo.get_active_contract(pool, p.id)
            season_stats = await _season_stats_for_player(pool, p.id)
            recent_games = await _recent_games_for_player(pool, p.id)
            embed = player_profile_embed(p, team_name, contract, season_stats, recent_games, nba_profile=nba_profile)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="all-time-records", description="Show notable all-time league records")
    async def all_time_records(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        records = await player_repo.get_league_all_time_records(pool, league.id)
        embed = all_time_records_embed(records)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="compare", description="Compare two players side-by-side")
    @app_commands.describe(player1="First player name or ID", player2="Second player name or ID")
    async def compare(
        self,
        interaction: discord.Interaction,
        player1: str,
        player2: str,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        pool = await get_pool()

        try:
            p_a = await _resolve_player(pool, league.id, player1)
            p_b = await _resolve_player(pool, league.id, player2)
        except DBAError as exc:
            await interaction.followup.send(exc.message, ephemeral=True)
            return

        stats_a = await _season_stats_for_player(pool, p_a.id)
        stats_b = await _season_stats_for_player(pool, p_b.id)

        embed = compare_embed(p_a, stats_a, p_b, stats_b)
        await interaction.followup.send(embed=embed, ephemeral=True)


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(StatsGroup())

    @app_commands.command(name="power-rankings", description="Show current power rankings for all teams")
    @app_commands.describe(
        mode=(
            "Ranking mode: record (default, power score), "
            "expected (projected wins), talent (roster OVR)"
        ),
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Record (power score)", value="record"),
        app_commands.Choice(name="Expected wins (posture)", value="expected"),
        app_commands.Choice(name="Talent (roster OVR)", value="talent"),
    ])
    async def power_rankings(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] = None,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        rank_mode = mode.value if mode else "record"
        pool = await get_pool()

        standings_rows = await pool.fetch(
            "SELECT * FROM standings_cache WHERE league_id = $1 AND season = $2",
            league.id,
            league.current_season,
        )
        if not standings_rows:
            await interaction.followup.send("No standings data yet.", ephemeral=True)
            return

        all_teams = await team_repo.get_all(pool, league.id)
        teams_by_id = {t.id: t for t in all_teams}

        # Default "record" mode with no posture enrichment — matches legacy output.
        if rank_mode == "record" and mode is None:
            ranked = compute_power_rankings([dict(r) for r in standings_rows], teams_by_id)
            embed = power_rankings_embed(ranked)
            await interaction.followup.send(embed=embed)
            return

        # Posture-aware path: bulk fetch posture for all teams (1 batch call).
        team_ids = [t.id for t in all_teams]
        try:
            intel_by_team = await team_intel.build_team_intel(
                pool, league, league.current_season,
                team_ids,
                include=("posture",),
            )
        except Exception as exc:
            log.error(f"power_rankings posture fetch failed: {exc}", exc_info=True)
            intel_by_team = {}

        # Fetch avg OVR per team for talent mode (1 query regardless of mode).
        ovr_rows = await pool.fetch(
            """
            SELECT l.team_id, AVG(p.overall)::float AS avg_ovr
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            WHERE l.league_id = $1
            GROUP BY l.team_id
            """,
            league.id,
        )
        avg_ovr_by_team: dict[int, float] = {
            r["team_id"]: r["avg_ovr"] for r in ovr_rows if r["avg_ovr"] is not None
        }

        ranked = compute_power_rankings_posture(
            [dict(r) for r in standings_rows],
            teams_by_id,
            intel_by_team,
            avg_ovr_by_team,
            rank_mode,
        )
        embed = power_rankings_posture_embed(ranked, rank_mode)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="h2h", description="Head-to-head record between two teams")
    @app_commands.describe(team1_code="First team code (e.g. LAL)", team2_code="Second team code (e.g. BOS)")
    async def h2h(
        self,
        interaction: discord.Interaction,
        team1_code: str,
        team2_code: str,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league found.", ephemeral=True)
            return

        pool = await get_pool()
        team_a = await team_repo.get_by_code(pool, league.id, team1_code)
        team_b = await team_repo.get_by_code(pool, league.id, team2_code)

        if not team_a:
            await interaction.followup.send(f"Team `{team1_code.upper()}` not found.", ephemeral=True)
            return
        if not team_b:
            await interaction.followup.send(f"Team `{team2_code.upper()}` not found.", ephemeral=True)
            return

        rows = await pool.fetch(
            """
            SELECT g.winner_team_id, g.home_score, g.away_score, g.scheduled_date,
                   g.home_team_id, g.away_team_id
            FROM games g
            WHERE g.league_id = $1
              AND g.status = 'simmed'
              AND (
                  (g.home_team_id = $2 AND g.away_team_id = $3)
                  OR
                  (g.home_team_id = $3 AND g.away_team_id = $2)
              )
            ORDER BY g.scheduled_date DESC
            """,
            league.id,
            team_a.id,
            team_b.id,
        )

        games = [dict(r) for r in rows]
        wins_a, wins_b = count_h2h_wins(games, team_a.id, team_b.id)

        team_code_by_id = {team_a.id: team_a.nba_team_code, team_b.id: team_b.nba_team_code}
        recent = []
        for g in games[:5]:
            recent.append({
                "scheduled_date": str(g["scheduled_date"]),
                "home_code": team_code_by_id.get(g["home_team_id"], "???"),
                "away_code": team_code_by_id.get(g["away_team_id"], "???"),
                "home_score": g["home_score"],
                "away_score": g["away_score"],
            })

        embed = h2h_embed(
            team_a=team_a.nba_team_code,
            team_b=team_b.nba_team_code,
            wins_a=wins_a,
            wins_b=wins_b,
            recent_games=recent,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCog(bot))
