"""
player_style_service.py

Shared utilities for loading pre-built tendency snapshots and generating
human-readable player style tags and summaries.

Used by:
  - bot/embeds/stats_embeds.py  (player profile embed)
  - services/batch_sim_runner.py  (columnist context enrichment)
"""
from __future__ import annotations

import json
import pathlib
import unicodedata

_TEND_CACHE_DIR = pathlib.Path(__file__).parent.parent / "data" / "tendency_cache"

_NAME_ALIASES: dict[str, str] = {
    "alex sarr":       "alexandre sarr",
    "nic claxton":     "nicolas claxton",
    "bones hyland":    "nahshon hyland",
    "bub carrington":  "carlton carrington",
}

# Module-level cache: season int → {norm_name: profile dict}
_cache: dict[int, dict[str, dict]] = {}


def _norm(name: str) -> str:
    normalized = " ".join(
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .lower()
        .replace(".", "")
        .replace("'", "")
        .strip()
        .split()
    )
    return _NAME_ALIASES.get(normalized, normalized)


def load_season_cache(season: int) -> dict[str, dict]:
    """Load (and cache) the tendency snapshot for a season. Returns {} if missing."""
    if season not in _cache:
        path = _TEND_CACHE_DIR / f"season_{season}.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    _cache[season] = json.load(f)
            except (json.JSONDecodeError, OSError):
                _cache[season] = {}
        else:
            _cache[season] = {}
    return _cache[season]


def load_profile(player_name: str, season: int) -> dict | None:
    """Return the tendency snapshot for a player, or None if not found."""
    return load_season_cache(season).get(_norm(player_name))


def style_tags(profile: dict) -> str:
    """Return a compact · -separated style description from tendency values."""
    tags: list[str] = []

    t3    = profile.get("tendency_3pt",     50)
    drv   = profile.get("tendency_drive",   50)
    pass_ = profile.get("tendency_pass",    50)
    ast   = profile.get("ast_tendency",     50)
    reb   = profile.get("reb_tendency",     50)
    usage = profile.get("usage_weight",     50)
    defd  = profile.get("defense_tendency", 50)
    clutch = profile.get("clutch_rating",   50)

    # Scoring role
    if usage >= 82:
        tags.append("Franchise Star")
    elif usage >= 68:
        tags.append("Primary Option")
    elif usage >= 52:
        tags.append("Secondary Option")
    else:
        tags.append("Role Player")

    # Shot profile
    if t3 >= 60:
        tags.append("3PT Specialist")
    elif t3 >= 40:
        tags.append("3-Point Threat")
    if drv >= 40:
        tags.append("Rim Attacker")
    elif drv < 20 and t3 < 35:
        tags.append("Mid-Range")

    # Playmaking
    if ast >= 75 or (pass_ >= 55 and ast >= 65):
        tags.append("Elite Playmaker")
    elif pass_ >= 50 and ast >= 50:
        tags.append("Pass-Oriented")
    elif pass_ <= 42:
        tags.append("Score-First")

    # Rebounding
    if reb >= 75:
        tags.append("Elite Rebounder")

    # Defense
    if defd >= 68:
        tags.append("Lockdown Defender")
    elif defd >= 55:
        tags.append("Active Defender")

    # Clutch
    if clutch >= 85:
        tags.append("Closer")

    return "  ·  ".join(tags) if tags else "Balanced"


def tendencies_block(profile: dict) -> str:
    """Compact code-block of all tendency values for Discord embeds."""
    t3   = profile.get("tendency_3pt",     50)
    drv  = profile.get("tendency_drive",   50)
    mid  = profile.get("tendency_mid",     50)
    post = profile.get("tendency_post",    0)
    tp   = profile.get("tendency_pass",    50)
    ast  = profile.get("ast_tendency",     50)
    reb  = profile.get("reb_tendency",     50)
    stl  = profile.get("stl_tendency",     50)
    blk  = profile.get("blk_tendency",     50)
    deft = profile.get("defense_tendency", 50)
    defe = profile.get("defensive_effort", 50)
    usg  = profile.get("usage_weight",     50)
    clut = profile.get("clutch_rating",    50)

    lines = [
        f"{'SHOT':<5}  3PT {t3:>3}  Drive {drv:>3}  Mid {mid:>3}  Post {post:>3}",
        f"{'PLAY':<5}  AST {ast:>3}  Pass  {tp:>3}  Reb  {reb:>3}",
        f"{'DEF':<5}  STL {stl:>3}  Blk   {blk:>3}  Def  {deft:>3}  Effort {defe:>3}",
        f"{'ROLE':<5}  Usage {usg:>3}  Clutch {clut:>3}",
    ]
    return "```\n" + "\n".join(lines) + "\n```"


def context_summary(player_name: str, season: int) -> dict | None:
    """Return a compact dict suitable for injecting into columnist context.

    Keys: style (str), role (str), shot_profile (str), playmaking (str),
          defense (str), key_tendencies (dict of int values).
    Returns None when no profile is found.
    """
    profile = load_profile(player_name, season)
    if not profile:
        return None

    tags = style_tags(profile)

    # Compact readable descriptions for the AI to reference naturally
    t3  = profile.get("tendency_3pt",   50)
    drv = profile.get("tendency_drive", 50)
    mid = profile.get("tendency_mid",   50)

    if t3 >= 55:
        shot_desc = "primarily a 3-point shooter"
    elif drv >= 38:
        shot_desc = "attacks the rim and draws fouls"
    elif mid >= 45 and t3 < 30:
        shot_desc = "mid-range and post-oriented scorer"
    else:
        shot_desc = "balanced scorer"

    ast = profile.get("ast_tendency", 50)
    pass_ = profile.get("tendency_pass", 50)
    if ast >= 75 or (pass_ >= 55 and ast >= 65):
        play_desc = "elite playmaker who creates for teammates"
    elif pass_ <= 42:
        play_desc = "score-first, low assist role"
    else:
        play_desc = "balanced playmaking and scoring"

    defd = profile.get("defense_tendency", 50)
    if defd >= 68:
        def_desc = "lockdown defender"
    elif defd >= 55:
        def_desc = "active defender"
    else:
        def_desc = "average or below-average defender"

    return {
        "style":           tags,
        "shot_profile":    shot_desc,
        "playmaking":      play_desc,
        "defense":         def_desc,
        "key_tendencies": {
            "3pt":    profile.get("tendency_3pt",     50),
            "drive":  profile.get("tendency_drive",   50),
            "pass":   profile.get("tendency_pass",    50),
            "ast":    profile.get("ast_tendency",     50),
            "usage":  profile.get("usage_weight",     50),
            "clutch": profile.get("clutch_rating",    50),
            "def":    profile.get("defense_tendency", 50),
        },
    }
