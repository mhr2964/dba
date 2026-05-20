"""Ride-along Phase 4 smoke test / demo script.

Activates ride-along mode and triggers:
  1. One role re-derive for CLE (surfaces role_assignment event)
  2. One mock philosophy assignment for a test team (surfaces philosophy_assigned)
  3. One mock trade decision (surfaces existing trade prompt)
  4. summarize_session() at the end

Run from project root (ride-along prompts will appear interactively):

    $env:DBA_RIDE_ALONG = "1"
    .venv/Scripts/python.exe scripts/_ride_along_demo.py

To run non-interactively (auto-accept everything via EOF):

    $env:DBA_RIDE_ALONG = "1"
    echo "" | .venv/Scripts/python.exe scripts/_ride_along_demo.py

To suppress role pauses (trade-only mode):

    $env:DBA_RIDE_ALONG = "1"
    $env:RIDE_ALONG_ROLE_PAUSE = "0"
    .venv/Scripts/python.exe scripts/_ride_along_demo.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(PROJECT_ROOT / ".env")

# Force ride-along on for this demo if not already set.
if os.environ.get("DBA_RIDE_ALONG") != "1":
    os.environ["DBA_RIDE_ALONG"] = "1"
    print("[demo] DBA_RIDE_ALONG=1 set automatically for this run.")

import asyncpg  # noqa: E402

from services import ride_along  # noqa: E402
from services.ride_along import summarize_session  # noqa: E402


async def go() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])

    # ── Resolve league and season ────────────────────────────────────────────
    lg = await conn.fetchval("SELECT id FROM leagues ORDER BY id DESC LIMIT 1")
    if lg is None:
        print("[demo] No league found. Run the headless test or create a league first.")
        await conn.close()
        return

    season = await conn.fetchval(
        "SELECT COALESCE(MAX(season), 2024) FROM games WHERE league_id = $1", lg
    )
    print(f"[demo] Using league={lg}  season={season}\n")

    # ── 1. Role re-derive for CLE ────────────────────────────────────────────
    print("=== DEMO STEP 1: Role re-derive for CLE ===")
    cle_id = await conn.fetchval(
        "SELECT id FROM teams WHERE nba_team_code = $1 AND league_id = $2",
        "CLE", lg,
    )
    if cle_id is None:
        print("[demo] CLE not found in league — skipping role derive step.")
    else:
        from services.role_service import derive_and_persist_all_for_team
        await derive_and_persist_all_for_team(conn, lg, cle_id, season)
        print("[demo] CLE role derive complete.\n")

    # ── 2. Mock philosophy assignment ────────────────────────────────────────
    print("=== DEMO STEP 2: Philosophy assignment (mock — BOS) ===")
    bos_id = await conn.fetchval(
        "SELECT id FROM teams WHERE nba_team_code = $1 AND league_id = $2",
        "BOS", lg,
    )
    if bos_id is None:
        print("[demo] BOS not found — skipping philosophy step.")
    else:
        # Emit directly so we don't actually write to the DB in the demo.
        # In real league create, _randomize_philosophies does the DB write.
        bos_row = await conn.fetchrow(
            "SELECT coach_philosophy FROM teams WHERE id = $1", bos_id
        )
        demo_philosophy = bos_row["coach_philosophy"] or "star_maxer"
        action = ride_along.emit_philosophy_assigned(
            league_id=lg,
            team_id=bos_id,
            team_code="BOS",
            philosophy=demo_philosophy,
        )
        print(f"[demo] Philosophy action for BOS: {action}\n")

    # ── 3. Mock trade decision ────────────────────────────────────────────────
    print("=== DEMO STEP 3: Mock trade decision ===")
    result = ride_along.prompt_decision(
        decision_type="propose",
        header="CPU [MIL] wants to PROPOSE → [PHX]",
        details={
            "offering": ["Giannis Antetokounmpo (OVR 97)"],
            "receiving": ["Devin Booker (OVR 90)", "2025 R1 pick"],
            "score_diff": "+4.2 (MIL favors)",
        },
        default_action="approve",
    )
    print(f"[demo] Trade result: {result['action']}\n")

    await conn.close()

    # ── 4. Session summary ────────────────────────────────────────────────────
    print("\n=== DEMO STEP 4: Session summary ===")
    summarize_session()
    print("\n[demo] Done. Check headless_logs/ for the JSONL session file.")


if __name__ == "__main__":
    asyncio.run(go())
