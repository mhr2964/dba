from __future__ import annotations

from typing import List, Optional

import discord

# ---------------------------------------------------------------------------
# Box score helpers
# ---------------------------------------------------------------------------

_TABLE_HEADER = "PLAYER             MIN  PTS  REB  AST  STL  BLK   TO   FG    3P   +/-"
_TABLE_SEP    = "─" * 69


def _fmt_pm(n: int) -> str:
    """Format plus/minus: +n, -n, or 0."""
    if n > 0:
        return f"+{n}"
    if n < 0:
        return str(n)
    return "0"


def _fmt_box_row(row: dict, *, is_starter: bool) -> str:
    """Return a single monospace table row for one player."""
    first = row.get("first_name", "")
    last = row.get("last_name", "")
    name_raw = f"{first[0]}. {last}" if first else last
    # 18 chars: position 0 is * or space, positions 1-18 are name
    prefix = "*" if is_starter else " "
    name_field = (prefix + name_raw)[:19].ljust(19)

    mins = int(round(row.get("minutes", 0)))
    pts  = row.get("points", 0)
    reb  = row.get("rebounds_off", 0) + row.get("rebounds_def", 0)
    ast  = row.get("assists", 0)
    stl  = row.get("steals", 0)
    blk  = row.get("blocks", 0)
    tov  = row.get("turnovers", 0)
    fgm  = row.get("fgm", 0)
    fga  = row.get("fga", 0)
    tpm  = row.get("tpm", 0)
    tpa  = row.get("tpa", 0)
    pm   = row.get("plus_minus", 0)

    fg_str = f"{fgm}/{fga}"
    tp_str = f"{tpm}/{tpa}"
    pm_str = _fmt_pm(pm)

    return (
        f"{name_field}"
        f"{mins:<5}{pts:<5}{reb:<5}{ast:<5}{stl:<5}{blk:<5}{tov:<5}"
        f"{fg_str:<6}{tp_str:<5}{pm_str}"
    )


def _totals_row(rows: list[dict], score_margin: int) -> str:
    """Build the TOTALS summary row."""
    total_min = int(round(sum(r.get("minutes", 0) for r in rows)))
    total_pts = sum(r.get("points", 0) for r in rows)
    total_reb = sum(r.get("rebounds_off", 0) + r.get("rebounds_def", 0) for r in rows)
    total_ast = sum(r.get("assists", 0) for r in rows)
    total_stl = sum(r.get("steals", 0) for r in rows)
    total_blk = sum(r.get("blocks", 0) for r in rows)
    total_tov = sum(r.get("turnovers", 0) for r in rows)
    total_fgm = sum(r.get("fgm", 0) for r in rows)
    total_fga = sum(r.get("fga", 0) for r in rows)
    total_tpm = sum(r.get("tpm", 0) for r in rows)
    total_tpa = sum(r.get("tpa", 0) for r in rows)

    name_field = "TOTALS".ljust(19)
    fg_str = f"{total_fgm}/{total_fga}"
    tp_str = f"{total_tpm}/{total_tpa}"
    pm_str = _fmt_pm(score_margin)

    return (
        f"{name_field}"
        f"{total_min:<5}{total_pts:<5}{total_reb:<5}{total_ast:<5}"
        f"{total_stl:<5}{total_blk:<5}{total_tov:<5}"
        f"{fg_str:<6}{tp_str:<5}{pm_str}"
    )


def _top_performers(away_box: list[dict], home_box: list[dict]) -> list[str]:
    """Return up to 3 bullet lines for top performers across both teams."""
    all_rows = away_box + home_box
    if not all_rows:
        return []

    top_pts = max(all_rows, key=lambda r: r.get("points", 0))
    top_reb = max(all_rows, key=lambda r: r.get("rebounds_off", 0) + r.get("rebounds_def", 0))
    top_ast = max(all_rows, key=lambda r: r.get("assists", 0))

    seen: set[int] = set()
    lines: list[str] = []
    for row, stat_label, stat_val in [
        (top_pts, "PTS", top_pts.get("points", 0)),
        (top_reb, "REB", top_reb.get("rebounds_off", 0) + top_reb.get("rebounds_def", 0)),
        (top_ast, "AST", top_ast.get("assists", 0)),
    ]:
        pid = row.get("player_id") or id(row)
        if pid in seen:
            continue
        seen.add(pid)
        first = row.get("first_name", "")
        last = row.get("last_name", "")
        name = f"{first[0]}. {last}" if first else last
        team_code = row.get("team_code", "")
        pts = row.get("points", 0)
        reb = row.get("rebounds_off", 0) + row.get("rebounds_def", 0)
        ast = row.get("assists", 0)
        fgm = row.get("fgm", 0)
        fga = row.get("fga", 0)
        tpm = row.get("tpm", 0)
        tpa = row.get("tpa", 0)
        lines.append(
            f"**{name}** ({team_code}) — {pts} PTS · {fgm}/{fga} FG · {tpm}/{tpa} 3P · {ast} AST · {reb} REB"
        )

    return lines


def box_score_summary_embed(
    game_row: dict,
    away_box: list[dict],
    home_box: list[dict],
) -> discord.Embed:
    """Final-score summary with top performers and team totals."""
    away_code = game_row.get("away_code", "???")
    home_code = game_row.get("home_code", "???")
    away_score = game_row.get("away_score", 0)
    home_score = game_row.get("home_score", 0)
    game_index = game_row.get("game_index", "?")
    scheduled_date = game_row.get("scheduled_date", "")

    title = f"{away_code} @ {home_code} — FINAL"
    score_line = f"**{away_code} {away_score}  –  {home_score} {home_code}**"

    embed = discord.Embed(
        title=title,
        description=score_line,
        color=discord.Color.blurple(),
    )

    performers = _top_performers(away_box, home_box)
    if performers:
        embed.add_field(
            name="Top Performers",
            value="\n".join(performers),
            inline=False,
        )

    # Team totals inline
    def _team_totals(rows: list[dict]) -> str:
        pts = sum(r.get("points", 0) for r in rows)
        reb = sum(r.get("rebounds_off", 0) + r.get("rebounds_def", 0) for r in rows)
        ast = sum(r.get("assists", 0) for r in rows)
        fgm = sum(r.get("fgm", 0) for r in rows)
        fga = sum(r.get("fga", 0) for r in rows)
        tpm = sum(r.get("tpm", 0) for r in rows)
        tpa = sum(r.get("tpa", 0) for r in rows)
        fg_pct = f"{fgm/fga*100:.1f}%" if fga > 0 else "—"
        tp_pct = f"{tpm/tpa*100:.1f}%" if tpa > 0 else "—"
        return (
            f"PTS: {pts} | REB: {reb} | AST: {ast}\n"
            f"FG: {fgm}/{fga} ({fg_pct}) | 3P: {tpm}/{tpa} ({tp_pct})"
        )

    embed.add_field(name=f"{away_code} Totals", value=_team_totals(away_box), inline=True)
    embed.add_field(name=f"{home_code} Totals", value=_team_totals(home_box), inline=True)

    # Quarters table — only rendered when quarter data is present.
    if game_row.get("q1_home") is not None:
        q_headers = ["Q1", "Q2", "Q3", "Q4"]
        home_qs = [
            game_row.get("q1_home", 0), game_row.get("q2_home", 0),
            game_row.get("q3_home", 0), game_row.get("q4_home", 0),
        ]
        away_qs = [
            game_row.get("q1_away", 0), game_row.get("q2_away", 0),
            game_row.get("q3_away", 0), game_row.get("q4_away", 0),
        ]
        ot_home = game_row.get("ot_home")
        ot_away = game_row.get("ot_away")
        if ot_home:
            q_headers.append("OT")
            home_qs.append(ot_home)
            away_qs.append(ot_away or 0)
        q_headers.append("F")
        home_qs.append(home_score)
        away_qs.append(away_score)

        col_w = 4
        team_w = 5
        header_row = " " * team_w + "".join(h.rjust(col_w) for h in q_headers)
        home_row = away_code.ljust(team_w) + "".join(str(v).rjust(col_w) for v in away_qs)
        away_row = home_code.ljust(team_w) + "".join(str(v).rjust(col_w) for v in home_qs)
        quarters_block = f"```\n{header_row}\n{home_row}\n{away_row}\n```"
        embed.add_field(name="Quarters", value=quarters_block, inline=False)

    embed.set_footer(text=f"Game #{game_index} | {scheduled_date} | Use menu below for full box scores")
    return embed


def box_score_team_embed(
    team_code: str,
    team_name: str,
    team_box_rows: list[dict],
    game_row: dict,
) -> discord.Embed:
    """Full per-player box score for one team as a monospace code block."""
    game_index = game_row.get("game_index", "?")
    away_code  = game_row.get("away_code", "???")
    home_code  = game_row.get("home_code", "???")
    away_score = game_row.get("away_score", 0)
    home_score = game_row.get("home_score", 0)

    is_away = team_code == away_code
    team_score    = away_score if is_away else home_score
    opp_score     = home_score if is_away else away_score
    score_margin  = team_score - opp_score

    lines = [_TABLE_HEADER, _TABLE_SEP]
    for row in team_box_rows:
        is_starter = bool(row.get("started", False))
        lines.append(_fmt_box_row(row, is_starter=is_starter))

    lines.append(_TABLE_SEP)
    lines.append(_totals_row(team_box_rows, score_margin))

    table = "```\n" + "\n".join(lines) + "\n```"

    embed = discord.Embed(
        title=f"{team_name} Box Score",
        description=table,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Game #{game_index} | * = starter")
    return embed


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
    # Build date range from actual game data; title (e.g. "Games 1-10") becomes a subtitle field.
    dates = [r["game"].get("scheduled_date") for r in games_results if r["game"].get("scheduled_date")]
    if dates:
        lo, hi = min(dates), max(dates)
        if lo == hi:
            date_str = f"{lo.strftime('%b')} {lo.day}"
        else:
            date_str = f"{lo.strftime('%b')} {lo.day} – {hi.strftime('%b')} {hi.day}"
        embed_title = date_str
    else:
        embed_title = title  # fallback when no date data

    embed = discord.Embed(title=embed_title, color=discord.Color.blurple())
    embed.add_field(name="Games", value=title, inline=True)

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


def batch_recap_with_standings(
    games_results: List[dict],
    standings_rows: List[dict],
) -> discord.Embed:
    """
    Combined batch recap + top-5 per conference standings snapshot.
    Builds the same score list as batch_recap, then appends East/West top-5 fields.
    """
    # --- Date range title (same logic as batch_recap) ---
    dates = [r["game"].get("scheduled_date") for r in games_results if r["game"].get("scheduled_date")]
    if dates:
        lo, hi = min(dates), max(dates)
        if lo == hi:
            embed_title = f"{lo.strftime('%b')} {lo.day}"
        else:
            embed_title = f"{lo.strftime('%b')} {lo.day} – {hi.strftime('%b')} {hi.day}"
    else:
        first_idx = games_results[0]["game"].get("game_index", "?") if games_results else "?"
        last_idx = games_results[-1]["game"].get("game_index", "?") if games_results else "?"
        embed_title = f"Games {first_idx}–{last_idx}"

    embed = discord.Embed(title=embed_title, color=discord.Color.blurple())

    # Game index subtitle
    if games_results:
        first_idx = games_results[0]["game"].get("game_index", "?")
        last_idx = games_results[-1]["game"].get("game_index", "?")
        embed.add_field(name="Games", value=f"Games {first_idx}–{last_idx}", inline=True)

    # Score lines
    lines = []
    for r in games_results:
        home_team = r["home_team"]
        away_team = r["away_team"]
        result = r["result"]
        home_name = home_team.nba_team_code if home_team else "???"
        away_name = away_team.nba_team_code if away_team else "???"
        lines.append(
            f"`{away_name}` @ `{home_name}`:  **{result['away_score']}–{result['home_score']}**"
        )
    embed.description = "\n".join(lines) if lines else "No games."

    # Separator
    embed.add_field(name="​", value="​", inline=False)

    # Standings top-5 per conference
    def _top5_str(rows: List[dict]) -> str:
        def _smoothed_win_pct(r: dict) -> float:
            """Laplace-smoothed win% for sorting: (wins+1)/(wins+losses+2).
            Prevents a 2-0 team from outranking an 11-1 team — small samples
            are pulled toward .500, larger samples dominate correctly."""
            w = r.get("wins") or 0
            l = r.get("losses") or 0
            return (w + 1) / (w + l + 2)

        def _games_played(r: dict) -> int:
            return (r.get("wins") or 0) + (r.get("losses") or 0)

        sorted_rows = sorted(rows, key=lambda r: (-_smoothed_win_pct(r), -_games_played(r)))[:5]
        parts = []
        for i, row in enumerate(sorted_rows, start=1):
            code = row.get("nba_team_code", str(row.get("team_id", "?")))
            w = row.get("wins", 0)
            l = row.get("losses", 0)
            parts.append(f"{i}. {code} {w}-{l}")
        return "\n".join(parts) if parts else "No data"

    east_rows = [r for r in standings_rows if r.get("conference") == "East"]
    west_rows = [r for r in standings_rows if r.get("conference") == "West"]

    embed.add_field(name="East Top 5", value=_top5_str(east_rows), inline=True)
    embed.add_field(name="West Top 5", value=_top5_str(west_rows), inline=True)

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

    def _conf_smoothed_win_pct(r: dict) -> float:
        """Laplace-smoothed win% for sorting: (wins+1)/(wins+losses+2).
        Determines conference seed order consistently with standings display."""
        w = r.get("wins") or 0
        l = r.get("losses") or 0
        return (w + 1) / (w + l + 2)

    east = sorted(
        [r for r in standings_rows if r["conference"] == "East"],
        key=lambda r: -_conf_smoothed_win_pct(r),
    )
    west = sorted(
        [r for r in standings_rows if r["conference"] == "West"],
        key=lambda r: -_conf_smoothed_win_pct(r),
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
        f"Game #{m.get('game_index', '?')} — {m.get('scheduled_date', '')}"
        for m in matchup_list
    ]
    total = len(lines)
    # Discord field value limit is 1024 chars — show first N lines that fit.
    value = ""
    for line in lines:
        candidate = (value + line + "\n")
        if len(candidate) > 980:
            value += f"… and {total - value.count(chr(10))} more"
            break
        value += line + "\n"
    embed.add_field(name=f"Affected Games ({total})", value=value.strip() or "None", inline=False)
    return embed


def sim_recap_embed(storylines: list[str]) -> Optional[discord.Embed]:
    """One embed field per storyline (newspaper style), capped at 5. Returns None if empty."""
    if not storylines:
        return None
    embed = discord.Embed(title="Around the League", color=discord.Color.blurple())
    for storyline in storylines[:5]:
        embed.add_field(name="​", value=storyline, inline=False)
    embed.set_footer(text="Daily wrap-up")
    return embed


def season_record_embed(announcement: str) -> discord.Embed:
    """Styled embed for a new season record announcement."""
    embed = discord.Embed(
        title="📊 New Season Record",
        description=announcement,
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Season records")
    return embed


def regular_season_complete_embed() -> discord.Embed:
    """End-of-regular-season announcement embed."""
    embed = discord.Embed(
        title="🏁 Regular Season Complete",
        color=discord.Color.dark_blue(),
    )
    embed.add_field(
        name="What's Next",
        value="Commissioner: use `/playoffs seed` to begin the postseason.",
        inline=False,
    )
    embed.set_footer(text="End of regular season")
    return embed


def trade_deadline_open_embed() -> discord.Embed:
    """Announcement embed when the trade window opens."""
    embed = discord.Embed(
        title="🚨 Trade Deadline Open",
        description="The trade window is open. Managers may now propose and execute trades.",
        color=discord.Color.red(),
    )
    embed.add_field(
        name="To resume simming",
        value="Commissioner: `/league advance REGULAR_SEASON_POSTDEADLINE`",
        inline=False,
    )
    embed.set_footer(text="Trade deadline window")
    return embed


def standings_snapshot_embed(standings_rows: List[dict], game_index: int) -> discord.Embed:
    """Standings-only embed for the #standings channel — full conference tables, sorted by win%."""
    def _win_pct(r: dict) -> float:
        """Raw win% for display only."""
        games = r["wins"] + r["losses"]
        return r["wins"] / games if games > 0 else 0.0

    def _smoothed_win_pct(r: dict) -> float:
        """Laplace-smoothed win% for sorting: (wins+1)/(wins+losses+2).
        Prevents a 2-0 team from outranking an 11-1 team in sort order."""
        w = r.get("wins") or 0
        l = r.get("losses") or 0
        return (w + 1) / (w + l + 2)

    def _games_played(r: dict) -> int:
        return (r.get("wins") or 0) + (r.get("losses") or 0)

    east = sorted(
        [r for r in standings_rows if r.get("conference") == "East"],
        key=lambda r: (-_smoothed_win_pct(r), -_games_played(r)),
    )
    west = sorted(
        [r for r in standings_rows if r.get("conference") == "West"],
        key=lambda r: (-_smoothed_win_pct(r), -_games_played(r)),
    )

    def _conf_lines(rows: List[dict]) -> str:
        lines = []
        for i, r in enumerate(rows, start=1):
            code = r.get("nba_team_code", str(r.get("team_id", "?")))
            w = r.get("wins", 0)
            l = r.get("losses", 0)
            pct = f"{_win_pct(r):.3f}".lstrip("0") or ".000"
            lines.append(f"`{i:2}.` **{code}**  {w}–{l}  `{pct}`")
        return "\n".join(lines) or "No data"

    embed = discord.Embed(
        title=f"League Standings — After Game {game_index}",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Eastern Conference", value=_conf_lines(east), inline=True)
    embed.add_field(name="Western Conference", value=_conf_lines(west), inline=True)
    return embed


def user_matchup_sim_failed_embed() -> discord.Embed:
    """Error embed when the scheduled user matchup fails to sim."""
    embed = discord.Embed(
        title="⚠️ User Matchup Sim Failed",
        description="Failed to sim the scheduled user matchup. Please contact the commissioner.",
        color=discord.Color.red(),
    )
    embed.set_footer(text="Sim error")
    return embed
