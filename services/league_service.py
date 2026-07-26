from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Optional

import asyncpg
import discord

from core.errors import DBAError
from core.logging import get_logger
from data.db import get_pool
from data.repositories import league_repo, team_repo, trade_repo
from services.role_service import COACH_PHILOSOPHIES

log = get_logger(__name__)

CHANNEL_ROLES = ["league-news", "analysis", "transactions", "injuries", "box-scores", "standings", "trade-block", "records"]


async def _randomize_philosophies(pool_or_conn, league_id: int, rng: Optional[random.Random] = None) -> None:
    """Assign a random coach_philosophy to every team in the league.

    Called once at league creation so all 30 CPU teams start with differentiated
    philosophies rather than the default 'tendency_respecter'.

    When DBA_RIDE_ALONG=1 and role pauses are on, each assignment is surfaced
    interactively.  Typing 'r' re-rolls that team's philosophy until accepted.

    rng: pass a seeded random.Random for deterministic tests; defaults to the
         module-level random (non-deterministic).
    """
    from services.ride_along import is_role_pause_enabled, emit_philosophy_assigned

    team_rows = await pool_or_conn.fetch(
        "SELECT id, nba_team_code FROM teams WHERE league_id = $1 ORDER BY id", league_id
    )
    rand = rng or random
    for row in team_rows:
        team_id = row["id"]
        team_code = row["nba_team_code"]

        philosophy = rand.choice(COACH_PHILOSOPHIES)

        if is_role_pause_enabled():
            # Reroll loop: repeat until user accepts.
            while True:
                action = emit_philosophy_assigned(
                    league_id=league_id,
                    team_id=team_id,
                    team_code=team_code,
                    philosophy=philosophy,
                )
                if action == "reroll":
                    philosophy = rand.choice(COACH_PHILOSOPHIES)
                    print(f"   [re-rolled {team_code} → {philosophy}]")
                    continue
                break

        await pool_or_conn.execute(
            "UPDATE teams SET coach_philosophy = $1 WHERE id = $2",
            philosophy,
            team_id,
        )
    log.info(
        "league_service: randomized coach_philosophy for %d teams in league %d",
        len(team_rows),
        league_id,
    )

_SEEDS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "seeds", "nba_teams.json")

# Per-guild lock for /league create. The DB unique constraint stops cross-bot
# races, but within a single bot two concurrent interactions can still both
# pass the get_by_guild pre-check and reach guild.create_category — observed
# 2026-05-18 with two 🏀 DBA categories created 8 ms apart. This lock
# serializes create() calls for the same guild inside the bot process so only
# one can be in flight at a time. Different guilds are independent.
_create_locks: dict[int, asyncio.Lock] = {}
_create_locks_guard = asyncio.Lock()


async def _acquire_create_lock(guild_id: int) -> asyncio.Lock:
    async with _create_locks_guard:
        lock = _create_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            _create_locks[guild_id] = lock
        return lock


def _load_nba_teams() -> list[dict]:
    path = os.path.normpath(_SEEDS_PATH)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def create(
    guild: discord.Guild,
    commissioner: discord.Member,
    name: str,
    season_year: int,
) -> league_repo.League:
    lock = await _acquire_create_lock(guild.id)
    async with lock:
        return await _create_locked(guild, commissioner, name, season_year)


async def create_headless(
    name: str,
    season_year: int,
    commissioner_id: int,
    guild_id: int = 0,
) -> league_repo.League:
    """Create a league with all Discord side effects skipped.

    Intended for headless test harnesses only — no channels, no roles,
    no onboarding guide.  The league row, 30 teams, and draft picks are
    created exactly as the normal path does.  commissioner_id must be a
    valid snowflake (can be any int in headless context).

    Uses guild_id=0 by default; pass a unique value if running multiple
    headless leagues concurrently.
    """
    lock = await _acquire_create_lock(guild_id)
    async with lock:
        pool = await get_pool()

        existing = await pool.fetchrow(
            "SELECT id, name FROM leagues WHERE discord_guild_id = $1", guild_id
        )
        if existing:
            raise DBAError(
                f"Headless league already exists for guild_id={guild_id}: {existing['name']!r}"
            )

        try:
            league = await league_repo.create(
                pool,
                guild_id=guild_id,
                name=name,
                season_year=season_year,
                commissioner_id=commissioner_id,
            )
        except asyncpg.UniqueViolationError:
            raise DBAError(
                "Headless league creation race — another call already created this league."
            )

        # Seed all 30 teams (identical to the Discord path).
        nba_teams = _load_nba_teams()
        for i, team_data in enumerate(nba_teams):
            await team_repo.create(pool, league.id, team_data)
            log.info(f"Headless league {league.id}: seeded team {i+1}/30 ({team_data['code']})")

        team_count = await pool.fetchval(
            "SELECT COUNT(*) FROM teams WHERE league_id = $1", league.id
        )
        if team_count != 30:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
            raise DBAError(
                f"Headless league setup incomplete: only {team_count}/30 teams created."
            )

        await trade_repo.seed_picks_for_league(pool, league.id, season_year)

        # Roster import must come before archetype backfill and philosophy
        # randomisation so those steps operate on the actual seeded players.
        try:
            from services import roster_seed_service
            await roster_seed_service.apply_reseed(pool, league.id, league.current_season, dry_run=False)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning(f"Headless roster reseed failed: {exc}")

        # Archetype backfill: populate defensive_archetype immediately after
        # roster import and before plan derivation.  Plans derived with NULL
        # archetypes produce wrong core/flex/surplus buckets (defensive
        # specialists land in surplus).  Failure is non-fatal — plans will
        # derive without archetype data and can be corrected later.
        try:
            from scripts.backfill_overall_from_defense import backfill_league_archetypes
            updated = await backfill_league_archetypes(
                pool, league_id=league.id, season=league.current_season
            )
            log.info(
                "league_service: archetype backfill updated %d players for league %d",
                updated, league.id,
            )
        except Exception as exc:
            log.warning(
                "league_service: archetype backfill failed for league %d — "
                "plans will derive without archetype data: %s",
                league.id, exc,
            )

        await _randomize_philosophies(pool, league.id)

        log.info(f"Headless league '{name}' created (id={league.id}, guild_id={guild_id})")
        return league


async def _create_locked(
    guild: discord.Guild,
    commissioner: discord.Member,
    name: str,
    season_year: int,
) -> league_repo.League:
    pool = await get_pool()

    existing = await league_repo.get_by_guild(pool, guild.id)
    if existing:
        raise DBAError(f"This server already has an active league: **{existing.name}**.")

    # DB row FIRST. The discord_guild_id unique constraint atomically blocks
    # concurrent /league create races at the cross-bot layer. The per-guild
    # asyncio.Lock above serializes within this bot process. Together they
    # guarantee no orphan Discord category can be created by a race-loser.
    try:
        league = await league_repo.create(
            pool,
            guild_id=guild.id,
            name=name,
            season_year=season_year,
            commissioner_id=commissioner.id,
        )
    except asyncpg.UniqueViolationError:
        raise DBAError(
            "This server already has an active league — another /league create just won the race. "
            "Run /league info to confirm."
        )

    category: Optional[discord.CategoryChannel] = None
    commissioner_role: Optional[discord.Role] = None
    channel_ids: dict[str, int] = {}

    # Any failure past this point must roll back BOTH the league row
    # (CASCADE clears all related DB state) AND any partial Discord state.
    category_name = f"🏀 {name}"
    try:
        log.info(f"League {league.id}: calling guild.create_category({category_name!r})")
        category = await guild.create_category(category_name)
        log.info(f"League {league.id}: created Discord category id={category.id}")

        # Pre-emptive sweep: Discord's API sometimes processes our category
        # create twice when discord.py retries on a transient. If a duplicate
        # of our category name exists already (other than the one we just
        # tracked), delete it NOW — while it's still empty — so we don't have
        # to delete its child channels later and the user never sees the
        # doubled state. Final sweep below catches any duplicates that show
        # up after our child-channel creates.
        try:
            existing = await guild.fetch_channels()
            early_dupes = [
                c for c in existing
                if isinstance(c, discord.CategoryChannel)
                and c.name == category_name
                and c.id != category.id
            ]
            for dup in early_dupes:
                log.warning(
                    f"League {league.id}: pre-emptive sweep found duplicate "
                    f"category id={dup.id} (kept id={category.id}) — deleting now"
                )
                try:
                    await dup.delete(reason="DBA pre-emptive duplicate sweep")
                except Exception as exc:
                    log.warning(f"  pre-emptive delete failed for {dup.id}: {exc}")
        except Exception as exc:
            log.warning(f"League {league.id}: pre-emptive sweep failed: {exc}")

        for role_name in CHANNEL_ROLES:
            ch = await category.create_text_channel(role_name)
            channel_ids[role_name] = ch.id

        # Combined duplicate sweep: fetch channels ONCE and clean up both
        # duplicate categories (phantom siblings of our tracked category)
        # and duplicate channels (same-named siblings within our category).
        # Running this immediately after channel creation — before any
        # team-seeding / archetype / philosophy work — means the user
        # sees the duplicates flicker for at most ~200ms instead of
        # several seconds.
        try:
            fresh_channels = await guild.fetch_channels()
            # Kill phantom duplicate categories (and their children) FIRST,
            # so the per-channel sweep below doesn't have to look at them.
            dup_cats = [
                c for c in fresh_channels
                if isinstance(c, discord.CategoryChannel)
                and c.name == category_name
                and c.id != category.id
            ]
            for orphan_cat in dup_cats:
                log.warning(
                    f"League {league.id}: post-create sweep killing duplicate "
                    f"category id={orphan_cat.id} (kept id={category.id})"
                )
                for ch in [
                    c for c in fresh_channels
                    if getattr(c, "category_id", None) == orphan_cat.id
                ]:
                    try:
                        await ch.delete(reason="DBA orphan-category sweep")
                    except Exception as exc:
                        log.warning(f"  orphan child delete failed for {ch.id}: {exc}")
                try:
                    await orphan_cat.delete(reason="DBA orphan-category sweep")
                except Exception as exc:
                    log.warning(f"  orphan category delete failed for {orphan_cat.id}: {exc}")

            # Per-channel dedupe WITHIN our category: same-name duplicates
            # from retried create_text_channel calls.
            kept_ids = set(channel_ids.values())
            in_category = [
                c for c in fresh_channels
                if isinstance(c, discord.TextChannel)
                and getattr(c, "category_id", None) == category.id
            ]
            for role_name in CHANNEL_ROLES:
                for dup in [c for c in in_category if c.name == role_name]:
                    if dup.id in kept_ids:
                        continue
                    log.warning(
                        f"League {league.id}: post-create sweep killing duplicate "
                        f"#{role_name} id={dup.id} (kept id={channel_ids[role_name]})"
                    )
                    try:
                        await dup.delete(reason="DBA per-channel duplicate sweep")
                    except Exception as exc:
                        log.warning(f"  per-channel delete failed for {dup.id}: {exc}")
        except Exception as exc:
            log.warning(f"League {league.id}: post-create sweep failed: {exc}")

        commissioner_role = await guild.create_role(
            name="DBA Commissioner",
            color=discord.Color.gold(),
            hoist=True,
            reason=f"DBA league '{name}' created",
        )
        await commissioner.add_roles(commissioner_role, reason="Assigned as DBA Commissioner")

        for role_name, channel_id in channel_ids.items():
            await league_repo.add_channel(pool, league.id, role_name, channel_id)
        await league_repo.add_role(pool, league.id, "commissioner", None, commissioner_role.id)

        # Team roles are created lazily on /team assign to avoid the Discord
        # per-guild rate limit that fires when creating 30 roles at once.
        nba_teams = _load_nba_teams()
        for i, team_data in enumerate(nba_teams):
            await team_repo.create(pool, league.id, team_data)
            log.info(f"League {league.id}: seeded team {i+1}/30 ({team_data['code']})")

        team_count = await pool.fetchval(
            "SELECT COUNT(*) FROM teams WHERE league_id = $1", league.id
        )
        if team_count != 30:
            raise DBAError(
                f"League setup incomplete: only {team_count}/30 teams were created. "
                "The league has been removed — please try `/league create` again."
            )

    except Exception as exc:
        log.error(f"League creation failed for guild {guild.id}: {exc}", exc_info=True)
        try:
            await pool.execute("DELETE FROM leagues WHERE id = $1", league.id)
        except Exception:
            pass
        for role_name, channel_id in channel_ids.items():
            ch = guild.get_channel(channel_id)
            if ch:
                try:
                    await ch.delete()
                except Exception:
                    pass
        if commissioner_role:
            try:
                await commissioner_role.delete()
            except Exception:
                pass
        if category:
            try:
                await category.delete()
            except Exception:
                pass
        if isinstance(exc, DBAError):
            raise
        raise DBAError(
            f"League setup failed — server has been cleaned up. Please try `/league create` again. "
            f"Error: {type(exc).__name__}: {exc}"
        ) from exc

    # Place the commissioner role just below the bot's managed role.
    # Team roles are created lazily on /team assign, so only the commissioner role exists now.
    bot_top_role = guild.me.top_role if guild.me else None
    if bot_top_role and bot_top_role.position > 1:
        try:
            await guild.edit_role_positions(positions={
                commissioner_role: max(1, bot_top_role.position - 1)
            })
        except discord.HTTPException as exc:
            log.warning(f"Could not reorder commissioner role: {exc}")

    await trade_repo.seed_picks_for_league(pool, league.id, season_year)

    # Roster import must come before archetype backfill and philosophy
    # randomisation so those steps operate on the actual seeded players.
    try:
        from services import roster_seed_service
        await roster_seed_service.apply_reseed(pool, league.id, league.current_season, dry_run=False)
    except FileNotFoundError:
        pass  # No seed for this year — silently skip
    except Exception as exc:
        log.warning(f"Roster reseed at league creation failed: {exc}")

    # Archetype backfill: populate defensive_archetype immediately after
    # roster import and before plan derivation.  Plans derived with NULL
    # archetypes produce wrong core/flex/surplus buckets (defensive
    # specialists land in surplus).  Failure is non-fatal — plans will
    # derive without archetype data and can be corrected later.
    try:
        from scripts.backfill_overall_from_defense import backfill_league_archetypes
        updated = await backfill_league_archetypes(
            pool, league_id=league.id, season=league.current_season
        )
        log.info(
            "league_service: archetype backfill updated %d players for league %d",
            updated, league.id,
        )
    except Exception as exc:
        log.warning(
            "league_service: archetype backfill failed for league %d — "
            "plans will derive without archetype data: %s",
            league.id, exc,
        )

    await _randomize_philosophies(pool, league.id)

    await _post_onboarding_guide(guild, pool, league.id, name)
    log.info(f"League '{name}' created in guild {guild.id} by {commissioner.id}")
    return league


async def _post_onboarding_guide(guild: discord.Guild, pool, league_id: int, name: str) -> None:
    channel_id = await league_repo.get_channel(pool, league_id, "league-news")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return

    embed = discord.Embed(
        title=f"🏀 Welcome to {name}!",
        description="The league is live. Here's how to get started:",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Step 1 — Claim Teams",
        value="`/team assign @user TEAMCODE` — assign managers (e.g., `/team assign @koby LAL`)\n`/team list` — see all 30 teams",
        inline=False,
    )
    embed.add_field(
        name="Step 2 — Start the Season",
        value="`/season start` — generates the 82-game schedule and begins play",
        inline=False,
    )
    embed.add_field(
        name="Step 3 — Sim Games",
        value="All managers `/team ready` up, then commissioner runs `/sim to-next-rival` to advance to the first human matchup",
        inline=False,
    )
    embed.add_field(
        name="Useful Commands",
        value="`/league status` · `/season standings` · `/roster [team]` · `/help` · `/strategy view`",
        inline=False,
    )
    embed.set_footer(text="Good luck! 🏆 Use /help at any time for phase-aware command guidance.")
    await channel.send(embed=embed)


async def assign_manager(
    guild: discord.Guild,
    league: league_repo.League,
    team_code: str,
    user: discord.Member,
) -> team_repo.Team:
    pool = await get_pool()

    team = await team_repo.get_by_code(pool, league.id, team_code)
    if not team:
        raise DBAError(f"No team found with code **{team_code.upper()}**.")

    if team.manager_user_id is not None:
        old_member = guild.get_member(team.manager_user_id)
        if old_member:
            old_role_id = await league_repo.get_team_role(pool, league.id, team.id)
            if old_role_id:
                old_role = guild.get_role(old_role_id)
                if old_role:
                    await old_member.remove_roles(old_role, reason="DBA manager replaced")

    existing_team = await team_repo.get_by_manager(pool, league.id, user.id)
    if existing_team:
        raise DBAError(
            f"{user.mention} already manages the **{existing_team.full_name}**. Remove them first."
        )

    await team_repo.set_manager(pool, team.id, user.id)
    await team_repo.log_manager_change(pool, team.id, user.id, assigned_by=user.id)

    # Get or lazily create the team's Discord role (roles are deferred from /league create
    # to avoid hitting Discord's per-guild rate limit during bulk creation).
    role_id = await league_repo.get_team_role(pool, league.id, team.id)
    role = guild.get_role(role_id) if role_id else None
    if not role:
        try:
            role = await guild.create_role(
                name=team.full_name,
                reason=f"DBA team role for {team.full_name} (lazy create on first assign)",
            )
            await league_repo.add_role(pool, league.id, "team", team.id, role.id)
        except Exception as exc:
            log.warning(f"Could not create team role for {team.full_name}: {exc}")
            role = None
    if role:
        await user.add_roles(role, reason=f"DBA: assigned manager of {team.full_name}")

    team.manager_user_id = user.id
    return team


async def remove_manager(
    guild: discord.Guild,
    league: league_repo.League,
    team_code: str,
    requester: discord.Member,
) -> team_repo.Team:
    pool = await get_pool()

    team = await team_repo.get_by_code(pool, league.id, team_code)
    if not team:
        raise DBAError(f"No team found with code **{team_code.upper()}**.")

    if team.manager_user_id is None:
        raise DBAError(f"**{team.full_name}** has no manager — it's already CPU controlled.")

    member = guild.get_member(team.manager_user_id)
    if member:
        role_id = await league_repo.get_team_role(pool, league.id, team.id)
        if role_id:
            role = guild.get_role(role_id)
            if role:
                await member.remove_roles(role, reason=f"DBA: manager removed from {team.full_name}")

    await team_repo.set_manager(pool, team.id, None)
    await team_repo.log_manager_change(pool, team.id, None, assigned_by=requester.id)

    log.info(f"Manager removed from team {team.id} by {requester.id}")
    team.manager_user_id = None
    return team


async def get_league(guild_id: int) -> Optional[league_repo.League]:
    pool = await get_pool()
    return await league_repo.get_by_guild(pool, guild_id)


async def advance_phase(league_id: int, new_phase: str) -> None:
    """Update the league's current_phase in DB.

    PT1: validates the transition against the phase-adjacency graph
    (phase/graph.py) before writing. Previously this was a blind UPDATE that
    accepted any string parsing as a valid Phase member, with zero check that
    the move was legal from the league's actual current phase -- see
    docs/design/phase-transition-logic-rules.md.
    """
    from core.errors import PhaseError  # deferred to avoid circular import
    from phase.graph import is_legal_transition, legal_next_phases
    from phase.states import Phase  # deferred to avoid circular import
    pool = await get_pool()

    target = Phase(new_phase)  # raises ValueError for a string that isn't a real Phase member

    row = await pool.fetchrow("SELECT current_phase FROM leagues WHERE id = $1", league_id)
    if row is None:
        raise DBAError(f"League {league_id} not found.")
    current = row["current_phase"]

    if not is_legal_transition(current, target.value):
        legal = legal_next_phases(current)
        legal_str = ", ".join(legal) if legal else "(no legal next phase from here)"
        raise PhaseError(
            f"Cannot advance from `{current}` to `{target.value}` — not a legal phase "
            f"transition. Legal next phase(s) from `{current}`: {legal_str}."
        )

    await pool.execute(
        "UPDATE leagues SET current_phase = $1 WHERE id = $2",
        target.value,
        league_id,
    )


async def ensure_channels(guild: discord.Guild, pool, league_id: int) -> list[str]:
    """Create any missing CHANNEL_ROLES channels for an existing league.

    Idempotent — skips roles already registered in league_channels, and skips
    Discord channel creation when a channel with that name already exists in
    the league's category.  Returns a list of role names that were created.
    """
    # Find the category owned by this league (first registered channel's category).
    rows = await pool.fetch(
        "SELECT discord_channel_id FROM league_channels WHERE league_id = $1 LIMIT 1",
        league_id,
    )
    category: Optional[discord.CategoryChannel] = None
    if rows:
        existing_ch = guild.get_channel(rows[0]["discord_channel_id"])
        if existing_ch and existing_ch.category:
            category = existing_ch.category

    created: list[str] = []
    for role in CHANNEL_ROLES:
        registered_id = await league_repo.get_channel(pool, league_id, role)
        if registered_id and guild.get_channel(registered_id):
            # Already registered and the Discord channel still exists — nothing to do.
            continue

        # Find or create the Discord channel.
        discord_ch: Optional[discord.TextChannel] = None
        if category:
            discord_ch = discord.utils.get(category.text_channels, name=role)

        if discord_ch is None:
            if category:
                discord_ch = await category.create_text_channel(role)
            else:
                discord_ch = await guild.create_text_channel(role)

        await league_repo.add_channel(pool, league_id, role, discord_ch.id)
        created.append(role)
        log.info(f"ensure_channels: created '{role}' (id={discord_ch.id}) for league {league_id}")

    return created
