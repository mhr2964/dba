"""League digest computation — aggregates cross-team signals since a given batch index.

Used by /league digest and the diagnostic script.

Public API
----------
build_league_digest(pool, league, season, since_batch_index) -> dict

Return shape
------------
{
    "mode_changes":         [{"team_code", "old_mode", "new_mode"}, ...],
    "plan_pivots":          [{"team_code", "old_goal", "new_goal"}, ...],
    "role_swaps":           [{"team_code", "player_name", "old_role", "new_role", "assigned_by"}, ...],
    "top_signals":          [{"code", "count", "avg_delta"}, ...],
    "loud_trades":          [{"trade_id", "summary", "headline_signal"}, ...],
    "philosophy_outputs":   [{"team_code", "philosophy", "headline"}, ...],
}

Query budget: one query per slice — 6 queries total.  Aggregation in Python.
"""
from __future__ import annotations

import json
from collections import defaultdict

from core.logging import get_logger
from data.repositories import league_repo

log = get_logger(__name__)

# Philosophies whose outputs are considered "noteworthy" for the digest.
_NOTEWORTHY_PHILOSOPHIES = frozenset({"chaos", "vet_overrater", "youth_developer"})


async def build_league_digest(
    pool,
    league: league_repo.League,
    season: int,
    since_batch_index: int,
) -> dict:
    """Aggregate league events since `since_batch_index`.

    Parameters
    ----------
    pool:
        asyncpg connection pool.
    league:
        League object (carries league.id).
    season:
        Season integer (e.g. 2024).
    since_batch_index:
        sim_batch_index of the earliest snapshot to include.  Everything
        strictly newer than this index (i.e. > since_batch_index) is compared
        against the snapshot AT since_batch_index to compute diffs.

    Returns
    -------
    Dict with keys: mode_changes, plan_pivots, role_swaps, top_signals,
    loud_trades, philosophy_outputs.
    """
    league_id = league.id

    result: dict = {
        "mode_changes":       [],
        "plan_pivots":        [],
        "role_swaps":         [],
        "top_signals":        [],
        "loud_trades":        [],
        "philosophy_outputs": [],
    }

    # ------------------------------------------------------------------
    # Slice 1+2: mode_changes and plan_pivots
    # Query the two most recent snapshots per team in the window.
    # ------------------------------------------------------------------
    try:
        snapshot_rows = await pool.fetch(
            """
            SELECT tss.team_id,
                   t.nba_team_code,
                   tss.sim_batch_index,
                   tss.mode,
                   tss.plan_goal,
                   ROW_NUMBER() OVER (
                       PARTITION BY tss.team_id
                       ORDER BY tss.sim_batch_index DESC
                   ) AS rn
            FROM team_state_snapshots tss
            JOIN teams t ON t.id = tss.team_id
            WHERE tss.league_id = $1
              AND tss.season    = $2
              AND tss.sim_batch_index >= $3
            """,
            league_id, season, since_batch_index,
        )

        # Group into most-recent (rn=1) and second-most-recent (rn=2) per team.
        latest_by_team: dict[int, dict] = {}
        prev_by_team: dict[int, dict] = {}
        for row in snapshot_rows:
            tid = row["team_id"]
            rn = row["rn"]
            if rn == 1:
                latest_by_team[tid] = dict(row)
            elif rn == 2:
                prev_by_team[tid] = dict(row)

        for tid, latest in latest_by_team.items():
            prev = prev_by_team.get(tid)
            if prev is None:
                continue
            team_code = latest["nba_team_code"]

            if latest["mode"] != prev["mode"] and prev["mode"] and latest["mode"]:
                result["mode_changes"].append({
                    "team_code": team_code,
                    "old_mode":  prev["mode"],
                    "new_mode":  latest["mode"],
                })

            if latest["plan_goal"] != prev["plan_goal"] and prev["plan_goal"] and latest["plan_goal"]:
                result["plan_pivots"].append({
                    "team_code": team_code,
                    "old_goal":  prev["plan_goal"],
                    "new_goal":  latest["plan_goal"],
                })

    except Exception as exc:
        log.warning(f"build_league_digest: snapshot diff query failed: {exc}")

    # ------------------------------------------------------------------
    # Slice 3: role_swaps
    # Query player_roles rows assigned in the window.
    # ------------------------------------------------------------------
    try:
        # Approximate "since_batch_index" using the snapshot_at timestamp for
        # the earliest snapshot in the window as the time anchor.
        anchor_ts = await pool.fetchval(
            """
            SELECT MIN(snapshot_at)
            FROM team_state_snapshots
            WHERE league_id = $1 AND season = $2 AND sim_batch_index >= $3
            """,
            league_id, season, since_batch_index,
        )

        if anchor_ts is not None:
            role_rows = await pool.fetch(
                """
                SELECT pr.team_id,
                       t.nba_team_code,
                       p.first_name || ' ' || p.last_name AS player_name,
                       pr.role         AS new_role,
                       pr.assigned_by,
                       pr.assigned_at
                FROM player_roles pr
                JOIN players p ON p.id = pr.player_id
                JOIN teams t   ON t.id = pr.team_id
                WHERE pr.league_id  = $1
                  AND pr.season     = $2
                  AND pr.assigned_at >= $3
                ORDER BY pr.assigned_at DESC
                LIMIT 20
                """,
                league_id, season, anchor_ts,
            )
            result["role_swaps"] = [
                {
                    "team_code":   r["nba_team_code"],
                    "player_name": r["player_name"],
                    "old_role":    None,  # no history table — cannot diff
                    "new_role":    r["new_role"],
                    "assigned_by": r["assigned_by"],
                }
                for r in role_rows
            ]
    except Exception as exc:
        log.warning(f"build_league_digest: role_swaps query failed: {exc}")

    # ------------------------------------------------------------------
    # Slice 4: top_signals
    # Query trades in the window; aggregate signal codes + deltas in Python.
    # ------------------------------------------------------------------
    try:
        trade_rows = await pool.fetch(
            """
            SELECT tr.id,
                   tr.context_signals,
                   t1.nba_team_code AS proposer_code,
                   t2.nba_team_code AS counterparty_code
            FROM trades tr
            JOIN teams t1 ON t1.id = tr.proposer_team_id
            JOIN teams t2 ON t2.id = tr.counterparty_team_id
            WHERE tr.league_id       = $1
              AND tr.status          = 'approved'
              AND tr.context_signals IS NOT NULL
            ORDER BY tr.id DESC
            LIMIT 50
            """,
            league_id,
        )

        signal_counts: dict[str, int] = defaultdict(int)
        signal_deltas: dict[str, float] = defaultdict(float)
        # Also track per-trade total |delta| for loud_trades.
        trade_total_abs_delta: list[tuple[int, float, str, str]] = []  # (id, Σ|δ|, proposer, counterparty)

        for tr in trade_rows:
            raw_signals = tr["context_signals"]
            if not raw_signals:
                continue

            # context_signals is JSONB — may be a dict {player_id: [signals]}
            if isinstance(raw_signals, str):
                try:
                    raw_signals = json.loads(raw_signals)
                except Exception:
                    continue

            # Flatten: each value is a list of {code, delta, reason} dicts.
            trade_abs_sum = 0.0
            headline_signal: str = ""
            headline_abs = 0.0
            if isinstance(raw_signals, dict):
                all_signals = []
                for _signals in raw_signals.values():
                    if isinstance(_signals, list):
                        all_signals.extend(_signals)
            elif isinstance(raw_signals, list):
                all_signals = raw_signals
            else:
                continue

            for sig in all_signals:
                code = sig.get("code", "")
                delta = float(sig.get("delta", 0.0))
                if code:
                    signal_counts[code] += 1
                    signal_deltas[code] += delta
                    trade_abs_sum += abs(delta)
                    if abs(delta) > headline_abs:
                        headline_abs = abs(delta)
                        headline_signal = code

            trade_total_abs_delta.append((
                tr["id"],
                round(trade_abs_sum, 4),
                tr["proposer_code"],
                tr["counterparty_code"],
                headline_signal,
            ))

        # Top signals sorted by frequency.
        top_signals = sorted(
            [
                {
                    "code":      code,
                    "count":     count,
                    "avg_delta": round(signal_deltas[code] / count, 4) if count else 0.0,
                }
                for code, count in signal_counts.items()
            ],
            key=lambda s: s["count"],
            reverse=True,
        )
        result["top_signals"] = top_signals[:8]

        # Loud trades: top-3 by Σ|δ|.
        loud = sorted(trade_total_abs_delta, key=lambda t: t[1], reverse=True)[:3]
        result["loud_trades"] = [
            {
                "trade_id":        t[0],
                "summary":         f"#{t[0]} {t[2]}↔{t[3]}  (Σ|Δ| {t[1]})",
                "headline_signal": t[4],
            }
            for t in loud
        ]

    except Exception as exc:
        log.warning(f"build_league_digest: top_signals query failed: {exc}")

    # ------------------------------------------------------------------
    # Slice 5: philosophy_outputs
    # Surface teams whose philosophy produced noteworthy role assignments.
    # ------------------------------------------------------------------
    try:
        if anchor_ts is not None:  # type: ignore[reportPossiblyUnbound]
            phil_rows = await pool.fetch(
                """
                SELECT t.id AS team_id,
                       t.nba_team_code,
                       t.coach_philosophy,
                       pr.role,
                       p.first_name || ' ' || p.last_name AS player_name,
                       p.overall,
                       p.position,
                       pr.assigned_at
                FROM player_roles pr
                JOIN players p ON p.id = pr.player_id
                JOIN teams   t ON t.id = pr.team_id
                WHERE pr.league_id     = $1
                  AND pr.season        = $2
                  AND pr.assigned_at  >= $3
                  AND t.coach_philosophy = ANY($4::text[])
                ORDER BY pr.assigned_at DESC
                LIMIT 30
                """,
                league_id, season, anchor_ts,
                list(_NOTEWORTHY_PHILOSOPHIES),
            )

            seen_teams: set[int] = set()
            for row in phil_rows:
                tid = row["team_id"]
                if tid in seen_teams:
                    continue
                seen_teams.add(tid)
                philosophy = row["coach_philosophy"] or "tendency_respecter"
                role = (row["role"] or "?").replace("_", " ")
                player_name = row["player_name"]
                ovr = row["overall"]
                pos = row["position"]
                team_code = row["nba_team_code"]

                headline = f"{team_code}/{philosophy}: {player_name} ({pos}, {ovr} OVR) tagged {role}"
                result["philosophy_outputs"].append({
                    "team_code":  team_code,
                    "philosophy": philosophy,
                    "headline":   headline,
                })
                if len(result["philosophy_outputs"]) >= 5:
                    break
    except Exception as exc:
        log.warning(f"build_league_digest: philosophy_outputs query failed: {exc}")

    return result
