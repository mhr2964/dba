"""Discord slash commands for human commissioner / manager role overrides.

Command tree:
  /coach role assign <player> <role>   — was: /coach assign-role
  /coach role show [team_member]       — was: /coach show-roles
  /coach role unlock <player>          — was: /coach unlock
  /coach directive set <player> …      — was: /directive set
  /coach directive view [team]         — was: /directive view
  /coach directive reset <player>      — was: /directive reset
  /coach philosophy show [code]        — thin wrapper over /team philosophy

Deprecation aliases (one-cycle window, removed after next rollover):
  /coach assign-role  → /coach role assign
  /coach show-roles   → /coach role show
  /coach unlock       → /coach role unlock

Auth pattern mirrors strategy_cog: manager owns their team; commissioner can
touch any team.
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.errors import DBAError, safe_defer
from core.logging import get_logger
from data.db import get_pool
from data.repositories import player_repo
from services import league_service, role_service, team_intel
from services.batch_sim_runner import invalidate_role_cache
from services.ride_along import emit_role_change, is_role_pause_enabled
from bot.embeds import intel_embeds

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Role choices — all 23 registry keys, sorted alphabetically.  Discord caps
# choices at 25; we have 23 so this fits with room to spare.
# ---------------------------------------------------------------------------

_ROLE_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name=key.replace("_", " ").title(), value=key)
    for key in sorted(role_service.ROLE_REGISTRY.keys())
]

# Valid directive values per category (mirrored from the old directive_cog)
_VALID_VALUES: dict[str, frozenset[str]] = {
    "shot_diet": frozenset({"auto", "force_3s", "attack_rim", "post_heavy", "midrange"}),
    "usage":     frozenset({"feature", "normal", "conserve"}),
    "defense":   frozenset({"lockdown", "standard", "off"}),
    "role":      frozenset({"creator", "scorer", "spot_up"}),
    "clutch":    frozenset({"hero", "normal", "hide"}),
}

_CATEGORY_COLUMN: dict[str, str] = {
    "shot_diet": "shot_diet",
    "usage":     "usage_mode",
    "defense":   "defense_mode",
    "role":      "role_mode",
    "clutch":    "clutch_mode",
}

_CATEGORY_LABELS: dict[str, str] = {
    "shot_diet":    "Shot Diet",
    "usage_mode":   "Usage",
    "defense_mode": "Defense",
    "role_mode":    "Role",
    "clutch_mode":  "Clutch",
}


# ---------------------------------------------------------------------------
# Deprecation warning helper
# ---------------------------------------------------------------------------

# Per-cog-instance set: track which users have already seen a deprecation
# notice this session so we don't spam them on every invocation.
_DEPRECATION_WARNED: set[int] = set()


async def _send_deprecation_warning(
    interaction: discord.Interaction,
    old: str,
    new: str,
) -> None:
    """Send a one-time ephemeral notice that a command path has moved."""
    uid = interaction.user.id
    if uid in _DEPRECATION_WARNED:
        return
    _DEPRECATION_WARNED.add(uid)
    try:
        await interaction.followup.send(
            f"**Heads up:** `{old}` has moved to `{new}`. "
            "The old path will be removed after the next season rollover.",
            ephemeral=True,
        )
    except Exception:
        pass  # best-effort; don't block the real response


# ---------------------------------------------------------------------------
# Internal helpers (mirror strategy_cog pattern)
# ---------------------------------------------------------------------------

async def _get_league_id(pool, guild_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT id FROM leagues WHERE discord_guild_id = $1 AND archived_at IS NULL",
        guild_id,
    )
    if not row:
        raise DBAError("No active league in this server.")
    return row["id"]


async def _get_season(pool, league_id: int) -> int:
    row = await pool.fetchrow("SELECT current_season FROM leagues WHERE id = $1", league_id)
    return row["current_season"] if row else 1


async def _resolve_caller_team(pool, league_id: int, user_id: int) -> Optional[dict]:
    """Return team row for the calling user, or None if not a manager."""
    row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id,
        user_id,
    )
    return dict(row) if row else None


async def _is_commissioner(pool, league_id: int, user_id: int) -> bool:
    val = await pool.fetchval(
        "SELECT commissioner_user_id FROM leagues WHERE id = $1", league_id
    )
    return val == user_id


async def _resolve_target_team(
    pool,
    league_id: int,
    user_id: int,
    target_member: Optional[discord.Member],
) -> tuple[dict, bool]:
    """Return (team_row, is_own_team).

    If target_member is given, look up that member's team.
    Otherwise fall back to the caller's team.
    Raises DBAError if no team can be found.
    """
    lookup_user = target_member.id if target_member else user_id
    row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id,
        lookup_user,
    )
    if not row:
        if target_member:
            raise DBAError(f"{target_member.mention} doesn't manage a team in this league.")
        raise DBAError("You don't manage a team in this league.")
    is_own = lookup_user == user_id
    return dict(row), is_own


async def _resolve_player_on_team(
    pool,
    league_id: int,
    team_id: int,
    player_name: str,
) -> dict:
    """Fuzzy-search player by name on a specific team.  Returns player row dict.

    Raises DBAError if none or multiple matches.
    """
    rows = await pool.fetch(
        """
        SELECT p.id, p.first_name, p.last_name, p.position, p.overall
        FROM players p
        JOIN lineups l ON l.player_id = p.id
        WHERE l.league_id = $1
          AND l.team_id   = $2
          AND unaccent(p.first_name || ' ' || p.last_name) ILIKE unaccent($3)
        ORDER BY p.overall DESC
        LIMIT 25
        """,
        league_id,
        team_id,
        f"%{player_name}%",
    )
    if not rows:
        raise DBAError(f"No player matching '{player_name}' found on that roster.")
    if len(rows) > 1:
        names = ", ".join(f"{r['first_name']} {r['last_name']}" for r in rows[:5])
        raise DBAError(f"Multiple matches for '{player_name}': {names}. Be more specific.")
    row = rows[0]
    return {
        "id": row["id"],
        "full_name": f"{row['first_name']} {row['last_name']}",
        "position": row["position"],
        "overall": row["overall"],
    }


async def _player_autocomplete_for_team(
    pool,
    league_id: int,
    team_id: int,
    current: str,
) -> list[app_commands.Choice[str]]:
    rows = await pool.fetch(
        """
        SELECT p.id, p.first_name, p.last_name, p.overall
        FROM players p
        JOIN lineups l ON l.player_id = p.id
        WHERE l.league_id = $1
          AND l.team_id   = $2
          AND unaccent(p.first_name || ' ' || p.last_name) ILIKE unaccent($3)
        ORDER BY p.overall DESC
        LIMIT 25
        """,
        league_id,
        team_id,
        f"%{current}%",
    )
    return [
        app_commands.Choice(
            name=f"{r['first_name']} {r['last_name']} (OVR {r['overall']})",
            value=f"{r['first_name']} {r['last_name']}",
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _roles_embed(team: dict, roles: list[dict], season: int) -> discord.Embed:
    """Render a role table embed for a team's current-season assignments."""
    team_name = f"{team.get('city', '')} {team.get('name', '')}".strip()
    embed = discord.Embed(
        title=f"{team_name} — Player Roles (Season {season})",
        color=discord.Color.purple(),
    )

    if not roles:
        embed.description = "No role assignments found for this team."
        return embed

    lines: list[str] = []
    for r in roles:
        lock_icon = "🔒" if r.get("locked") else "  "
        assigned_by = r.get("assigned_by") or "cpu"
        by_label = "human" if assigned_by.startswith("human:") else "cpu"
        share_pct = int(round((r.get("touch_share") or 0) * 100))
        role_display = (r.get("role") or "?").replace("_", " ")
        lines.append(
            f"{lock_icon} `{r.get('position', '?'):2}` **{r.get('name', '?')}**"
            f"  —  {role_display}  ({share_pct}%)  [{by_label}]"
        )

    chunk_size = 8
    chunks = [lines[i : i + chunk_size] for i in range(0, len(lines), chunk_size)]
    for idx, chunk in enumerate(chunks):
        embed.add_field(
            name="Assignments" if idx == 0 else "​",
            value="\n".join(chunk),
            inline=False,
        )

    embed.set_footer(text="🔒 = locked (CPU re-derive skips)  |  cpu = system-assigned")
    return embed


def _assign_confirm_embed(
    player_name: str,
    old_role: Optional[str],
    old_touch: Optional[float],
    new_role: str,
    new_touch: float,
    discord_username: str,
) -> discord.Embed:
    old_ts_str = f" ({int(round(old_touch * 100))}%)" if old_touch is not None else ""
    old_label = f"{(old_role or 'none').replace('_', ' ')}{old_ts_str}" if old_role else "none"
    new_label = f"{new_role.replace('_', ' ')} ({int(round(new_touch * 100))}%)"

    embed = discord.Embed(
        title="Role Override — Locked",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Player", value=player_name, inline=True)
    embed.add_field(name="Old Role", value=old_label, inline=True)
    embed.add_field(name="New Role", value=f"**{new_label}**  🔒", inline=True)
    embed.set_footer(text=f"Locked by {discord_username} — CPU re-derive will skip this player.")
    return embed


def _unlock_confirm_embed(
    player_name: str,
    new_role: Optional[str],
    new_touch: Optional[float],
    discord_username: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="Role Unlocked — CPU Re-derived",
        color=discord.Color.green(),
    )
    embed.add_field(name="Player", value=player_name, inline=True)
    if new_role:
        new_label = f"{new_role.replace('_', ' ')} ({int(round((new_touch or 0) * 100))}%)"
        embed.add_field(name="New CPU Role", value=new_label, inline=True)
    else:
        embed.add_field(name="Status", value="CPU will assign at next derive.", inline=True)
    embed.set_footer(text=f"Unlocked by {discord_username}")
    return embed


# ---------------------------------------------------------------------------
# Shared role-assign implementation (shared by new + legacy paths)
# ---------------------------------------------------------------------------

async def _do_role_assign(
    interaction: discord.Interaction,
    player: str,
    role: app_commands.Choice[str],
) -> None:
    """Core logic for /coach role assign."""
    await safe_defer(interaction)

    pool = await get_pool()
    league = await league_service.get_league(interaction.guild_id)
    if not league:
        await interaction.followup.send("No active league in this server.", ephemeral=True)
        return

    league_id = league.id
    is_commish = await _is_commissioner(pool, league_id, interaction.user.id)
    caller_team = await _resolve_caller_team(pool, league_id, interaction.user.id)

    if not caller_team and not is_commish:
        await interaction.followup.send(
            "You don't manage a team in this league.", ephemeral=True
        )
        return

    if caller_team:
        team = caller_team
    else:
        await interaction.followup.send(
            "Commissioner: use `/coach role assign` from a team manager's perspective. "
            "As commissioner you have no team; use a team manager's account or "
            "manually locate the player ID.",
            ephemeral=True,
        )
        return

    try:
        player_row = await _resolve_player_on_team(pool, league_id, team["id"], player)
    except DBAError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

    season = await _get_season(pool, league_id)
    new_role_key = role.value
    new_touch = role_service.ROLE_REGISTRY[new_role_key]["touch_share"]

    old_row = await pool.fetchrow(
        """
        SELECT role, touch_share
        FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3 AND player_id = $4
        """,
        league_id, team["id"], season, player_row["id"],
    )
    old_role = old_row["role"] if old_row else None
    old_touch = float(old_row["touch_share"]) if old_row and old_row["touch_share"] is not None else None

    rationale = f"Manual override by {interaction.user.name}"
    assigned_by = f"human:{interaction.user.id}"

    await pool.execute(
        """
        INSERT INTO player_roles
            (league_id, team_id, season, player_id, role, touch_share,
             rationale, assigned_by, locked, assigned_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE, NOW())
        ON CONFLICT (league_id, team_id, season, player_id) DO UPDATE
            SET role        = EXCLUDED.role,
                touch_share = EXCLUDED.touch_share,
                rationale   = EXCLUDED.rationale,
                assigned_by = EXCLUDED.assigned_by,
                locked      = TRUE,
                assigned_at = NOW()
        """,
        league_id, team["id"], season, player_row["id"],
        new_role_key, new_touch, rationale, assigned_by,
    )

    invalidate_role_cache(league_id, team["id"], season)

    if is_role_pause_enabled():
        emit_role_change(
            league_id=league_id,
            team_id=team["id"],
            team_code=team.get("nba_team_code", f"team#{team['id']}"),
            philosophy="human_override",
            deltas=[{
                "player_id": player_row["id"],
                "name": player_row["full_name"],
                "old_role": old_role or "none",
                "old_touch_share": old_touch or 0.0,
                "new_role": new_role_key,
                "new_touch_share": new_touch,
            }],
            reason=rationale,
            pause=False,
        )

    embed = _assign_confirm_embed(
        player_name=player_row["full_name"],
        old_role=old_role,
        old_touch=old_touch,
        new_role=new_role_key,
        new_touch=new_touch,
        discord_username=interaction.user.name,
    )
    await interaction.followup.send(embed=embed)


async def _do_role_show(
    interaction: discord.Interaction,
    team_member: Optional[discord.Member],
) -> None:
    """Core logic for /coach role show."""
    await safe_defer(interaction)

    pool = await get_pool()
    league = await league_service.get_league(interaction.guild_id)
    if not league:
        await interaction.followup.send("No active league in this server.", ephemeral=True)
        return

    league_id = league.id

    try:
        team, _is_own = await _resolve_target_team(pool, league_id, interaction.user.id, team_member)
    except DBAError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

    season = await _get_season(pool, league_id)

    rows = await pool.fetch(
        """
        SELECT
            pr.player_id,
            p.first_name || ' ' || p.last_name AS name,
            p.position,
            p.overall,
            pr.role,
            pr.touch_share,
            pr.locked,
            pr.assigned_by
        FROM player_roles pr
        JOIN players p ON p.id = pr.player_id
        WHERE pr.league_id = $1
          AND pr.team_id   = $2
          AND pr.season    = $3
        ORDER BY pr.touch_share DESC
        """,
        league_id, team["id"], season,
    )

    roles = [dict(r) for r in rows]
    embed = _roles_embed(team, roles, season)
    await interaction.followup.send(embed=embed)


async def _do_role_unlock(
    interaction: discord.Interaction,
    player: str,
) -> None:
    """Core logic for /coach role unlock."""
    await safe_defer(interaction)

    pool = await get_pool()
    league = await league_service.get_league(interaction.guild_id)
    if not league:
        await interaction.followup.send("No active league in this server.", ephemeral=True)
        return

    league_id = league.id
    is_commish = await _is_commissioner(pool, league_id, interaction.user.id)
    caller_team = await _resolve_caller_team(pool, league_id, interaction.user.id)

    if not caller_team and not is_commish:
        await interaction.followup.send("You don't manage a team in this league.", ephemeral=True)
        return

    if not caller_team:
        await interaction.followup.send(
            "Commissioner: unlock requires specifying a team. Run this as the team's manager account.",
            ephemeral=True,
        )
        return

    team = caller_team

    try:
        player_row = await _resolve_player_on_team(pool, league_id, team["id"], player)
    except DBAError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return

    season = await _get_season(pool, league_id)

    existing = await pool.fetchrow(
        """
        SELECT locked FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3 AND player_id = $4
        """,
        league_id, team["id"], season, player_row["id"],
    )
    if existing and not existing["locked"]:
        await interaction.followup.send(
            f"**{player_row['full_name']}** is already unlocked.", ephemeral=True
        )
        return

    await pool.execute(
        """
        UPDATE player_roles
           SET locked      = FALSE,
               assigned_by = 'cpu',
               assigned_at = NOW()
         WHERE league_id = $1 AND team_id = $2 AND season = $3 AND player_id = $4
        """,
        league_id, team["id"], season, player_row["id"],
    )

    await role_service.derive_and_persist_all_for_team(
        pool, league_id, team["id"], season, silent_emit=True
    )

    invalidate_role_cache(league_id, team["id"], season)

    new_row = await pool.fetchrow(
        """
        SELECT role, touch_share
        FROM player_roles
        WHERE league_id = $1 AND team_id = $2 AND season = $3 AND player_id = $4
        """,
        league_id, team["id"], season, player_row["id"],
    )
    new_role = new_row["role"] if new_row else None
    new_touch = float(new_row["touch_share"]) if new_row and new_row["touch_share"] is not None else None

    embed = _unlock_confirm_embed(
        player_name=player_row["full_name"],
        new_role=new_role,
        new_touch=new_touch,
        discord_username=interaction.user.name,
    )
    await interaction.followup.send(embed=embed)


async def _player_autocomplete_own_team(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocomplete helper: own team's roster (used by role assign + unlock)."""
    try:
        pool = await get_pool()
        league = await league_service.get_league(interaction.guild_id)
        if not league:
            return []
        is_commish = await _is_commissioner(pool, league.id, interaction.user.id)
        caller_team = await _resolve_caller_team(pool, league.id, interaction.user.id)
        if not caller_team and not is_commish:
            return []
        if caller_team:
            return await _player_autocomplete_for_team(pool, league.id, caller_team["id"], current)
        # Commissioner: show all rostered players
        rows = await pool.fetch(
            """
            SELECT p.id, p.first_name, p.last_name, p.overall
            FROM players p
            JOIN lineups l ON l.player_id = p.id
            WHERE l.league_id = $1
              AND unaccent(p.first_name || ' ' || p.last_name) ILIKE unaccent($2)
            ORDER BY p.overall DESC
            LIMIT 25
            """,
            league.id,
            f"%{current}%",
        )
        return [
            app_commands.Choice(
                name=f"{r['first_name']} {r['last_name']} (OVR {r['overall']})",
                value=f"{r['first_name']} {r['last_name']}",
            )
            for r in rows
        ]
    except Exception:
        return []


async def _require_manager_or_commissioner_directive(
    pool, league_id: int, user_id: int, player_team_id: int
) -> None:
    """Raise DBAError if the user is neither manager of player's team nor commissioner."""
    league_row = await pool.fetchrow(
        "SELECT commissioner_user_id FROM leagues WHERE id = $1", league_id
    )
    if league_row and user_id == league_row["commissioner_user_id"]:
        return
    team_row = await pool.fetchrow(
        "SELECT id FROM teams WHERE league_id = $1 AND manager_user_id = $2 AND id = $3",
        league_id, user_id, player_team_id,
    )
    if not team_row:
        raise DBAError("You can only set directives for players on your own team.")


async def _resolve_team_for_directive_view(
    pool, league_id: int, team_code: Optional[str], user_id: int
) -> dict:
    if team_code:
        row = await pool.fetchrow(
            "SELECT * FROM teams WHERE league_id = $1 AND UPPER(nba_team_code) = UPPER($2)",
            league_id, team_code,
        )
        if not row:
            raise DBAError(f"Team `{team_code.upper()}` not found.")
        return dict(row)
    row = await pool.fetchrow(
        "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
        league_id, user_id,
    )
    if not row:
        raise DBAError(
            "You don't manage a team. Provide a team code to view another team's directives."
        )
    return dict(row)


# ---------------------------------------------------------------------------
# /coach role subgroup
# ---------------------------------------------------------------------------

class RoleGroup(app_commands.Group, name="role", description="Player role overrides"):

    @app_commands.command(
        name="assign",
        description="Lock a player's CPU role (overrides CPU re-derive)",
    )
    @app_commands.describe(
        player="Player name (e.g. LeBron James)",
        role="Role to assign",
    )
    @app_commands.choices(role=_ROLE_CHOICES)
    async def assign(
        self,
        interaction: discord.Interaction,
        player: str,
        role: app_commands.Choice[str],
    ) -> None:
        await _do_role_assign(interaction, player, role)

    @assign.autocomplete("player")
    async def _assign_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _player_autocomplete_own_team(interaction, current)

    @app_commands.command(
        name="show",
        description="Display current role assignments for a team",
    )
    @app_commands.describe(team_member="Show roles for this member's team (defaults to your team)")
    async def show(
        self,
        interaction: discord.Interaction,
        team_member: Optional[discord.Member] = None,
    ) -> None:
        await _do_role_show(interaction, team_member)

    @app_commands.command(
        name="unlock",
        description="Remove a role lock — CPU will re-derive this player's role",
    )
    @app_commands.describe(player="Player name to unlock")
    async def unlock(
        self,
        interaction: discord.Interaction,
        player: str,
    ) -> None:
        await _do_role_unlock(interaction, player)

    @unlock.autocomplete("player")
    async def _unlock_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            pool = await get_pool()
            league = await league_service.get_league(interaction.guild_id)
            if not league:
                return []
            caller_team = await _resolve_caller_team(pool, league.id, interaction.user.id)
            if not caller_team:
                return []
            return await _player_autocomplete_for_team(pool, league.id, caller_team["id"], current)
        except Exception:
            return []


# ---------------------------------------------------------------------------
# /coach directive subgroup
# ---------------------------------------------------------------------------

class DirectiveSubGroup(app_commands.Group, name="directive", description="Player coaching directives"):

    @app_commands.command(name="set", description="Set a coaching directive for a player")
    @app_commands.describe(
        player="Player name (e.g. LeBron James)",
        category="Category: shot_diet / usage / defense / role / clutch",
        value="New value for that category",
    )
    async def set_directive(
        self,
        interaction: discord.Interaction,
        player: str,
        category: str,
        value: str,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        category = category.lower().strip()
        value = value.lower().strip()

        if category not in _VALID_VALUES:
            valid_cats = ", ".join(sorted(_VALID_VALUES))
            await interaction.followup.send(
                f"Unknown category `{category}`. Valid categories: {valid_cats}",
                ephemeral=True,
            )
            return

        if value not in _VALID_VALUES[category]:
            valid_vals = ", ".join(sorted(_VALID_VALUES[category]))
            await interaction.followup.send(
                f"Invalid value `{value}` for `{category}`. Valid values: {valid_vals}",
                ephemeral=True,
            )
            return

        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)

        matches = await player_repo.search_by_name(pool, league_id, player)
        if not matches:
            await interaction.followup.send(
                f"No player found matching '{player}'.", ephemeral=True
            )
            return
        if len(matches) > 1:
            names = ", ".join(f"{p.first_name} {p.last_name}" for p in matches[:5])
            await interaction.followup.send(
                f"Multiple players match '{player}': {names}. Be more specific.",
                ephemeral=True,
            )
            return

        found_player = matches[0]
        if found_player.team_id is None:
            await interaction.followup.send("That player is not on a team.", ephemeral=True)
            return

        try:
            await _require_manager_or_commissioner_directive(
                pool, league_id, interaction.user.id, found_player.team_id
            )
        except DBAError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        column = _CATEGORY_COLUMN[category]
        await pool.execute(
            f"""
            INSERT INTO player_directives (league_id, player_id, {column}, set_by, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (league_id, player_id) DO UPDATE
                SET {column} = EXCLUDED.{column},
                    set_by = EXCLUDED.set_by,
                    updated_at = EXCLUDED.updated_at
            """,
            league_id, found_player.id, value, interaction.user.id,
        )

        log.info(
            f"directive set: league={league_id} player={found_player.id} "
            f"{column}={value} by user={interaction.user.id}"
        )

        embed = discord.Embed(title="Directive Updated", color=discord.Color.green())
        embed.add_field(name="Player", value=found_player.full_name, inline=True)
        embed.add_field(name="Category", value=_CATEGORY_LABELS.get(column, category), inline=True)
        embed.add_field(name="Value", value=f"`{value}`", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="view", description="View all directives for your team")
    @app_commands.describe(team="Team code (e.g. LAL), defaults to your team")
    async def view_directives(
        self,
        interaction: discord.Interaction,
        team: Optional[str] = None,
    ) -> None:
        await safe_defer(interaction)

        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)
        try:
            team_row = await _resolve_team_for_directive_view(pool, league_id, team, interaction.user.id)
        except DBAError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        rows = await pool.fetch(
            """
            SELECT p.id, p.first_name, p.last_name, p.position, p.overall,
                   pd.shot_diet, pd.usage_mode, pd.defense_mode, pd.role_mode, pd.clutch_mode
            FROM lineups l
            JOIN players p ON p.id = l.player_id
            LEFT JOIN player_directives pd ON pd.league_id = $1 AND pd.player_id = p.id
            WHERE l.league_id = $1 AND l.team_id = $2
            ORDER BY l.slot ASC
            """,
            league_id, team_row["id"],
        )

        team_name = f"{team_row.get('city', '')} {team_row.get('name', '')}".strip()
        embed = discord.Embed(
            title=f"{team_name} — Player Directives",
            color=discord.Color.blue(),
        )

        if not rows:
            embed.description = "No players in lineup."
            await interaction.followup.send(embed=embed)
            return

        lines = []
        for r in rows:
            name = f"{r['first_name']} {r['last_name']}"
            pos = r["position"] or "?"
            shot = r["shot_diet"] or "auto"
            usage = r["usage_mode"] or "normal"
            defense = r["defense_mode"] or "standard"
            role = r["role_mode"] or "scorer"
            clutch = r["clutch_mode"] or "normal"
            lines.append(
                f"**{name}** ({pos}, OVR {r['overall']})\n"
                f"  Shot: `{shot}` | Usage: `{usage}` | Defense: `{defense}` "
                f"| Role: `{role}` | Clutch: `{clutch}`"
            )

        chunk: list[str] = []
        chunk_len = 0
        field_idx = 1
        for line in lines:
            if chunk_len + len(line) + 1 > 1020 and chunk:
                embed.add_field(
                    name=f"Players ({field_idx})",
                    value="\n".join(chunk),
                    inline=False,
                )
                chunk = []
                chunk_len = 0
                field_idx += 1
            chunk.append(line)
            chunk_len += len(line) + 1

        if chunk:
            label = "Players" if field_idx == 1 else f"Players ({field_idx})"
            embed.add_field(name=label, value="\n".join(chunk), inline=False)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="reset", description="Reset all directives for a player to auto")
    @app_commands.describe(player="Player name (e.g. LeBron James)")
    async def reset_directive(
        self,
        interaction: discord.Interaction,
        player: str,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        pool = await get_pool()
        league_id = await _get_league_id(pool, interaction.guild_id)

        matches = await player_repo.search_by_name(pool, league_id, player)
        if not matches:
            await interaction.followup.send(
                f"No player found matching '{player}'.", ephemeral=True
            )
            return
        if len(matches) > 1:
            names = ", ".join(f"{p.first_name} {p.last_name}" for p in matches[:5])
            await interaction.followup.send(
                f"Multiple players match '{player}': {names}. Be more specific.",
                ephemeral=True,
            )
            return

        found_player = matches[0]
        if found_player.team_id is None:
            await interaction.followup.send("That player is not on a team.", ephemeral=True)
            return

        try:
            await _require_manager_or_commissioner_directive(
                pool, league_id, interaction.user.id, found_player.team_id
            )
        except DBAError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await pool.execute(
            "DELETE FROM player_directives WHERE league_id = $1 AND player_id = $2",
            league_id, found_player.id,
        )

        log.info(
            f"directive reset: league={league_id} player={found_player.id} "
            f"by user={interaction.user.id}"
        )

        embed = discord.Embed(
            title="Directives Reset",
            description=(
                f"All directives for **{found_player.full_name}** have been cleared. "
                "The sim engine will use their natural tendencies."
            ),
            color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /coach philosophy subgroup
# ---------------------------------------------------------------------------

class PhilosophyGroup(app_commands.Group, name="philosophy", description="Team coaching philosophy"):

    @app_commands.command(
        name="show",
        description="View a team's coach philosophy and its effect on role assignments",
    )
    @app_commands.describe(code="Team code (e.g. LAL). Defaults to your team.")
    async def show(
        self,
        interaction: discord.Interaction,
        code: str | None = None,
    ) -> None:
        await safe_defer(interaction)

        league = await league_service.get_league(interaction.guild_id)
        if not league:
            await interaction.followup.send("No active league in this server.", ephemeral=True)
            return

        pool = await get_pool()

        # Resolve the team the same way TeamGroup.philosophy does.
        if code:
            row = await pool.fetchrow(
                "SELECT * FROM teams WHERE league_id = $1 AND UPPER(nba_team_code) = UPPER($2)",
                league.id, code,
            )
        else:
            row = await pool.fetchrow(
                "SELECT * FROM teams WHERE league_id = $1 AND manager_user_id = $2",
                league.id, interaction.user.id,
            )

        if row is None:
            if code:
                await interaction.followup.send(
                    f"Team `{code.upper()}` not found in this league.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "You don't manage a team. Use `/coach philosophy show LAL` to view any team.",
                    ephemeral=True,
                )
            return

        team = dict(row)
        team_id: int = team["id"]

        try:
            philosophy = await team_intel.get_team_philosophy(pool, team_id)
            recent_role_changes = await team_intel.get_recent_role_changes(
                pool, league.id, league.current_season, team_id
            )
        except Exception as exc:
            log.error(
                f"coach philosophy show failed for team {team_id}: {exc}", exc_info=True
            )
            await interaction.followup.send(
                "Failed to retrieve philosophy data.", ephemeral=True
            )
            return

        team_name = f"{team['city']} {team['name']}"
        embed = intel_embeds.philosophy_embed(
            team_name=team_name,
            philosophy=philosophy,
            recent_role_changes=recent_role_changes,
        )
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /coach command group — contains role, directive, philosophy subgroups
# plus flat deprecation aliases for the old /coach assign-role, show-roles, unlock
# ---------------------------------------------------------------------------

class CoachGroup(app_commands.Group, name="coach", description="CPU role management for team managers"):

    def __init__(self) -> None:
        super().__init__()
        self.add_command(RoleGroup())
        self.add_command(DirectiveSubGroup())
        self.add_command(PhilosophyGroup())

    # ------------------------------------------------------------------
    # Deprecation aliases — old flat commands forwarded to canonical paths
    # Remove these after the next season rollover.
    # ------------------------------------------------------------------

    @app_commands.command(
        name="assign-role",
        description="[MOVED] Use /coach role assign instead",
    )
    @app_commands.describe(
        player="Player name (e.g. LeBron James)",
        role="Role to assign",
    )
    @app_commands.choices(role=_ROLE_CHOICES)
    async def assign_role_legacy(
        self,
        interaction: discord.Interaction,
        player: str,
        role: app_commands.Choice[str],
    ) -> None:
        await safe_defer(interaction)
        await _send_deprecation_warning(
            interaction,
            old="/coach assign-role",
            new="/coach role assign",
        )
        await _do_role_assign(interaction, player, role)

    @assign_role_legacy.autocomplete("player")
    async def _assign_role_legacy_player_ac(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await _player_autocomplete_own_team(interaction, current)

    @app_commands.command(
        name="show-roles",
        description="[MOVED] Use /coach role show instead",
    )
    @app_commands.describe(team_member="Show roles for this member's team (defaults to your team)")
    async def show_roles_legacy(
        self,
        interaction: discord.Interaction,
        team_member: Optional[discord.Member] = None,
    ) -> None:
        await safe_defer(interaction)
        await _send_deprecation_warning(
            interaction,
            old="/coach show-roles",
            new="/coach role show",
        )
        await _do_role_show(interaction, team_member)

    @app_commands.command(
        name="unlock",
        description="[MOVED] Use /coach role unlock instead",
    )
    @app_commands.describe(player="Player name to unlock")
    async def unlock_legacy(
        self,
        interaction: discord.Interaction,
        player: str,
    ) -> None:
        await safe_defer(interaction)
        await _send_deprecation_warning(
            interaction,
            old="/coach unlock",
            new="/coach role unlock",
        )
        await _do_role_unlock(interaction, player)

    @unlock_legacy.autocomplete("player")
    async def _unlock_legacy_player_ac(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            pool = await get_pool()
            league = await league_service.get_league(interaction.guild_id)
            if not league:
                return []
            caller_team = await _resolve_caller_team(pool, league.id, interaction.user.id)
            if not caller_team:
                return []
            return await _player_autocomplete_for_team(pool, league.id, caller_team["id"], current)
        except Exception:
            return []


class CoachCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(CoachGroup())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CoachCog(bot))
