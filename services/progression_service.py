from __future__ import annotations

import datetime
import random
from typing import Optional

import asyncpg

from core.logging import get_logger
from data.db import get_pool
from data.repositories import player_repo

log = get_logger(__name__)

RATING_ATTRIBUTES = [
    "overall",
    "speed",
    "shooting_2pt",
    "shooting_3pt",
    "shooting_mid",
    "finishing",
    "playmaking",
    "defense",
    "rebounding",
    "iq",
]

# Attributes that decline first for athletic positions; iq is listed last so it
# declines slowest.
_ATHLETIC_ATTRS = ["speed", "finishing"]
_LATE_DECLINE_ATTRS = ["iq"]

# Positions where athleticism erodes faster post-peak.
_ATHLETIC_POSITIONS = {"PG", "SG", "SF"}

# Positions where decline is slower (big men rely less on burst athleticism).
_BIG_POSITIONS = {"PF", "C"}

# Age boundary that triggers steeper decline regardless of peak window.
_STEEP_DECLINE_AGE = 38


def _compute_age(birth_date: Optional[datetime.date], years_pro: int) -> int:
    """
    Return player age. If birth_date is present use it (relative to today);
    otherwise approximate from years_pro assuming debut at 20.
    """
    if birth_date is not None:
        today = datetime.date.today()
        age = today.year - birth_date.year
        if (today.month, today.day) < (birth_date.month, birth_date.day):
            age -= 1
        return age
    return 20 + years_pro


def _potential_growth_weight(potential: int) -> float:
    """Map potential (40-99) to a growth multiplier (0.4 – 1.0)."""
    return 0.4 + 0.6 * (potential - 40) / 59


async def _has_season_ending_injury(
    pool: asyncpg.Pool, league_id: int, player_id: int, season: int
) -> bool:
    row = await pool.fetchrow(
        """
        SELECT 1 FROM injuries
        WHERE league_id = $1
          AND player_id = $2
          AND severity = 'season_ending'
          AND season = $3
        LIMIT 1
        """,
        league_id,
        player_id,
        season,
    )
    return row is not None


async def _avg_minutes(
    pool: asyncpg.Pool, league_id: int, player_id: int, season: int
) -> tuple[float, int]:
    """
    Return (avg_minutes, games_played) for this player this season.
    Uses game_box_scores joined to games for the season filter.
    """
    row = await pool.fetchrow(
        """
        SELECT
            AVG(b.minutes)   AS avg_min,
            COUNT(b.id)      AS games
        FROM game_box_scores b
        JOIN games g ON g.id = b.game_id
        WHERE g.league_id = $1
          AND g.season     = $2
          AND b.player_id  = $3
          AND g.status     = 'completed'
        """,
        league_id,
        season,
        player_id,
    )
    if row is None or row["games"] == 0:
        return 0.0, 0
    return float(row["avg_min"] or 0.0), int(row["games"])


def _clamp(value: int, floor: int = 40, ceiling: int = 99) -> int:
    return max(floor, min(ceiling, value))


def _build_deltas_growth(
    player: player_repo.Player,
    potential_weight: float,
    low_minutes: bool,
) -> dict[str, int]:
    """Return per-attribute deltas for pre-peak players."""
    deltas: dict[str, int] = {}

    # overall: +1 to +3, skewed by potential
    overall_max = 1 + round(2 * potential_weight)  # 1, 2, or 3
    deltas["overall"] = random.randint(1, max(1, overall_max))

    for attr in RATING_ATTRIBUTES:
        if attr == "overall":
            continue
        deltas[attr] = random.randint(0, 2)

    if low_minutes:
        for attr in deltas:
            deltas[attr] = deltas[attr] // 2

    return deltas


def _build_deltas_stable(player: player_repo.Player) -> dict[str, int]:
    deltas: dict[str, int] = {}
    # overall: biased toward 0 (weights: -1=1, 0=2, +1=1)
    deltas["overall"] = random.choices([-1, 0, 1], weights=[1, 2, 1])[0]
    for attr in RATING_ATTRIBUTES:
        if attr == "overall":
            continue
        deltas[attr] = random.randint(-1, 1)
    return deltas


def _build_deltas_decline(player: player_repo.Player, age: int) -> dict[str, int]:
    deltas: dict[str, int] = {}

    if age >= _STEEP_DECLINE_AGE:
        overall_drop = random.randint(2, 4)
    else:
        overall_drop = random.randint(1, 3)
    deltas["overall"] = -overall_drop

    position = player.position.upper()

    for attr in RATING_ATTRIBUTES:
        if attr == "overall":
            continue

        # iq is last to go — only mild decline
        if attr in _LATE_DECLINE_ATTRS:
            deltas[attr] = random.choices([-1, 0], weights=[1, 3])[0]
            continue

        # Athletic positions lose speed/finishing faster
        if attr in _ATHLETIC_ATTRS and position in _ATHLETIC_POSITIONS:
            deltas[attr] = random.randint(-2, -1)
            continue

        # Big men decline more slowly in athletic attrs
        if attr in _ATHLETIC_ATTRS and position in _BIG_POSITIONS:
            deltas[attr] = random.choices([-1, 0], weights=[1, 2])[0]
            continue

        deltas[attr] = random.randint(-1, 0)

    return deltas


async def _process_player(
    pool: asyncpg.Pool,
    player: player_repo.Player,
    season: int,
    league_id: int,
) -> None:
    age = _compute_age(player.birth_date, player.years_pro)
    potential_weight = _potential_growth_weight(player.potential)

    # --- career stage ---
    if age < player.peak_age_start:
        stage = "growth"
    elif age <= player.peak_age_end:
        stage = "stable"
    else:
        stage = "decline"

    # --- low minutes check ---
    avg_min, games_played = await _avg_minutes(pool, league_id, player.id, season)
    low_minutes = games_played > 10 and avg_min < 15.0

    # --- build base deltas ---
    if stage == "growth":
        deltas = _build_deltas_growth(player, potential_weight, low_minutes)
        base_reason = "pre_peak_growth"
    elif stage == "stable":
        deltas = _build_deltas_stable(player)
        base_reason = "peak_stable"
    else:
        deltas = _build_deltas_decline(player, age)
        base_reason = "post_peak_decline"

    log_entries: list[tuple[str, int, int, str]] = []

    # --- apply base deltas, collect entries that changed ---
    attr_values: dict[str, int] = {attr: getattr(player, attr) for attr in RATING_ATTRIBUTES}
    for attr, delta in deltas.items():
        if delta == 0:
            continue
        before = attr_values[attr]
        after = _clamp(before + delta)
        if after != before:
            attr_values[attr] = after
            log_entries.append((attr, before, after, base_reason))

    # --- breakout check (growth phase only) ---
    if stage == "growth" and player.years_pro <= 3 and random.random() < 0.08:
        breakout_overall_delta = random.randint(2, 4)
        before_ovr = attr_values["overall"]
        attr_values["overall"] = _clamp(before_ovr + breakout_overall_delta)
        if attr_values["overall"] != before_ovr:
            log_entries.append(("overall", before_ovr, attr_values["overall"], "breakout"))

        breakout_attrs = random.sample(
            [a for a in RATING_ATTRIBUTES if a != "overall"], k=2
        )
        for attr in breakout_attrs:
            before = attr_values[attr]
            after = _clamp(before + random.randint(2, 4))
            if after != before:
                attr_values[attr] = after
                log_entries.append((attr, before, after, "breakout"))

    # --- injury setback ---
    if await _has_season_ending_injury(pool, league_id, player.id, season):
        before_ovr = attr_values["overall"]
        injury_drop = random.randint(2, 4)
        attr_values["overall"] = _clamp(before_ovr - injury_drop)
        if attr_values["overall"] != before_ovr:
            log_entries.append(("overall", before_ovr, attr_values["overall"], "injury_setback"))

        physical_attrs = [
            a for a in RATING_ATTRIBUTES
            if a not in ("overall", "iq", "playmaking", "shooting_2pt", "shooting_3pt", "shooting_mid")
        ]
        for attr in random.sample(physical_attrs, k=min(2, len(physical_attrs))):
            before = attr_values[attr]
            after = _clamp(before - random.randint(1, 3))
            if after != before:
                attr_values[attr] = after
                log_entries.append((attr, before, after, "injury_setback"))

    # --- low minutes log entry (if halving caused meaningful difference) ---
    # The halving already happened in _build_deltas_growth; we record it only
    # if there were any non-zero deltas and the player qualified.
    if low_minutes and stage == "growth" and log_entries:
        # Tag existing entries as low_minutes — replace reason on base growth entries
        log_entries = [
            (attr, bv, av, "low_minutes" if reason == "pre_peak_growth" else reason)
            for attr, bv, av, reason in log_entries
        ]

    # --- persist attribute changes ---
    await pool.execute(
        """
        UPDATE players
        SET overall        = $1,
            speed          = $2,
            shooting_2pt   = $3,
            shooting_3pt   = $4,
            shooting_mid   = $5,
            finishing      = $6,
            playmaking     = $7,
            defense        = $8,
            rebounding     = $9,
            iq             = $10
        WHERE id = $11
        """,
        attr_values["overall"],
        attr_values["speed"],
        attr_values["shooting_2pt"],
        attr_values["shooting_3pt"],
        attr_values["shooting_mid"],
        attr_values["finishing"],
        attr_values["playmaking"],
        attr_values["defense"],
        attr_values["rebounding"],
        attr_values["iq"],
        player.id,
    )

    # --- insert progression_log rows ---
    if log_entries:
        await pool.executemany(
            """
            INSERT INTO progression_log
                (league_id, season_completed, player_id, attribute, before_value, after_value, reason)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            [
                (league_id, season, player.id, attr, bv, av, reason)
                for attr, bv, av, reason in log_entries
            ],
        )

    # --- increment years_pro ---
    await pool.execute(
        "UPDATE players SET years_pro = years_pro + 1 WHERE id = $1",
        player.id,
    )


async def run_progression(league_id: int, season: int) -> int:
    """
    Run end-of-season progression for all active players in this league.
    Returns the count of players processed.
    Called by rollover service after the final phase of the offseason.
    """
    pool = await get_pool()

    rows = await pool.fetch(
        """
        SELECT * FROM players
        WHERE league_id = $1 AND roster_status = 'active'
        """,
        league_id,
    )

    players = [player_repo._player_from_record(r) for r in rows]

    for player in players:
        try:
            await _process_player(pool, player, season, league_id)
        except Exception:
            log.exception(
                "Progression failed for player %d (league %d, season %d)",
                player.id,
                league_id,
                season,
            )
            raise

    log.info(
        "Progression complete: %d players processed (league %d, season %d)",
        len(players),
        league_id,
        season,
    )
    return len(players)
