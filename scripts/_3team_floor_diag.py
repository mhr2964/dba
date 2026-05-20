"""
3-team floor diagnostic — verify per-team ratio checks catch lopsided deals.

Does NOT require a live league or DB connection.  Uses player_trade_value and
pick_trade_value directly with fabricated inputs that mirror real observed cases.

Run:
    python scripts/_3team_floor_diag.py

Output shows, for each scenario:
    pkg_val   — what A has to offer B from its trade block (gap-check value)
    sec_val   — value of secondary player A sends to C
    b_leg2    — corrected leg-2 value B actually receives from A (pkg_val - sec_val)
    filler    — pick value C sends (via A) to B
    b_in      — total incoming value for B = b_leg2 + filler
    b_out     — what B gives up (target player)
    ratios    — A/B/C ratios vs _MIN_3WAY_RATIO threshold
    verdict   — PASS (floor fires, deal blocked) or FAIL (floor missed it)
"""
from __future__ import annotations

import io
import sys
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import trade_evaluator  # noqa: E402

_MIN_3WAY_RATIO = 0.70   # must match cpu_trade_service._MIN_3WAY_RATIO
_SALARY_CAP = 140_588_000

# Scenario definition keys:
#   name         — human label
#   sec_ovr      — secondary player (A→C) overall rating
#   sec_age      — secondary player age
#   target_ovr   — target player (B→A) overall rating
#   target_age   — target player age
#   filler_year  — draft pick year (C→B)
#   filler_round — draft pick round (1 or 2)
#   current_season
#   pkg_val_approx — approximation of what _build_return_package returned in gap-check
#                    (this should equal sec_val + some_leg2_players in the old buggy code,
#                     or just the leg-2 players in the fixed code)
#   expected_blocked — True if the floor SHOULD fire and block this deal

SCENARIOS = [
    {
        # The UTA/PHX/MIL trade that fired despite the floor.
        # UTA(A) sec=Sexton(OVR 77) → PHX(C) sends 2024 R2 → MIL(B) sends Rollins(OVR 78)
        # Old bug: pkg_val in floor check included Sexton's value (~8 pts) even though
        # Sexton goes to PHX (C), not MIL (B).
        "name": "UTA/PHX/MIL (Sexton to PHX, PHX-R2 to MIL, Rollins to UTA) [BUG CASE]",
        "sec_ovr": 77, "sec_age": 24,
        "target_ovr": 78, "target_age": 23,
        "filler_year": 2024, "filler_round": 2,
        "current_season": 2025,
        # pkg_val from gap-check included Sexton (secondary) — that was the bug.
        # In the old code: pkg_val ≈ sec_val + 0 = ~8+0 = ~8.
        # The old _b_value_in = pkg_val + filler = 8 + 1 = 9 vs target = ~10.5 → ratio 0.86 PASSES 0.60.
        # The new _b_leg2 = max(0, pkg_val - sec_val) = max(0, 8-8) = 0
        # New _b_value_in = 0 + 1 = 1 vs target = ~10.5 → ratio 0.10 BLOCKED by 0.70.
        "pkg_val_from_gap_check": None,  # computed below from sec_val
        "expected_blocked": True,
    },
    {
        # Near-miss scenario: A's block has sec player (OVR 72, val ~43.6) AND a separate
        # leg-2 player (OVR 75, val ~46.2).  pkg_val from gap-check = 43.6 + 46.2 = 89.8.
        # But target (OVR 80, val ~42.3) < pkg_val, so gap_ratio < 0 — this trade would
        # never be selected in the candidate loop (gap_ratio must be 0.20-0.40).
        # This confirms: fair 3-team deals can only happen when A has real substantial
        # leg-2 players that make pkg_val close to target.  The current 3-team
        # construction (secondary-only gap filler) is correctly blocked by the floor.
        # Here we set pkg_val to a hypothetical value where A has solid leg-2 assets.
        # sec=43.6, pkg_val=89.8 (gap-check includes sec + leg2 worth 46.2).
        # Fixed: b_leg2 = 89.8-43.6=46.2, b_in=46.2+5.2=51.4, b_out=42.3, b_ratio=1.21 PASSES.
        # But a_ratio = 42.3 / (43.6 + 46.2 + 5.2) = 42.3 / 95 = 0.45 — A ratio fails.
        # This is correct: if A sends a sec AND a full-value leg-2 player for a weaker target,
        # the trade shouldn't fire.  expected_blocked=True.
        "name": "A sends 2 block players but target too weak (a_ratio fails)",
        "sec_ovr": 72, "sec_age": 25,
        "target_ovr": 80, "target_age": 27,
        "filler_year": 2025, "filler_round": 2,
        "current_season": 2025,
        "pkg_val_from_gap_check": None,
        "_pkg_val_override": 89.8,  # sec(43.6) + leg2 player(46.2)
        "expected_blocked": True,
    },
    {
        # C gets a bad deal: C sends current-season R1 (~36.4 pts) but secondary player
        # is OVR 65 age 35 (~19.1 pts).  c_ratio = 19.1/36.4 = 0.53 < 0.70 — blocked.
        "name": "C gets robbed (current R1 pick for OVR-65 age-35 player)",
        "sec_ovr": 65, "sec_age": 35,
        "target_ovr": 82, "target_age": 26,
        "filler_year": 2025, "filler_round": 1,
        "current_season": 2025,
        "pkg_val_from_gap_check": None,
        "expected_blocked": True,
    },
    {
        # A ratio fails: A sends too much (sec OVR 78 val ~63.8 + leg-2 ~30 + filler 5.2)
        # for a weak target OVR 74 val ~37.8.
        # a_ratio = 37.8 / (63.8 + 30 + 5.2) = 37.8 / 99 = 0.38 — blocked by 0.70 floor.
        # pkg_val_override = sec_val(63.8) + leg-2(30) = 93.8 from gap-check.
        "name": "A overpays (sec OVR 78 + leg-2 for weak OVR 74 target)",
        "sec_ovr": 78, "sec_age": 24,
        "target_ovr": 74, "target_age": 28,
        "filler_year": 2025, "filler_round": 2,
        "current_season": 2025,
        "pkg_val_from_gap_check": None,
        "_pkg_val_override": 93.8,   # sec_val(63.8) + 30 pts of leg-2 players
        "expected_blocked": True,
    },
]

# Standard contract used for all players in this diagnostic (fair contract, neutral modifier).
_NEUTRAL_CONTRACT = {"salary": int(_SALARY_CAP * 0.10), "years_remaining": 2}


def _pval(ovr: int, age: int) -> float:
    return trade_evaluator.player_trade_value(
        {"overall": ovr, "age": age},
        _NEUTRAL_CONTRACT,
        _SALARY_CAP,
    )


def _pickval(year: int, rnd: int, current_season: int) -> float:
    return trade_evaluator.pick_trade_value(year, rnd, current_season)


def run_scenarios() -> None:
    print(f"\n{'='*80}")
    print(f"3-team floor diagnostic  (threshold = {_MIN_3WAY_RATIO})")
    print(f"{'='*80}\n")

    all_pass = True
    for sc in SCENARIOS:
        cur = sc["current_season"]
        sec_val = _pval(sc["sec_ovr"], sc["sec_age"])
        target_val = _pval(sc["target_ovr"], sc["target_age"])
        filler_val = _pickval(sc["filler_year"], sc["filler_round"], cur)

        # pkg_val from the gap-check loop in cpu_trade_service.
        # Old (buggy) code would include secondary player in pkg_val.
        # We simulate both old and new behavior.
        if sc.get("_pkg_val_override") is not None:
            pkg_val = sc["_pkg_val_override"]
        else:
            # Default: assume the ONLY asset A had was the secondary player
            # (worst-case — reproduces the UTA/MIL bug exactly).
            pkg_val = sec_val

        # ── OLD (buggy) formula ─────────────────────────────────────────────
        old_b_value_in = pkg_val + filler_val
        old_b_ratio = old_b_value_in / max(target_val, 1.0)

        # ── NEW (fixed) formula ─────────────────────────────────────────────
        b_leg2_val = max(0.0, pkg_val - sec_val)   # strip secondary player from B's incoming
        new_b_value_in = b_leg2_val + filler_val
        new_b_ratio = new_b_value_in / max(target_val, 1.0)

        # C ratio (unchanged — always was correct)
        c_ratio = sec_val / max(filler_val, 1.0)

        # A ratio (fixed: A's out = sec + leg2 + filler_pick)
        a_value_out = sec_val + b_leg2_val + filler_val
        a_ratio = target_val / max(a_value_out, 1.0)

        # Verdict: deal is blocked if ANY ratio < threshold
        old_blocked = old_b_ratio < _MIN_3WAY_RATIO or c_ratio < _MIN_3WAY_RATIO or a_ratio < _MIN_3WAY_RATIO
        new_blocked = new_b_ratio < _MIN_3WAY_RATIO or c_ratio < _MIN_3WAY_RATIO or a_ratio < _MIN_3WAY_RATIO

        expected = sc["expected_blocked"]
        test_ok = (new_blocked == expected)
        if not test_ok:
            all_pass = False

        status = "OK " if test_ok else "FAIL"
        print(f"[{status}] {sc['name']}")
        print(
            f"       sec_val={sec_val:.1f}  target_val={target_val:.1f}  "
            f"filler_val={filler_val:.1f}  pkg_val(gap-check)={pkg_val:.1f}"
        )
        print(
            f"       OLD: b_in={old_b_value_in:.1f}  b_ratio={old_b_ratio:.2f}  "
            f"blocked={old_blocked} {'[BUG - missed]' if not old_blocked and expected else ''}"
        )
        print(
            f"       NEW: b_leg2={b_leg2_val:.1f}  b_in={new_b_value_in:.1f}  "
            f"b_ratio={new_b_ratio:.2f}  c_ratio={c_ratio:.2f}  a_ratio={a_ratio:.2f}  "
            f"blocked={new_blocked}"
        )
        print(f"       expected_blocked={expected}  result={'PASS' if test_ok else 'FAIL'}")
        print()

    print(f"{'─'*80}")
    if all_pass:
        print("All scenarios passed.")
    else:
        print("SOME SCENARIOS FAILED — floor logic needs review.")
    print()


if __name__ == "__main__":
    run_scenarios()
