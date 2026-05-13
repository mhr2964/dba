from __future__ import annotations

import discord

from bot.embeds.stats_embeds import leaders_embed
from core.logging import get_logger
from data.db import get_pool
from data.repositories import player_repo

log = get_logger(__name__)

_CATEGORIES: list[tuple[str, str]] = [
    ("ppg", "Points"),
    ("rpg", "Rebounds"),
    ("apg", "Assists"),
    ("spg", "Steals"),
    ("bpg", "Blocks"),
]

# Map from display label to DB column used in get_leaders stat allowlist.
# get_leaders sorts by player attribute columns (overall, speed, …) — for
# season averages we query game_box_scores directly in the select handler.
_LABEL_TO_COL: dict[str, str] = {label: col for col, label in _CATEGORIES}


class LeaderboardView(discord.ui.View):
    """Select menu to switch between stat categories. Edits the original message."""

    def __init__(self, league_id: int, current_category: str) -> None:
        super().__init__(timeout=120)
        self.league_id = league_id
        self.current_category = current_category

        options = [
            discord.SelectOption(
                label=label,
                value=col,
                default=(col == current_category),
            )
            for col, label in _CATEGORIES
        ]
        self.select_menu.options = options

    @discord.ui.select(
        placeholder="Switch stat category...",
        options=[discord.SelectOption(label=label, value=col) for col, label in _CATEGORIES],
    )
    async def select_menu(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        chosen_col = select.values[0]
        await interaction.response.defer()

        pool = await get_pool()
        leaders = await _fetch_season_leaders(pool, self.league_id, chosen_col)

        embed = leaders_embed(chosen_col, leaders)
        self.current_category = chosen_col
        for opt in self.select_menu.options:
            opt.default = opt.value == chosen_col

        await interaction.message.edit(embed=embed, view=self)


async def _fetch_season_leaders(
    pool,
    league_id: int,
    stat_col: str,
    limit: int = 10,
) -> list[tuple[object, float]]:
    """
    Aggregate season averages from game_box_scores for the given stat column.
    stat_col must be one of: ppg, rpg, apg, spg, bpg.
    rebounds_off + rebounds_def are summed because the table has no single
    rebounds column.
    """
    _BOX_ALLOWLIST = frozenset({"ppg", "rpg", "apg", "spg", "bpg"})
    if stat_col not in _BOX_ALLOWLIST:
        raise ValueError(f"Invalid leaderboard stat: {stat_col!r}")

    # Build the AVG expression — rebounds span two columns in game_box_scores.
    avg_exprs = {
        "ppg": "AVG(b.points)",
        "rpg": "AVG(b.rebounds_off + b.rebounds_def)",
        "apg": "AVG(b.assists)",
        "spg": "AVG(b.steals)",
        "bpg": "AVG(b.blocks)",
    }
    avg_expr = avg_exprs[stat_col]

    rows = await pool.fetch(
        f"""
        SELECT p.*, {avg_expr} AS _avg
        FROM game_box_scores b
        JOIN players p ON p.id = b.player_id
        WHERE p.league_id = $1
        GROUP BY p.id
        ORDER BY _avg DESC
        LIMIT $2
        """,
        league_id,
        limit,
    )

    result: list[tuple[object, float]] = []
    for r in rows:
        player = player_repo._player_from_record(r)
        result.append((player, float(r["_avg"])))
    return result
