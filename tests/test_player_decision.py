from __future__ import annotations

import pytest

from services.player_decision import (
    Decision,
    LeagueContext,
    OfferContext,
    PlayerProfile,
    _salary_floor,
    decide,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAP = 136_000_000


def _profile(**kwargs) -> PlayerProfile:
    defaults = dict(
        overall=75,
        age=25,
        years_pro=4,
        loyalty=50,
        money_drive=50,
        win_drive=50,
        star_leverage=40,
    )
    defaults.update(kwargs)
    return PlayerProfile(**defaults)


def _league(day: int = 1, total: int = 8, best: int = 0) -> LeagueContext:
    return LeagueContext(
        salary_cap=_CAP,
        current_fa_day=day,
        total_fa_days=total,
        best_offer_this_day=best,
    )


def _offer(
    salary: int,
    years: int = 3,
    wins: int = 45,
    current: bool = False,
    cap_space: int = _CAP,
) -> OfferContext:
    return OfferContext(
        salary_per_year=salary,
        years=years,
        team_wins=wins,
        team_is_current=current,
        cap_space=cap_space,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_rookie_signs_quickly() -> None:
    """Age 19 rookie with high money_drive and low star leverage signs a good offer."""
    profile = _profile(age=19, overall=72, money_drive=80, star_leverage=15, loyalty=40)
    floor = _salary_floor(profile, _CAP)
    good_salary = int(floor * 1.05)  # well above floor

    offer = _offer(salary=good_salary, wins=38, current=False)
    league = _league(day=2, total=8, best=good_salary)

    decision, counter_sal, counter_yrs = decide(profile, offer, league, [offer])

    assert decision == Decision.SIGN
    assert counter_sal is None
    assert counter_yrs is None


def test_prime_star_counters() -> None:
    """Prime-age star with high leverage counters when a better offer exists elsewhere.

    The offer must be >= 95% of asking to reach the counter branch; the counter triggers
    when star_leverage > 70 and best_offer_this_day > offered_salary * 1.1.
    """
    profile = _profile(age=29, overall=88, star_leverage=85, win_drive=50, loyalty=40)
    floor = _salary_floor(profile, _CAP)
    # 96% of floor puts us in the >=95% band so the counter branch is reachable.
    this_salary = int(floor * 0.96)
    # A competing offer 15% higher than this one satisfies the best > salary * 1.1 condition.
    best_salary = int(this_salary * 1.15)

    offer = _offer(salary=this_salary, wins=35, current=False)
    league = _league(day=2, total=8, best=best_salary)

    decision, counter_sal, counter_yrs = decide(profile, offer, league, [offer])

    assert decision == Decision.COUNTER
    assert counter_sal is not None
    assert counter_sal <= int(_CAP * 0.35)


def test_aging_vet_takes_contender() -> None:
    """Age 34 vet with high win_drive signs with a winning team even if not current."""
    profile = _profile(age=34, overall=79, win_drive=90, loyalty=40, star_leverage=30)
    floor = _salary_floor(profile, _CAP)
    salary = int(floor * 1.0)  # exactly at floor (>= 95% guaranteed)

    offer = _offer(salary=salary, wins=55, current=False)
    league = _league(day=3, total=8, best=salary)

    decision, _, _ = decide(profile, offer, league, [offer])

    # win_drive >= 65 (after age modifier: 90+20=100 effective) and wins > 40 → SIGN
    assert decision == Decision.SIGN


def test_exit_stage_takes_minimum() -> None:
    """Age 38 player gets an offer below floor, but it's the last day — signs out of desperation."""
    profile = _profile(age=38, overall=65, win_drive=50, loyalty=40, star_leverage=20)
    floor = _salary_floor(profile, _CAP)
    below_floor = int(floor * 0.70)  # well below 85%

    offer = _offer(salary=below_floor, wins=30, current=False)
    # Last day: day == total_days - 1 → index 7 when total=8
    league = _league(day=7, total=8, best=below_floor)

    decision, _, _ = decide(profile, offer, league, [offer])

    assert decision == Decision.SIGN


def test_low_leverage_player_signs_at_floor() -> None:
    """Player with no star leverage cannot hold out — signs at floor salary."""
    profile = _profile(age=26, overall=74, star_leverage=15, loyalty=30, win_drive=30)
    floor = _salary_floor(profile, _CAP)
    at_floor = int(floor * 0.97)  # >=95% of asking

    offer = _offer(salary=at_floor, wins=40, current=False)
    league = _league(day=3, total=8, best=at_floor)

    decision, _, _ = decide(profile, offer, league, [offer])

    # star_leverage=15 < 70, so no counter branch — should SIGN
    assert decision == Decision.SIGN


def test_high_leverage_waits_for_better() -> None:
    """High-leverage player on day 2 of 8 waits when the offer is in the 85–95% band."""
    profile = _profile(age=27, overall=82, star_leverage=90, win_drive=30, loyalty=20)
    floor = _salary_floor(profile, _CAP)
    mid_offer = int(floor * 0.88)  # 85-95% band

    offer = _offer(salary=mid_offer, wins=38, current=False)
    league = _league(day=2, total=8, best=mid_offer)

    decision, _, _ = decide(profile, offer, league, [offer])

    # 85-95% band, day 2 < total-2 (6), star_leverage 90 > 40 → WAIT
    assert decision == Decision.WAIT


def test_decline_below_threshold() -> None:
    """FA8: an offer well under 85% of asking, not on the last day, is a real
    decline — not a fallback sign."""
    profile = _profile(age=25, overall=78, star_leverage=40, loyalty=40, win_drive=40)
    floor = _salary_floor(profile, _CAP)
    lowball = int(floor * 0.70)  # well under the 85% decline threshold

    offer = _offer(salary=lowball, wins=20, current=False)
    league = _league(day=1, total=8, best=lowball)  # far from the last day

    decision, counter_sal, counter_yrs = decide(profile, offer, league, [offer])

    assert decision == Decision.DECLINE
    assert counter_sal is None
    assert counter_yrs is None


def test_counter_bounded_by_offering_teams_cap_space() -> None:
    """FA5: counter_sal must never exceed the offering team's real remaining
    cap space — previously bounded only by a flat 35%-of-cap constant
    regardless of whether that specific team actually had that much room.
    """
    profile = _profile(age=29, overall=90, star_leverage=85, win_drive=50, loyalty=40)
    floor = _salary_floor(profile, _CAP)
    this_salary = int(floor * 0.96)       # >=95% of asking reaches the counter branch
    best_salary = int(this_salary * 1.2)  # competing offer > salary * 1.1

    tight_cap_space = 4_000_000  # far below both asking*1.05 and cap*0.35

    offer = _offer(salary=this_salary, wins=35, current=False, cap_space=tight_cap_space)
    league = _league(day=2, total=8, best=best_salary)

    decision, counter_sal, counter_yrs = decide(profile, offer, league, [offer])

    assert decision == Decision.COUNTER
    assert counter_sal is not None
    # The cap-space bound is the binding constraint here — well below the
    # asking*1.05 and cap*0.35 ceilings the old code would have allowed.
    assert counter_sal <= tight_cap_space
    assert counter_sal < int(this_salary * 1.05)
    assert counter_sal < int(_CAP * 0.35)


def test_salary_floor_decreases_with_age() -> None:
    """Same OVR player at 30 vs 37 — older player accepts a lower floor."""
    profile_30 = _profile(age=30, overall=78)
    profile_37 = _profile(age=37, overall=78)

    floor_30 = _salary_floor(profile_30, _CAP)
    floor_37 = _salary_floor(profile_37, _CAP)

    assert floor_37 < floor_30
    assert floor_37 == pytest.approx(floor_30 * 0.70, abs=1)
