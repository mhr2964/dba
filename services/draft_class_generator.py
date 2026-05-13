from __future__ import annotations

import random

POSITIONS = ["PG", "SG", "SF", "PF", "C"]

# Realistic position distribution: more guards/wings than bigs.
_POSITION_WEIGHTS = [22, 22, 24, 18, 14]

FIRST_NAMES = [
    "Jaylen", "Marcus", "Trevion", "DeShawn", "Kobe", "Darius", "Elijah",
    "Malik", "Talen", "Scoot", "Jalen", "Brandon", "Cameron", "Jordan",
    "Isaiah", "Tyrese", "Cade", "Evan", "Keegan", "Bennedict",
    "Andre", "Terrence", "Davion", "Trayce", "Bones",
]
LAST_NAMES = [
    "Williams", "Johnson", "Thompson", "Davis", "Anderson", "Mitchell",
    "Harris", "Walker", "Robinson", "Lewis", "Jackson", "Martin",
    "Washington", "Moore", "Taylor", "Clark", "Allen", "Young", "Scott", "King",
    "Brooks", "Curry", "Durant", "Green", "White",
]

# Per-position attribute deltas, same schema as import_players.py.
_POSITION_PROFILE: dict[str, dict[str, int]] = {
    "PG": {
        "speed": 8, "playmaking": 10, "shooting_3pt": 5,
        "shooting_2pt": 2, "shooting_mid": 2,
        "finishing": -5, "defense": -2, "rebounding": -12, "iq": 5,
    },
    "SG": {
        "speed": 4, "shooting_3pt": 8, "shooting_2pt": 8, "shooting_mid": 6,
        "playmaking": 2, "finishing": 0, "defense": 0, "rebounding": -8, "iq": 2,
    },
    "SF": {
        "speed": 0, "shooting_3pt": 2, "shooting_2pt": 2, "shooting_mid": 2,
        "finishing": 2, "playmaking": 0, "defense": 2, "rebounding": 0, "iq": 0,
    },
    "PF": {
        "speed": -4, "rebounding": 8, "finishing": 6, "shooting_mid": 4,
        "shooting_3pt": -8, "shooting_2pt": 0, "defense": 4, "playmaking": -4, "iq": 0,
    },
    "C": {
        "speed": -10, "rebounding": 14, "finishing": 10, "defense": 8,
        "shooting_3pt": -18, "shooting_2pt": -4, "shooting_mid": -6,
        "playmaking": -10, "iq": 0,
    },
}

_ALL_ATTRS = [
    "speed", "shooting_2pt", "shooting_3pt", "shooting_mid",
    "finishing", "playmaking", "defense", "rebounding", "iq",
]

# OVR range per draft slot tier: (base_min, base_max)
_TIER_RANGES = [
    (5,  72, 82),   # picks 1-5: elite
    (14, 66, 75),   # picks 6-14: lottery
    (30, 60, 70),   # picks 15-30: first round second half
    (60, 55, 65),   # picks 31-60: second round
]


def _tier_for_slot(slot: int) -> tuple[int, int]:
    for cutoff, lo, hi in _TIER_RANGES:
        if slot <= cutoff:
            return lo, hi
    return 55, 65


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def _generate_attributes(overall: int, position: str) -> dict[str, int]:
    profile = _POSITION_PROFILE.get(position, _POSITION_PROFILE["SF"])
    return {
        attr: _clamp(overall + profile.get(attr, 0) + random.randint(-15, 15), 40, 99)
        for attr in _ALL_ATTRS
    }


def generate_draft_class(year: int, num_players: int = 60) -> list[dict]:
    """
    Generates synthetic draft prospects for years when real data is unavailable.
    OVR is tiered by draft slot; all attributes derive from OVR + position profile,
    matching the same logic as import_players.py so sim ratings are consistent.
    """
    used_names: set[str] = set()
    prospects: list[dict] = []

    for slot in range(1, num_players + 1):
        ovr_min, ovr_max = _tier_for_slot(slot)
        base_ovr = random.randint(ovr_min, ovr_max)
        overall = _clamp(base_ovr + random.randint(-3, 3), 40, 99)

        position = random.choices(POSITIONS, weights=_POSITION_WEIGHTS, k=1)[0]
        attrs = _generate_attributes(overall, position)

        # Unique name — retry until distinct within this class.
        for _ in range(50):
            first = random.choice(FIRST_NAMES)
            last = random.choice(LAST_NAMES)
            full = f"{first} {last}"
            if full not in used_names:
                used_names.add(full)
                break

        potential = _clamp(overall + random.randint(0, 15), 50, 99)
        peak_start = random.randint(24, 28)
        peak_end = peak_start + random.randint(4, 6)

        prospects.append({
            "external_id": None,
            "first_name": first,
            "last_name": last,
            "position": position,
            "height_in": None,
            "weight_lb": None,
            "birth_date": None,
            "years_pro": 0,
            "is_rookie": True,
            "team_id": None,
            "roster_status": "prospect",
            "overall": overall,
            **attrs,
            "potential": potential,
            "peak_age_start": peak_start,
            "peak_age_end": peak_end,
            "loyalty": random.randint(0, 100),
            "money_drive": random.randint(0, 100),
            "win_drive": random.randint(0, 100),
            "market_pref": random.choice(["big_market", "neutral", "indifferent", "neutral", "neutral"]),
            "star_leverage": (
                random.randint(80, 95) if overall >= 80
                else random.randint(50, 70) if overall >= 72
                else random.randint(10, 40)
            ),
            # Metadata for seeding — not a DB column, consumed by seed_draft_class.
            "_mock_rank": slot,
        })

    return prospects
