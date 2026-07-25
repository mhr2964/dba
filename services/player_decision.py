from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    SIGN = "sign"
    DECLINE = "decline"
    WAIT = "wait"
    COUNTER = "counter"


# Mirrors fa_service._MIN_SALARY. Duplicated rather than imported to avoid a
# circular import (fa_service already imports from this module).
_MIN_SALARY = 1_100_000


@dataclass
class PlayerProfile:
    overall: int
    age: int
    years_pro: int
    loyalty: int        # 0-100: higher = more likely to re-sign at discount
    money_drive: int    # 0-100
    win_drive: int      # 0-100
    star_leverage: int  # 0-100: determines ability to counter/wait
    # FA4: 'big_market' | 'neutral' | 'indifferent' — small asking-price nudge.
    # Defaults to 'neutral' so existing PlayerProfile() call sites don't break.
    market_pref: str = "neutral"


@dataclass
class OfferContext:
    salary_per_year: int
    years: int
    team_wins: int
    team_is_current: bool
    # FA5: offering team's real remaining cap space at the moment this offer
    # context was built. Bounds the COUNTER branch's counter_sal so a team
    # with little room can't get counter-demanded into a salary it can't pay.
    cap_space: int


@dataclass
class LeagueContext:
    salary_cap: int
    current_fa_day: int
    total_fa_days: int
    best_offer_this_day: int  # highest salary offered to this player this day


def decide(
    profile: PlayerProfile,
    offer: OfferContext,
    league: LeagueContext,
    all_offers_this_day: list[OfferContext],
) -> tuple[Decision, int | None, int | None]:
    """
    Returns (decision, counter_salary, counter_years).
    counter values are None unless decision=COUNTER.
    """
    effective = _apply_career_stage(profile)
    asking = _salary_floor(profile, league.salary_cap)
    day = league.current_fa_day
    total = league.total_fa_days
    salary = offer.salary_per_year

    is_last_day = day >= total - 1

    if salary >= asking * 0.95:
        if effective["loyalty"] > 65 and offer.team_is_current:
            return Decision.SIGN, None, None

        if effective["win_drive"] > 65 and offer.team_wins > 40:
            return Decision.SIGN, None, None

        if is_last_day:
            return Decision.SIGN, None, None

        if (
            effective["star_leverage"] > 70
            and league.best_offer_this_day > salary * 1.1
        ):
            # FA5: bound by the offering team's actual remaining cap space, not
            # just a flat 35%-of-cap ceiling — a team with little room left
            # shouldn't get counter-demanded into a salary it can't legally pay.
            counter_sal = min(
                int(asking * 1.05),
                int(league.salary_cap * 0.35),
                max(offer.cap_space, _MIN_SALARY),
            )
            return Decision.COUNTER, counter_sal, offer.years

        return Decision.SIGN, None, None

    if salary < asking * 0.85:
        if is_last_day:
            return Decision.SIGN, None, None
        return Decision.DECLINE, None, None

    # Between 85–95% of asking.
    if day < total - 2 and effective["star_leverage"] > 40:
        return Decision.WAIT, None, None
    return Decision.SIGN, None, None


def _apply_career_stage(profile: PlayerProfile) -> dict:
    """Returns adjusted attribute dict without mutating the profile."""
    money_drive = profile.money_drive
    loyalty = profile.loyalty
    win_drive = profile.win_drive
    star_leverage = profile.star_leverage

    age = profile.age
    if 18 <= age <= 23:
        money_drive = min(100, money_drive + 20)
        loyalty = max(0, loyalty - 10)
        star_leverage = min(star_leverage, 20)
    elif 28 <= age <= 31:
        win_drive = min(100, win_drive + 10)
    elif 32 <= age <= 35:
        win_drive = min(100, win_drive + 20)
        loyalty = min(100, loyalty + 15)
        money_drive = max(0, money_drive - 20)
    elif age >= 36:
        win_drive = min(100, win_drive + 30)
        loyalty = min(100, loyalty + 20)
        money_drive = max(0, money_drive - 40)
        star_leverage = min(star_leverage, 30)

    return {
        "money_drive": money_drive,
        "loyalty": loyalty,
        "win_drive": win_drive,
        "star_leverage": star_leverage,
    }


def _salary_floor(profile: PlayerProfile, salary_cap: int) -> int:
    """Minimum acceptable salary based on OVR and age."""
    ovr = profile.overall
    if ovr >= 90:
        floor = int(salary_cap * 0.28)
    elif ovr >= 80:
        floor = int(salary_cap * 0.18)
    elif ovr >= 70:
        floor = int(salary_cap * 0.10)
    elif ovr >= 60:
        floor = int(salary_cap * 0.05)
    else:
        floor = 1_100_000

    if profile.age >= 35:
        floor = int(floor * 0.70)

    # FA4: market_pref nudges asking price a small amount. There is no team
    # market-size attribute in the schema (out of scope — see
    # docs/design/fa-logic-rules.md's deferred items), so this reflects only
    # the player's own generated preference, not a real team/market fit.
    if profile.market_pref == "indifferent":
        floor = int(floor * 0.97)
    elif profile.market_pref == "big_market":
        floor = int(floor * 1.03)

    return floor
