"""
Characterization + behavior tests for services.cpu_coach_service.

Zero coverage existed for this module before this file, despite it being the
LIVE per-game gameplan decision path (sim_orchestrator._sim_single_game calls
cpu_coach_service.decide_gameplans unconditionally; the old auto_strategy.py
archetype-inference module was dead code by comparison and was deleted in
CA7 -- see docs/design/coaching-ai-logic-rules.md for the realism-audit background).
"""
from __future__ import annotations

import datetime
import random
from unittest.mock import AsyncMock

from data.repositories import game_repo, gameplan_repo
from services import cpu_coach_service as ccs
from services import team_intel

# asyncio_mode = auto (pytest.ini) already covers the async tests below --
# no blanket pytestmark, since this file also has plain sync tests for the
# pure-function helpers (_analyze_roster, _decide_strategy).


# ---------------------------------------------------------------------------
# _classify_posture — now reuses team_intel.compute_posture (B7 pattern) so
# gameplan posture and trade posture share one live-posture source instead of
# two independently-drifting heuristics (finding #5).
# ---------------------------------------------------------------------------


async def test_classify_posture_contending_mode_maps_to_contender(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "contending", "urgency": "pushing"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "contender"


async def test_classify_posture_rebuilding_mode_maps_to_tanking(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "rebuilding", "urgency": "tanking"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "tanking"


async def test_classify_posture_soft_rebuild_mode_maps_to_tanking(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "soft_rebuild", "urgency": "tanking"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "tanking"


async def test_classify_posture_play_in_fringe_maps_to_mid(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "play_in_fringe", "urgency": "pushing"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "mid"


async def test_classify_posture_developing_maps_to_mid(monkeypatch):
    async def _fake_compute_posture(pool, league, team_id):
        return {"mode": "developing", "urgency": "comfortable"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "mid"


async def test_classify_posture_respects_franchise_plan_goal_via_team_intel(monkeypatch):
    """A contend-plan team on a losing stretch should NOT be forced to 'tanking'.

    This is the exact B7-style regression finding #5 targets: previously
    _classify_posture only read win_pct/wins/pct_complete and had zero awareness
    of franchise_plan.goal. Now that it defers to team_intel.compute_posture,
    the plan_goal floor (already proven correct for trades) applies here too.
    Simulated by returning the mode team_intel would produce for a win_now
    plan floor (play_in_fringe, not rebuilding/soft_rebuild) despite a bad record.
    """
    async def _fake_compute_posture(pool, league, team_id):
        # team_intel.compute_posture would apply the plan_goal floor and return
        # play_in_fringe here instead of soft_rebuild/rebuilding.
        return {"mode": "play_in_fringe", "urgency": "desperate"}

    monkeypatch.setattr(team_intel, "compute_posture", _fake_compute_posture)

    result = await ccs._classify_posture(AsyncMock(), league_id=1, season=2025, team_id=1)
    assert result == "mid"
    assert result != "tanking"


# ---------------------------------------------------------------------------
# _analyze_roster — avg_speed / avg_defense / avg_defensive_effort computed
# for the finding #1 personnel gate.
# ---------------------------------------------------------------------------


def test_analyze_roster_computes_avg_speed_and_defense():
    players = [
        {"overall": 80, "speed": 90, "defense": 70, "defensive_effort": 60},
        {"overall": 80, "speed": 70, "defense": 50, "defensive_effort": 40},
    ]
    analysis = ccs._analyze_roster(players)
    assert analysis["avg_speed"] == 80.0
    assert analysis["avg_defense"] == 60.0
    assert analysis["avg_defensive_effort"] == 50.0


def test_analyze_roster_empty_returns_neutral_defaults():
    analysis = ccs._analyze_roster([])
    assert analysis["avg_speed"] == 50.0
    assert analysis["avg_defense"] == 50.0
    assert analysis["avg_defensive_effort"] == 50.0


# ---------------------------------------------------------------------------
# _decide_strategy — finding #1 personnel gate: press/switch_all must be
# pruned from the weighted defense_options entirely when the team's own
# roster can't execute them, not merely picked and left to hurt the team.
# ---------------------------------------------------------------------------


def _self_analysis(**overrides) -> dict:
    base = {
        "avg_ovr": 75.0, "has_elite_passer": False, "has_elite_scorer": False,
        "has_elite_big": False, "has_isolation_star": False, "three_point_count": 0,
        "elite_passer_name": None, "isolation_star_name": None, "elite_big_name": None,
        "elite_scorer_name": None,
        "avg_speed": 50.0, "avg_defense": 50.0, "avg_defensive_effort": 50.0,
    }
    base.update(overrides)
    return base


def _opp_analysis(**overrides) -> dict:
    base = {
        "avg_ovr": 90.0, "has_elite_passer": False, "has_elite_scorer": False,
        "has_elite_big": False, "has_isolation_star": False, "three_point_count": 0,
        "elite_passer_name": None, "isolation_star_name": None, "elite_big_name": None,
        "elite_scorer_name": None,
    }
    base.update(overrides)
    return base


_SEEDS = range(200)


def test_decide_strategy_slow_team_never_presses():
    """avg_speed=50 (league-average) is below the press gate -- press should
    never be selected even in the branch that would normally offer it
    (opponent OVR > 82)."""
    self_a = _self_analysis(avg_speed=50.0)
    opp_a = _opp_analysis(avg_ovr=90.0)
    for seed in _SEEDS:
        strategy, _ = ccs._decide_strategy(
            self_a, opp_a, "mid", False, 0, 0, random.Random(seed)
        )
        assert strategy["defensive_scheme"] != "press"


def test_decide_strategy_fast_team_can_press():
    """A genuinely fast roster (avg_speed >= 74) should still be able to press
    in the same branch."""
    self_a = _self_analysis(avg_speed=80.0)
    opp_a = _opp_analysis(avg_ovr=90.0)
    presses = sum(
        1 for seed in _SEEDS
        if ccs._decide_strategy(self_a, opp_a, "mid", False, 0, 0, random.Random(seed))[0]["defensive_scheme"] == "press"
    )
    assert presses > 0


def test_decide_strategy_poor_defense_team_never_switches_all():
    """avg_defense/avg_defensive_effort at league-average is below the
    switch_all gate -- switch_all should never fire even against an
    isolation-star opponent (the branch that would normally favor it)."""
    self_a = _self_analysis(avg_defense=50.0, avg_defensive_effort=50.0)
    opp_a = _opp_analysis(has_isolation_star=True, isolation_star_name="Star")
    for seed in _SEEDS:
        strategy, _ = ccs._decide_strategy(
            self_a, opp_a, "mid", False, 0, 0, random.Random(seed)
        )
        assert strategy["defensive_scheme"] != "switch_all"


def test_decide_strategy_elite_defense_team_can_switch_all():
    """A genuinely versatile, high-defense roster (avg_defense >= 74,
    avg_defensive_effort >= 60) should still be able to switch_all."""
    self_a = _self_analysis(avg_defense=85.0, avg_defensive_effort=75.0)
    opp_a = _opp_analysis(has_isolation_star=True, isolation_star_name="Star")
    switches = sum(
        1 for seed in _SEEDS
        if ccs._decide_strategy(self_a, opp_a, "mid", False, 0, 0, random.Random(seed))[0]["defensive_scheme"] == "switch_all"
    )
    assert switches > 0


def test_decide_strategy_always_returns_a_valid_defensive_scheme():
    """Pruning press/switch_all from defense_options must never leave the
    weighted-choice list empty -- every branch keeps a non-gated fallback
    (man_to_man or zone)."""
    self_a = _self_analysis(avg_speed=30.0, avg_defense=30.0, avg_defensive_effort=30.0)
    for opp_kwargs, posture in [
        ({"has_isolation_star": True, "isolation_star_name": "Star"}, "mid"),
        ({"avg_ovr": 90.0}, "mid"),
        ({}, "contender"),
        ({}, "mid"),
    ]:
        opp_a = _opp_analysis(**opp_kwargs)
        for seed in range(20):
            strategy, _ = ccs._decide_strategy(
                self_a, opp_a, posture, False, 0, 0, random.Random(seed)
            )
            assert strategy["defensive_scheme"] in ("man_to_man", "zone", "press", "switch_all")


# ---------------------------------------------------------------------------
# CA5 (coaching AI realism sweep) -- scheme stickiness. get_scheme_history's
# {"scheme": ..., "games": ..., "wins": ..., "losses": ...} shape (per
# gameplan_repo.get_scheme_history) feeds a flat weight bonus for a team's
# historically-favored scheme, only when that scheme is still on offer.
# ---------------------------------------------------------------------------


def test_apply_scheme_history_bonus_bumps_matching_option():
    options = [("three_heavy", 2), ("balanced", 1)]
    result = ccs._apply_scheme_history_bonus(options, "balanced")
    assert dict(result)["balanced"] == 1 + ccs._SCHEME_HISTORY_BONUS
    assert dict(result)["three_heavy"] == 2


def test_apply_scheme_history_bonus_no_op_when_scheme_not_offered():
    """A historically-favored scheme the roster/opponent context no longer
    offers this game (e.g. pruned by the personnel gate) gets no bonus --
    the options list is returned completely unchanged, not appended to."""
    options = [("man_to_man", 3), ("zone", 1)]
    result = ccs._apply_scheme_history_bonus(options, "switch_all")
    assert result == options


def test_apply_scheme_history_bonus_no_op_when_no_history():
    options = [("isolation", 3), ("inside_out", 1)]
    assert ccs._apply_scheme_history_bonus(options, None) == options


def test_decide_strategy_scheme_history_shifts_offensive_scheme_frequency():
    """A team with no standout roster signal (falls to the balanced/ball_movement
    default scheme_options) should pick its historically-favored scheme more
    often than a team with no history, averaged over many seeds."""
    self_a = _self_analysis()  # no elite passer/scorer/big/iso -> falls to default branch
    opp_a = _opp_analysis()

    def _frequency(history: dict | None) -> float:
        picks = [
            ccs._decide_strategy(self_a, opp_a, "mid", False, 0, 0, random.Random(seed), scheme_history=history)[0]["offensive_scheme"]
            for seed in _SEEDS
        ]
        return sum(1 for p in picks if p == "ball_movement") / len(picks)

    freq_no_history = _frequency(None)
    freq_with_history = _frequency({"offensive_scheme": {"scheme": "ball_movement", "games": 10, "wins": 6, "losses": 4}})

    assert freq_with_history > freq_no_history, (
        f"ball_movement should be picked more often with matching scheme history "
        f"({freq_with_history:.2f}) than without ({freq_no_history:.2f})"
    )


async def test_compute_cpu_gameplan_reads_real_scheme_history_from_db(db_pool):
    """CA5 real-DB integration test (standard, not a mandatory smoke test --
    this is a new signal being added, not a previously-dead path). Seeds 6
    real, simmed games with real game_cpu_gameplans rows (written via
    gameplan_repo.record_gameplan, the actual production write path) all
    running "ball_movement" for one team. Calls the real, unmocked
    _compute_cpu_gameplan against the real DB across many trials and checks
    the empirical offensive_scheme frequency shifts toward that real history
    compared to a team with no games recorded at all."""
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES (999301, 'CA5 Scheme Stickiness League', 2025, 2025, 'REGULAR_SEASON_ACTIVE', 12345)
        RETURNING id
        """
    )
    league_id: int = league_row["id"]
    season = 2025

    async def _seed_team(code: str) -> int:
        return await db_pool.fetchval(
            """
            INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
            VALUES ($1, $2, $3, $3, 'East', 'Atlantic') RETURNING id
            """,
            league_id, code, f"{code} City",
        )

    team_with_history_id = await _seed_team("HST")
    team_no_history_id = await _seed_team("NOH")
    opp_team_id = await _seed_team("OPP")

    for i in range(6):
        game_id = await game_repo.insert_game(db_pool, {
            "league_id": league_id, "season": season, "season_type": "regular",
            "game_index": i, "home_team_id": team_with_history_id, "away_team_id": opp_team_id,
            "scheduled_date": datetime.date(2025, 10, 1) + datetime.timedelta(days=i),
            "status": "simmed", "is_user_matchup": False, "rng_seed": i,
        })
        await gameplan_repo.record_gameplan(db_pool, game_id, team_with_history_id, {
            "source": "cpu",
            "strategy": {
                "offensive_pace": "balanced", "offensive_scheme": "ball_movement",
                "defensive_scheme": "man_to_man", "defensive_intensity": "normal", "star_usage": 50,
            },
            "player_directives": {},
            "rationale": "",
        })

    # Bland roster (no standout signal) so _decide_strategy falls to the
    # default scheme_options = [("balanced", 3), ("ball_movement", 1)] branch.
    def _bland_players(team_id: int) -> list[dict]:
        return [
            {"id": 10_000 + team_id * 100 + i, "overall": 75, "position": "SF",
             "playmaking": 60, "finishing": 70, "shooting_3pt": 60, "speed": 60,
             "defense": 60, "defensive_effort": 50}
            for i in range(8)
        ]

    opp_players = _bland_players(opp_team_id)

    async def _pick_offense(team_id: int, n_trials: int = 60) -> float:
        picks = []
        for i in range(n_trials):
            game_row = {
                "id": 900_000 + team_id * 1000 + i,
                "home_team_id": team_id,
                "away_team_id": opp_team_id,
                "scheduled_date": datetime.date(2025, 11, 1) + datetime.timedelta(days=i),
            }
            gameplan = await ccs._compute_cpu_gameplan(
                db_pool, league_id, season, game_row, team_id, _bland_players(team_id), opp_players,
            )
            picks.append(gameplan["strategy"]["offensive_scheme"])
        return sum(1 for p in picks if p == "ball_movement") / len(picks)

    freq_with_history = await _pick_offense(team_with_history_id)
    freq_no_history = await _pick_offense(team_no_history_id)

    assert freq_with_history > freq_no_history, (
        f"Team with real DB-persisted ball_movement history should pick it more often "
        f"({freq_with_history:.2f}) than a team with zero recorded games ({freq_no_history:.2f})"
    )
