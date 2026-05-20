"""Headless season test harness — drives a full DBA season + playoffs in seconds.

No Discord connection required. All Discord side effects are bypassed:
- League creation uses league_service.create_headless()
- Team assignment writes directly to the DB
- sim_range is called with a MockGuild that no-ops all Discord calls

CPU manager reasoning is printed to stdout when DBA_HEADLESS_MODE=1.

Run from project root:
    $env:DBA_HEADLESS_MODE = "1"
    .venv/Scripts/python.exe scripts/headless_test.py

    # With forced CPU trade rounds every 5 sim batches:
    $env:DBA_HEADLESS_MODE = "1"
    .venv/Scripts/python.exe scripts/headless_test.py --force-trades

Why --force-trades is needed:
    In normal headless runs, `_maybe_run_cpu_trades` is gated on `box_channel`
    being truthy (batch_sim_runner.py lines 2145/2175: `if ... and box_channel`).
    MockGuild.get_channel() always returns None, so box_channel is always None
    and the CPU trade block is unreachable.  --force-trades bypasses that gate
    by calling cpu_trade_service.maybe_initiate_round directly from the harness.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ── stdout encoding (Windows safe) ─────────────────────────────────────────
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import asyncpg

# Fake discord objects so services that import discord don't crash.
# We only need a no-op Guild that satisfies the isinstance checks enough
# to not raise AttributeError on .get_channel(), etc.
class _FakeChannel:
    """A channel object that silently absorbs all method calls."""
    id = 0
    category_id = None

    async def send(self, *a, **kw):
        pass

    async def delete(self, *a, **kw):
        pass

    def get(self, key, default=None):
        return default


class MockGuild:
    """No-op Discord guild. All methods that batch_sim_runner calls are present."""
    id: int = 0
    me = None
    name: str = "HeadlessGuild"

    def get_channel(self, channel_id: Optional[int]) -> None:
        return None

    def get_member(self, user_id: int) -> None:
        return None

    def get_role(self, role_id: int) -> None:
        return None

    async def fetch_channels(self):
        return []

    async def fetch_roles(self):
        return []

    async def create_category(self, *a, **kw):
        return _FakeChannel()

    async def create_text_channel(self, *a, **kw):
        return _FakeChannel()

    async def create_role(self, *a, **kw):
        return None

    async def edit_role_positions(self, *a, **kw):
        pass


DATABASE_URL = os.environ["DATABASE_URL"]

# ── fake user IDs (no real Discord users needed) ──────────────────────────
COMMISSIONER_ID = 1_000_000_001
MANAGER_LAL_ID  = 1_000_000_002
MANAGER_BOS_ID  = 1_000_000_003
HEADLESS_GUILD_ID = 1_000_000_000   # arbitrary; avoids collision with real guild

SEASON_YEAR = 2024
LEAGUE_NAME = "HeadlessTestLeague"

# ── progress printer ───────────────────────────────────────────────────────
_t0 = time.monotonic()

def progress(msg: str) -> None:
    elapsed = time.monotonic() - _t0
    print(f"[T+{elapsed:.0f}s] {msg}", flush=True)


# ── DB wipe ────────────────────────────────────────────────────────────────
async def wipe_db(conn: asyncpg.Connection) -> int:
    result = await conn.execute("DELETE FROM leagues")
    deleted = int(result.split()[-1]) if result.startswith("DELETE") else 0
    return deleted


# ── standings helper ───────────────────────────────────────────────────────
async def print_standings(pool, league_id: int, season: int) -> None:
    rows = await pool.fetch(
        """
        SELECT t.nba_team_code AS code, t.conference,
               COALESCE(sc.wins, 0) AS wins, COALESCE(sc.losses, 0) AS losses
        FROM teams t
        LEFT JOIN standings_cache sc
               ON sc.team_id = t.id AND sc.league_id = $1 AND sc.season = $2
        WHERE t.league_id = $1
        ORDER BY t.conference, COALESCE(sc.wins, 0) DESC, COALESCE(sc.losses, 0) ASC
        """,
        league_id,
        season,
    )
    east = [r for r in rows if r["conference"] == "East"]
    west = [r for r in rows if r["conference"] == "West"]
    print("\n  === FINAL STANDINGS — EAST (top 8) ===")
    for i, r in enumerate(east[:8], 1):
        print(f"  {i:2}. {r['code']}  {r['wins']}-{r['losses']}")
    print("\n  === FINAL STANDINGS — WEST (top 8) ===")
    for i, r in enumerate(west[:8], 1):
        print(f"  {i:2}. {r['code']}  {r['wins']}-{r['losses']}")


async def _force_trade_round(
    pool,
    league_id: int,
    league_season: int,
    deadline_game_index: int,
    total_regular_games: int,
    current_game_index: int,
    mock_guild: "MockGuild",
    trade_counters: dict,
) -> None:
    """
    Directly invoke cpu_trade_service.maybe_initiate_round with elevated pressure.

    Bypasses the box_channel gate in batch_sim_runner.sim_range that prevents
    trades from firing in headless mode.  Pressure is forced to 1.0 so the
    formula always attempts 3 offers per call (int(1.0*2)+1 = 3).
    """
    from services import cpu_trade_service

    # Monkey-patch pressure by calling with a game index that resolves to max
    # pressure: pass current_game_index = deadline_game_index so games_until_deadline=0
    # → ramp=1.0 → pressure=1.0.
    forced_pressure = 1.0
    elapsed = time.monotonic() - _t0
    print(
        f"[T+{elapsed:.0f}s] FORCING CPU trade round "
        f"(pressure={forced_pressure:.1f}, game_index={current_game_index}, "
        f"deadline_game_index={deadline_game_index})",
        flush=True,
    )

    try:
        proposed = await cpu_trade_service.maybe_initiate_round(
            pool=pool,
            league_id=league_id,
            season=league_season,
            current_game_index=deadline_game_index,  # force max pressure
            total_regular_games=total_regular_games,
            deadline_game_index=deadline_game_index,
            guild=mock_guild,
            refresh_block=True,
        )
        trade_counters["attempted"] += proposed
        elapsed = time.monotonic() - _t0
        print(
            f"[T+{elapsed:.0f}s] CPU trade round done: {proposed} proposed this round "
            f"(cumulative attempted={trade_counters['attempted']})",
            flush=True,
        )
    except Exception as exc:
        elapsed = time.monotonic() - _t0
        print(f"[T+{elapsed:.0f}s] CPU trade round ERROR: {exc}", flush=True)


async def main() -> None:
    global _t0
    _t0 = time.monotonic()

    parser = argparse.ArgumentParser(description="DBA headless season test harness")
    parser.add_argument(
        "--force-trades",
        action="store_true",
        default=False,
        help=(
            "Every N sim batches, forcibly call cpu_trade_service.maybe_initiate_round "
            "with max pressure.  Bypasses the box_channel gate that silently suppresses "
            "CPU trades in headless mode."
        ),
    )
    parser.add_argument(
        "--force-trades-interval",
        type=int,
        default=5,
        metavar="N",
        help="Call the forced trade round every N sim batches (default: 5).",
    )
    parser.add_argument(
        "--ride-along",
        action="store_true",
        default=False,
        help=(
            "Pause at every CPU trade decision and ask for user input (approve / "
            "veto / comment / skip).  Requires --force-trades to actually trigger "
            "CPU trade rounds in headless mode.  Sets DBA_RIDE_ALONG=1."
        ),
    )
    args = parser.parse_args()

    force_trades: bool = args.force_trades
    force_trades_interval: int = max(1, args.force_trades_interval)
    ride_along: bool = args.ride_along

    if ride_along:
        os.environ["DBA_RIDE_ALONG"] = "1"

    print("=" * 60)
    print("DBA Headless Season Test Harness")
    print(f"DBA_HEADLESS_MODE={os.environ.get('DBA_HEADLESS_MODE', '0')}")
    if force_trades:
        print(f"--force-trades ON (interval every {force_trades_interval} batches)")
    if ride_along:
        print("--ride-along ON  (DBA_RIDE_ALONG=1 — pausing at every CPU trade decision)")
    print("=" * 60)

    # ── Step 1: Wipe existing leagues ──────────────────────────────────────
    progress("Wiping existing leagues from DB...")
    raw_conn = await asyncpg.connect(DATABASE_URL)
    try:
        deleted = await wipe_db(raw_conn)
    finally:
        await raw_conn.close()
    progress(f"Wiped {deleted} league(s).")

    # ── Bootstrap the DB pool ─────────────────────────────────────────────
    from data import db as _db
    pool = await _db.get_pool()

    # ── Step 2: Create league (Discord-less) ──────────────────────────────
    progress(f"Creating league '{LEAGUE_NAME}' (headless)...")
    from services import league_service
    league = await league_service.create_headless(
        name=LEAGUE_NAME,
        season_year=SEASON_YEAR,
        commissioner_id=COMMISSIONER_ID,
        guild_id=HEADLESS_GUILD_ID,
    )
    progress(f"League created: id={league.id}  phase={league.current_phase}")

    # ── Step 3: Assign 2 human teams (LAL + BOS) ──────────────────────────
    progress("Assigning LAL and BOS to fake managers...")
    from data.repositories import team_repo
    lal = await team_repo.get_by_code(pool, league.id, "LAL")
    bos = await team_repo.get_by_code(pool, league.id, "BOS")
    if not lal or not bos:
        print("FATAL: LAL or BOS not found in teams — seed data missing.")
        sys.exit(1)

    await team_repo.set_manager(pool, lal.id, MANAGER_LAL_ID)
    await team_repo.log_manager_change(pool, lal.id, MANAGER_LAL_ID, assigned_by=COMMISSIONER_ID)
    await team_repo.set_manager(pool, bos.id, MANAGER_BOS_ID)
    await team_repo.log_manager_change(pool, bos.id, MANAGER_BOS_ID, assigned_by=COMMISSIONER_ID)
    progress(f"LAL assigned to fake user {MANAGER_LAL_ID}, BOS to fake user {MANAGER_BOS_ID}")

    # ── Step 4: Advance phase SETUP → PRESEASON_READY → REGULAR_SEASON_ACTIVE
    progress("Advancing phase to PRESEASON_READY...")
    await league_service.advance_phase(league.id, "PRESEASON_READY")
    progress("Advancing phase to REGULAR_SEASON_ACTIVE...")
    await league_service.advance_phase(league.id, "REGULAR_SEASON_ACTIVE")

    # Reload league to confirm phase.
    league_row = await pool.fetchrow("SELECT * FROM leagues WHERE id = $1", league.id)
    progress(f"Phase is now: {league_row['current_phase']}")

    # ── Step 5: Generate schedule ──────────────────────────────────────────
    progress("Generating schedule via schedule_service...")
    from services import schedule_service
    game_count = await schedule_service.generate_season(league.id, SEASON_YEAR)
    progress(f"Schedule generated: {game_count} games")
    if game_count != 1230:
        print(f"WARNING: expected 1230 games, got {game_count}")

    # ── Step 6: Mark both human teams as ready ────────────────────────────
    progress("Marking LAL and BOS as ready...")
    from data.repositories import game_repo
    await game_repo.set_ready(pool, league.id, lal.id, MANAGER_LAL_ID, True)
    await game_repo.set_ready(pool, league.id, bos.id, MANAGER_BOS_ID, True)
    progress("Both human teams marked ready.")

    # ── Step 7: Sim full regular season ───────────────────────────────────
    progress("Starting full regular season sim (force=True)...")
    from services import batch_sim_runner

    mock_guild = MockGuild()
    mock_guild.id = HEADLESS_GUILD_ID

    CHUNK_SIZE = 200  # batch size to print progress mid-sim
    games_simmed_total = 0
    chunk_start = 1
    batch_count = 0

    # Counters for --force-trades summary.
    trade_counters: dict = {"attempted": 0}

    # Resolve deadline game index once (needed for forced trade rounds).
    # game_repo was already imported above in Step 6.
    deadline_game_index: Optional[int] = None
    total_regular_games: int = game_count
    if force_trades:
        deadline_game_index = await game_repo.get_deadline_game_index(pool, league.id, SEASON_YEAR)
        total_regular_games = await game_repo.get_total_regular_season_games(pool, league.id, SEASON_YEAR)
        progress(
            f"Force-trades: deadline_game_index={deadline_game_index}, "
            f"total_regular_games={total_regular_games}"
        )

    while True:
        chunk_end = chunk_start + CHUNK_SIZE - 1
        result = await batch_sim_runner.sim_range(
            league_id=league.id,
            guild=mock_guild,
            season=SEASON_YEAR,
            to_game_index=chunk_end,
            bot=None,
            force=True,
        )
        chunk_simmed = result.get("games_simmed", 0)
        games_simmed_total += chunk_simmed
        batch_count += 1

        # Check current phase to detect season complete.
        phase_row = await pool.fetchrow(
            "SELECT current_phase FROM leagues WHERE id = $1", league.id
        )
        current_phase = phase_row["current_phase"] if phase_row else "?"

        # Count remaining unsimmed games.
        remaining = await pool.fetchval(
            "SELECT COUNT(*) FROM games WHERE league_id=$1 AND season=$2 AND status='scheduled'",
            league.id,
            SEASON_YEAR,
        )
        progress(
            f"simmed {games_simmed_total}/{game_count} games total "
            f"— {remaining} remaining — phase {current_phase}"
        )

        # --force-trades: inject a CPU trade round every N batches.
        # cpu_trade_service guards phase internally, so this is safe to call
        # even when the phase hasn't entered REGULAR_SEASON_ACTIVE yet.
        if (
            force_trades
            and deadline_game_index is not None
            and batch_count % force_trades_interval == 0
            and current_phase in ("REGULAR_SEASON_ACTIVE", "TRADE_DEADLINE_OPEN")
        ):
            await _force_trade_round(
                pool=pool,
                league_id=league.id,
                league_season=SEASON_YEAR,
                deadline_game_index=deadline_game_index,
                total_regular_games=total_regular_games,
                current_game_index=chunk_end,
                mock_guild=mock_guild,
                trade_counters=trade_counters,
            )

        if current_phase in ("REGULAR_SEASON_COMPLETE", "PLAYOFFS_R1", "PLAYIN_ACTIVE") or remaining == 0:
            break

        if chunk_simmed == 0:
            # No games were simmed (all in range already done or blocked) — advance window.
            chunk_start = chunk_end + 1
        else:
            chunk_start = chunk_end + 1

        if chunk_start > game_count + 100:
            progress("WARNING: chunk_start exceeded game count — breaking to avoid infinite loop")
            break

    progress(f"Regular season sim done. Total games simmed: {games_simmed_total}")

    # ── Step 8: Advance to PLAYOFFS_R1 if not already there ──────────────
    phase_row = await pool.fetchrow(
        "SELECT current_phase FROM leagues WHERE id = $1", league.id
    )
    final_phase = phase_row["current_phase"] if phase_row else "?"
    if final_phase == "REGULAR_SEASON_COMPLETE":
        progress("Advancing phase → PLAYOFFS_R1...")
        await league_service.advance_phase(league.id, "PLAYOFFS_R1")
        phase_row = await pool.fetchrow("SELECT current_phase FROM leagues WHERE id = $1", league.id)
        final_phase = phase_row["current_phase"] if phase_row else "?"
    progress(f"Final phase: {final_phase}")

    # ── Step 9: Summary report ─────────────────────────────────────────────
    try:
        articles_count = await pool.fetchval(
            "SELECT COUNT(*) FROM league_articles WHERE league_id = $1",
            league.id,
        ) or 0
    except Exception:
        articles_count = 0

    try:
        trades_count = await pool.fetchval(
            "SELECT COUNT(*) FROM trades WHERE league_id = $1 AND status = 'approved'",
            league.id,
        ) or 0
    except Exception:
        trades_count = 0

    total_wall_time = time.monotonic() - _t0

    print("\n" + "=" * 60)
    print("HEADLESS TEST COMPLETE")
    print("=" * 60)
    print(f"  Wall time:        {total_wall_time:.1f}s")
    print(f"  Games simmed:     {games_simmed_total}")
    print(f"  Final phase:      {final_phase}")
    print(f"  Articles posted:  {articles_count}")
    print(f"  Trades approved:  {trades_count}")
    if force_trades:
        print(f"  [--force-trades]  rounds attempted: {trade_counters['attempted']} proposed")

    await print_standings(pool, league.id, SEASON_YEAR)
    print()

    # Ride-along post-run summary — only prints if --ride-along was on AND
    # at least one decision was logged.
    if ride_along:
        try:
            from services import ride_along as _ra
            _ra.summarize_session()
        except Exception as exc:  # noqa: BLE001
            print(f"[ride_along] summary failed: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
