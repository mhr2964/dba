# DBA Services — Extensibility Guide

Each section covers one extension point. The goal: a developer with no prior
context can add a new philosophy, signal, role, persona, or intel provider
in roughly 10 minutes by following the template, then confirm the safety nets
caught any drift on next startup.

---

## 1. How to add a new philosophy

**What it is:** A coaching bias function that adjusts role-assignment scores.
Used by `role_service.get_or_derive_roles` when assigning players to roles.

**Files to touch:** one new file + one Alembic migration.

### Step 1 — Create `services/philosophies/<name>.py`

```python
# services/philosophies/aggressive_closer.py
from __future__ import annotations
from ._registry import register_philosophy

@register_philosophy("aggressive_closer")
def bias(player: dict, role: str, base_score: float, *, ovr_rank: int, team_context: dict) -> float:
    """Pushes clutch_rating ≥ 70 players into primary offensive roles late in the season."""
    if player.get("clutch_rating", 0) >= 70 and ovr_rank <= 5:
        if role in ("iso_scorer", "primary_initiator", "wing_creator"):
            return base_score + 20
    return base_score
```

The `services/philosophies/__init__.py` auto-discovers all non-underscore
sibling modules at import time — no further registration code needed.

### Step 2 — Alembic migration

Extend the `teams_coach_philosophy_check` constraint to include the new name:

```sql
ALTER TABLE teams DROP CONSTRAINT teams_coach_philosophy_check;
ALTER TABLE teams ADD CONSTRAINT teams_coach_philosophy_check
    CHECK (coach_philosophy IN (
        'tendency_respecter', 'star_maxer', 'egalitarian',
        'defense_first', 'vet_overrater', 'youth_developer',
        'chaos', 'aggressive_closer'   -- new value here
    ));
```

### Step 3 — Verify

`assert_philosophy_constraint_sync` fires at bot startup and raises
`RuntimeError` if any key in `PHILOSOPHY_BIASES` is absent from the constraint.
The error surfaces in startup logs immediately — no silent drift.

---

## 2. How to add a new context signal

**What it is:** A trade-evaluation signal that fires when an incoming player
meets some condition relative to the receiving team. Contributes a `delta`
(positive = good fit, negative = bad fit) that shifts the CPU's accept/reject math.

**Files to touch:** one new file only.

### Create `services/trade_signals/<name>.py`

```python
# services/trade_signals/clutch_fit.py
"""Clutch-rating fit detector: rewards high-clutch players joining close-game teams."""
from __future__ import annotations
from core.logging import get_logger
from ._registry import SignalContext, register_signal

log = get_logger(__name__)

@register_signal("clutch_fit")
async def detect(ctx: SignalContext):
    from services.trade_context import ContextSignal  # avoid circular at module level

    if ctx.incoming_player.get("clutch_rating", 0) < 70:
        return None

    # Check if the team has lost many close games (wins < projected from posture).
    projected = ctx.posture.get("projected_wins", 41)
    wins = ctx.posture.get("wins", 0)
    losses = ctx.posture.get("losses", 0)
    if wins + losses < 20:
        return None  # too early in season to judge

    if wins < projected * 0.85:
        return ContextSignal(
            delta=+0.05,
            reason="adds clutch scoring this team clearly needs — they've underperformed projections.",
            code="clutch_fit",
        )
    return None
```

`services/trade_signals/__init__.py` auto-discovers all sibling modules. No
further registration is needed. The signal will appear in ride-along narrative
output and in Marcus Cole's context for blockbuster trades.

**No DB migration required for signals.**

---

## 3. How to add a new role

**What it is:** A named player role with touch share, shot profile, minutes
tier, and defensive classification. Used by `role_service.get_or_derive_roles`.

**Files to touch:** `services/role_service.py` + one Alembic migration.

### Step 1 — Add to `ROLE_REGISTRY` in `role_service.py`

```python
# Inside ROLE_REGISTRY dict in services/role_service.py
"shot_creator": {
    "touch_share": 0.21,
    "fga_3pa_pct": 0.45,
    "fta_per_fga": 0.28,
    "minutes_tier": "starter",           # "starter" | "rotation" | "bench" | "depth"
    "defensive_role": "general",         # "anchor" | "perimeter" | "general" | "passive"
    "scheme_synergy": ["isolation", "ball_movement"],
    "tendencies_boosted": ["tendency_drive", "tendency_3pt"],
},
```

### Step 2 — Alembic migration

Extend `player_roles_role_check` to include the new role name. Same pattern
as the philosophy constraint above.

### Step 3 — Verify

`assert_role_constraint_sync` raises `RuntimeError` at startup if any key in
`ROLE_REGISTRY` is absent from the DB constraint.

---

## 4. How to add a new persona

**What it is:** A columnist character with a distinct voice and set of intel
slices they consume. Each persona writes a specific category of article.

**Files to touch:** one new file only.

### Create `services/personas/<name>.py`

```python
# services/personas/elena_ross.py
from __future__ import annotations
from services.personas.base import Persona
from services.personas._registry import register_persona

elena_ross = register_persona(Persona(
    id="elena_ross",
    display_name="Elena Ross",
    byline="The Long View · DBA Future Watch",
    avatar_emoji="🔭",
    voice_notes=(
        "You are Elena Ross, the DBA's contract and future-assets analyst. "
        "This league is the DBA — always say DBA. "
        "You care about: cap flexibility, draft capital, long-term windows. "
        "One paragraph max. Be precise and forward-looking. "
        'Return ONLY valid JSON: {"headline": "...", "body": "..."}'
    ),
    categories=("future_watch", "power_rankings"),
    # Declare which intel slices the AI will receive.
    # Keys must match INTEL_PROVIDERS in columnist_intel.py.
    context_keys=("posture", "plan"),
))
```

`services/personas/__init__.py` auto-discovers sibling modules. The persona
is immediately available via `PERSONAS["elena_ross"]`.

**Call site:** invoke `columnist_service.generate` with a `ColumnistRequest`:

```python
from services.columnist_service import generate, ColumnistRequest

article = await generate(
    pool, league_id, season,
    ColumnistRequest(
        persona_id="elena_ross",
        category="future_watch",
        subject_team_ids=[team_id],
        extra_context={"custom_key": "value"},
    ),
)
```

**No DB migration required for personas.**

---

## 5. How to add a new intel provider

**What it is:** An async function that fetches additional data for the
columnist prompt, keyed by a string that personas declare in `context_keys`.

**Files to touch:** `services/columnist_intel.py` (or a sibling module that
imports `register_intel_provider` from it).

### Add to `services/columnist_intel.py`

```python
from services.columnist_intel import register_intel_provider

@register_intel_provider("cap_space")
async def _provide_cap_space(pool, league, season: int, team_ids: list[int]) -> dict:
    """Return estimated cap space for each team."""
    result = {}
    for tid in team_ids:
        row = await pool.fetchrow(
            "SELECT cap_space FROM team_financials WHERE team_id = $1 AND season = $2",
            tid, season,
        )
        if row:
            result[tid] = {"cap_space": row["cap_space"]}
    return result
```

Then declare `"cap_space"` in the persona's `context_keys` tuple. The intel
builder will call this provider and merge its output into the team_intel block
injected into the prompt.

**No DB migration required** unless the provider itself reads a new table.

---

## 6. The startup safety nets

Two async functions fire during bot startup, after the DB connection is
established (`bot/client.py`, inside `on_ready`):

### `assert_philosophy_constraint_sync(conn)`
- Defined in `services/philosophies/__init__.py`
- Checks: every key in `PHILOSOPHY_BIASES` (= `REGISTRY`) appears in the
  `teams_coach_philosophy_check` DB constraint
- If any key is missing → raises `RuntimeError` with the missing names
- The error logs at ERROR level and surfaces during startup — the bot will
  continue but the problem is immediately visible

### `assert_role_constraint_sync(conn)`
- Defined in `services/role_service.py`
- Checks: every key in `ROLE_REGISTRY` appears in the
  `player_roles_role_check` DB constraint
- Same raise-on-drift behaviour as above

Both functions use `pg_get_constraintdef` to read the live constraint text,
so they catch real DB state, not a cached copy.

**What they do NOT check:** trade signal codes, intel provider keys, persona
ids. Those are validated at call time (unknown keys are silently skipped or
log a warning).

---

## 7. Naming conventions

| Thing | Convention | Example |
|---|---|---|
| Slash commands | kebab-case | `/sim-range`, `/trade-review` |
| Philosophy names | `snake_case`, noun or adjective | `vet_overrater` |
| Role names | `snake_case`, descriptive | `rim_protector`, `glue_guy` |
| Signal codes | `snake_case`, `<domain>_<outcome>` | `synergy_overlap`, `window_fit_match` |
| Persona IDs | `snake_case`, human name | `marcus_cole`, `pat_chen` |
| Intel provider keys | `snake_case`, short noun | `posture`, `recent_pivots` |
| Category strings | `snake_case`, article type | `trade_report`, `tank_watch` |
| Enum-like DB strings | lowercase, underscores | `"rebuilding"`, `"pushing"` |
| Persona file names | match persona id | `marcus_cole.py` |
| Philosophy file names | match philosophy name | `vet_overrater.py` |

---

## 8. Recurring anti-pattern — "code looks correct but a precondition isn't met"

The most common debugging scenario in this codebase: a function is wired up
correctly but silently returns early because a precondition is false (empty
standings cache, no `player_roles` rows, missing league row, zero games
played, etc.).

**How to diagnose it:** run the diagnostic scripts in `scripts/`:

```bash
python scripts/_team_intel_diag.py        # confirms team_intel fetches real data
python scripts/_league_digest_diag.py     # confirms league_digest produces output
```

These scripts write directly to stdout with no Discord layer, so you see the
raw data or the specific empty-result branch that fired.

**Pattern:** when adding any new service that reads from the DB, write a
`scripts/_<name>_diag.py` that prints the raw output for a known league_id.
It costs 20 lines and saves hours of guessing.

**The silent-return trap:** many functions in this codebase guard with
`if not subject_team_ids: return {}` or similar. When something is missing
from the prompt, check the logs for "skipping" / "failed" warning lines
first, then add a diag script to see what the DB actually contains.
