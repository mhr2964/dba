"""
Unit tests for _apply_final_trade_gates (B8 gate parity).

Verifies that the extracted gate helper:
1. Approves a clean, balanced trade.
2. Rejects a contender that ships a pick on a lateral wing swap (DEN/HOU case).
3. Rejects a contender 2-for-1 without upgrade (NYK/GSW case).
4. Reports rejection from _attempt_outgoing_first_offer when all candidates fail gates
   (gated via mock to confirm no proposal reaches trade_service.propose).

Tests 1-3 call _apply_final_trade_gates directly with mock pools.
Test 4 patches _apply_final_trade_gates inside _attempt_outgoing_first_offer.
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch


import services.cpu_trade_proposal_runner as _mod
from data.repositories import league_repo, player_repo, team_repo
from services.trade_gates import _apply_final_trade_gates

SALARY_CAP = 140_000_000
LEAGUE_ID = 1
SEASON = 2025


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------


class _FakeTeam:
    def __init__(self, team_id: int, cpu_mode: str = "contending"):
        self.id = team_id
        self.cpu_mode = cpu_mode
        self.nba_team_code = f"T{team_id}"


class _FakePlayer:
    def __init__(
        self,
        player_id: int,
        overall: int = 76,
        position: str = "SF",
        age: int = 28,
        **kwargs,
    ):
        self.id = player_id
        self.overall = overall
        self.position = position
        self.age = age
        self.birth_date = None
        self.first_name = "Test"
        self.last_name = f"Player{player_id}"
        self.team_id = 1
        # Tendency defaults — neutral archetype so B6 doesn't accidentally block.
        self.tendency_3pt = kwargs.get("tendency_3pt", 50)
        self.tendency_drive = kwargs.get("tendency_drive", 50)
        self.tendency_pass = kwargs.get("tendency_pass", 50)
        self.ast_tendency = kwargs.get("ast_tendency", 50)
        self.reb_tendency = kwargs.get("reb_tendency", 50)
        self.blk_tendency = kwargs.get("blk_tendency", 50)
        self.stl_tendency = kwargs.get("stl_tendency", 50)
        self.defense_tendency = kwargs.get("defense_tendency", 50)


class _FakeContract:
    def __init__(self, salary: int = 10_000_000, years_remaining: int = 2):
        self.salary = salary
        self.years_remaining = years_remaining


def _make_league() -> league_repo.League:
    return league_repo.League(
        id=LEAGUE_ID,
        discord_guild_id=999,
        name="Test",
        start_season_year=SEASON,
        current_season=SEASON,
        current_phase="trade_deadline",
        phase_data={},
        commissioner_user_id=1,
        fa_day_count=0,
        salary_cap=SALARY_CAP,
        created_at=datetime.datetime(2025, 1, 1),
        archived_at=None,
    )


def _make_pool(
    players: dict[int, _FakePlayer] | None = None,
    contracts: dict[int, _FakeContract] | None = None,
    picks: dict[int, dict] | None = None,
) -> MagicMock:
    """Build a pool mock that returns the provided player/contract/pick data."""
    players = players or {}
    contracts = contracts or {}
    picks = picks or {}

    pool = MagicMock()

    async def _get_by_id(pool_, pid):
        return players.get(pid)

    async def _get_active_contract(pool_, pid):
        return contracts.get(pid, _FakeContract())

    async def _fetchrow(sql, *args):
        # For "SELECT season, round FROM draft_picks WHERE id = $1"
        if args and isinstance(args[0], int):
            return picks.get(args[0])
        return None

    pool.fetchrow = _fetchrow
    return pool, _get_by_id, _get_active_contract


def _default_postures(team_a_id: int, mode_a: str = "contending") -> dict:
    return {
        team_a_id: {
            "mode": mode_a,
            "urgency": "comfortable",
            "avg_age": 27.0,
            "projected_wins": 48,
            "conf_rank": 4,
        }
    }


# ---------------------------------------------------------------------------
# Test 1: Approved trade returns (True, "")
# ---------------------------------------------------------------------------


async def test_approved_trade_returns_true():
    """A clean, balanced, posture-compatible trade passes all gates.

    Contending team A gives one OVR-79 player, receives one OVR-82 player.
    No picks sent. Net OVR gain = +3. Ratio = 1.0 (balanced).
    incoming OVR 82 >= contending min_ovr 79 (comfortable urgency) so B1 passes.
    All gates must pass → (True, "").
    """
    team_a = _FakeTeam(1, cpu_mode="contending")
    team_b = _FakeTeam(2)
    league = _make_league()

    outgoing_p = _FakePlayer(10, overall=79, position="SG")
    incoming_p = _FakePlayer(20, overall=82, position="SG")
    roster_a = [_FakePlayer(i, overall=75) for i in range(3, 14)] + [outgoing_p]

    outgoing_contract = _FakeContract(salary=14_000_000)
    incoming_contract = _FakeContract(salary=16_000_000)

    pool, _get_by_id, _get_active_contract = _make_pool(
        players={10: outgoing_p, 20: incoming_p},
        contracts={10: outgoing_contract, 20: incoming_contract},
    )

    with (
        patch.object(_mod.player_repo, "get_by_id", _get_by_id),
        patch.object(_mod.player_repo, "get_active_contract", _get_active_contract),
    ):
        ok, reason = await _apply_final_trade_gates(
            pool=pool,
            league=league,
            team_a=team_a,
            team_b=team_b,
            outgoing_pids_a=[10],
            incoming_pids_a=[20],
            outgoing_picks_a=[],
            incoming_picks_a=[],
            package_value=48.0,     # ratio = 1.0, above the 0.85 floor
            target_value=48.0,
            posture_a="contending",
            cp_contexts={1: {"current_payroll": int(SALARY_CAP * 0.85)}},
            cp_r1_counts={},
            roster_a=roster_a,
            postures=_default_postures(1, "contending"),
        )

    assert ok is True, f"Expected gates to approve clean trade; got reason={reason}"
    assert reason == "", f"Expected empty reason on approval; got: {reason}"


# ---------------------------------------------------------------------------
# Test 2: Contender picks + lateral swap → rejected (DEN/HOU case)
# ---------------------------------------------------------------------------


async def test_contender_lateral_swap_with_pick_rejected():
    """DEN/HOU: DEN ships Gordon-like (OVR 80) + 2nd-round pick, receives Brooks-like (OVR 80).

    Net OVR = 0 (lateral). DEN sends a pick on a lateral swap — gate must reject.
    Using OVR 80 so B1 passes (contending min_ovr=79); rejection comes from B5-sub1.

    The B5/cpu_should_accept layer fires on contending mode because:
      - contending teams don't give picks without receiving players (giving_picks guard)
      - OR the B5-sub1 pick-parity rule fires
    Either way the gate must return (False, ...) for this scenario.
    """
    team_a = _FakeTeam(1, cpu_mode="contending")
    team_b = _FakeTeam(2)
    league = _make_league()

    gordon = _FakePlayer(10, overall=80, position="PF")
    brooks = _FakePlayer(20, overall=80, position="SF")
    roster_a = [_FakePlayer(i, overall=75) for i in range(3, 14)] + [gordon]
    pick_row = {"season": 2027, "round": 2}

    pool, _get_by_id, _get_active_contract = _make_pool(
        players={10: gordon, 20: brooks},
        contracts={10: _FakeContract(salary=12_000_000), 20: _FakeContract(salary=12_000_000)},
        picks={99: pick_row},
    )

    with (
        patch.object(_mod.player_repo, "get_by_id", _get_by_id),
        patch.object(_mod.player_repo, "get_active_contract", _get_active_contract),
    ):
        ok, reason = await _apply_final_trade_gates(
            pool=pool,
            league=league,
            team_a=team_a,
            team_b=team_b,
            outgoing_pids_a=[10],
            incoming_pids_a=[20],
            outgoing_picks_a=[99],   # pick sent by DEN
            incoming_picks_a=[],
            package_value=48.0,     # close to target (passes sanity floor)
            target_value=48.0,
            posture_a="contending",
            cp_contexts={1: {"current_payroll": int(SALARY_CAP * 0.85)}},
            cp_r1_counts={},
            roster_a=roster_a,
            postures=_default_postures(1, "contending"),
        )

    assert ok is False, "Gate should reject contender lateral swap with lost pick; got ok=True"
    # The contending cpu_should_accept guard fires: "declines — contending teams protect their
    # future draft capital" when giving picks without receiving players; or B5-sub1 fires.
    # Either way a B5-family gate name is present.
    assert "B5" in reason, f"Expected B5-family rejection; got: {reason}"


# ---------------------------------------------------------------------------
# Test 3: Contender 2-for-1 without upgrade → rejected (NYK case)
# ---------------------------------------------------------------------------


async def test_contender_2for1_without_upgrade_rejected():
    """NYK/GSW: NYK ships Bridges-like (80) + Anunoby-like (82) for Kuminga-like (80).

    2 starters out (max OVR 82). 1 player in (OVR 80 — not > 82, not a strict upgrade).
    Using OVR 80 for Bridges so B1 passes. B5-sub2 must fire.
    Gate must return (False, ...).
    """
    team_a = _FakeTeam(1, cpu_mode="contending")
    team_b = _FakeTeam(2)
    league = _make_league()

    bridges = _FakePlayer(10, overall=80, position="SG")
    anunoby = _FakePlayer(11, overall=82, position="SF")
    kuminga = _FakePlayer(20, overall=80, position="SF")
    roster_a = [_FakePlayer(i, overall=75) for i in range(3, 13)] + [bridges, anunoby]

    pool, _get_by_id, _get_active_contract = _make_pool(
        players={10: bridges, 11: anunoby, 20: kuminga},
        contracts={
            10: _FakeContract(salary=14_000_000),
            11: _FakeContract(salary=18_000_000),
            20: _FakeContract(salary=12_000_000),
        },
    )

    with (
        patch.object(_mod.player_repo, "get_by_id", _get_by_id),
        patch.object(_mod.player_repo, "get_active_contract", _get_active_contract),
    ):
        ok, reason = await _apply_final_trade_gates(
            pool=pool,
            league=league,
            team_a=team_a,
            team_b=team_b,
            outgoing_pids_a=[10, 11],    # Bridges + Anunoby going out
            incoming_pids_a=[20],         # Kuminga coming in (not a strict upgrade)
            outgoing_picks_a=[],
            incoming_picks_a=[],
            package_value=60.0,          # close to combined value (passes sanity floor)
            target_value=60.0,
            posture_a="contending",
            cp_contexts={1: {"current_payroll": int(SALARY_CAP * 0.90)}},
            cp_r1_counts={},
            roster_a=roster_a,
            postures=_default_postures(1, "contending"),
        )

    assert ok is False, "Gate should reject contender 2-for-1 without upgrade; got ok=True"
    assert "B5" in reason, f"Expected B5-family rejection; got: {reason}"


# ---------------------------------------------------------------------------
# Test 4: Outgoing-first proposes nothing when all candidates fail gates
# ---------------------------------------------------------------------------


async def test_outgoing_first_proposes_nothing_when_gates_fail():
    """When _apply_final_trade_gates rejects every candidate, no trade is proposed.

    Patch _apply_final_trade_gates to always return (False, "test_gate_fail") and
    _score_outgoing_pair to return a positive score (so the outer loop would
    proceed to propose if gates didn't block).

    Asserts trade_service.propose is never called.
    """
    league = _make_league()
    team_a = team_repo.Team(
        id=1, league_id=LEAGUE_ID, nba_team_code="DEN", name="Denver",
        city="Denver", conference="West", division="Northwest",
        manager_user_id=None, cpu_mode="contending",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )
    team_b = team_repo.Team(
        id=2, league_id=LEAGUE_ID, nba_team_code="HOU", name="Houston",
        city="Houston", conference="West", division="Southwest",
        manager_user_id=None, cpu_mode="rebuilding",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )

    outgoing_player = player_repo.Player(
        id=10, league_id=LEAGUE_ID, external_id=None,
        first_name="Aaron", last_name="Gordon", position="PF",
        height_in=79, weight_lb=220, birth_date=datetime.date(1995, 9, 16),
        years_pro=8, is_rookie=False, team_id=1,
        roster_status="active", overall=76,
        speed=78, shooting_2pt=72, shooting_3pt=72, shooting_mid=72,
        finishing=76, playmaking=70, defense=82, rebounding=78,
        iq=76, potential=76, peak_age_start=26, peak_age_end=30,
        loyalty=60, money_drive=40, win_drive=70, market_pref="any",
        star_leverage=40,
    )
    outgoing_contract = player_repo.Contract(
        id=1, league_id=LEAGUE_ID, player_id=10, team_id=1,
        salary=20_000_000, years_remaining=2, total_years=4,
        contract_type="standard", signed_in_season=SEASON - 2,
        is_active=True, terminated_reason=None,
    )
    incoming_player = player_repo.Player(
        id=20, league_id=LEAGUE_ID, external_id=None,
        first_name="Dillon", last_name="Brooks", position="SF",
        height_in=77, weight_lb=215, birth_date=datetime.date(1996, 1, 22),
        years_pro=6, is_rookie=False, team_id=2,
        roster_status="active", overall=76,
        speed=77, shooting_2pt=72, shooting_3pt=74, shooting_mid=72,
        finishing=72, playmaking=70, defense=80, rebounding=68,
        iq=74, potential=76, peak_age_start=26, peak_age_end=30,
        loyalty=50, money_drive=50, win_drive=60, market_pref="any",
        star_leverage=30,
    )
    incoming_contract = player_repo.Contract(
        id=2, league_id=LEAGUE_ID, player_id=20, team_id=2,
        salary=18_000_000, years_remaining=2, total_years=3,
        contract_type="standard", signed_in_season=SEASON - 1,
        is_active=True, terminated_reason=None,
    )

    pool = MagicMock()

    async def _fetch(sql, *args):
        sql_lower = sql.lower()
        if "pending_counterparty" in sql_lower:
            return []
        return []

    async def _fetchval(sql, *args):
        return None

    async def _fetchrow(sql, *args):
        return None

    pool.fetch = _fetch
    pool.fetchval = _fetchval
    pool.fetchrow = _fetchrow

    plan_a = {
        "goal": "win_now",
        "surplus_player_ids": [10],
        "flex_player_ids": [],
        "core_player_ids": [],
        "asset_targets": [],
        "derived_from_record": {},
    }
    postures = {
        1: {"mode": "contending", "urgency": "comfortable", "avg_age": 28.0,
            "projected_wins": 50, "conf_rank": 2},
        2: {"mode": "rebuilding", "urgency": "tanking", "avg_age": 24.0,
            "projected_wins": 25, "conf_rank": 14},
    }

    # _derive_return_from_b returns a valid speculative return so the loop proceeds.
    async def _fake_derive_return(pool, league, team_b, outgoing_player,
                                   asset_targets_a, taken_player_ids,
                                   recently_signed_ids, plan_b, posture_b,
                                   cp_contexts, cp_r1_counts):
        return ([20], [], 35.0, {20: incoming_contract})

    # _score_outgoing_pair returns positive so the candidate is admitted.
    def _fake_score(team_a, outgoing_pid, team_b, speculative_return_player_ids,
                    speculative_return_pick_ids, speculative_return_players,
                    plan_a, posture_a, roster_a, cp_contexts, cp_r1_counts=None,
                    incoming_contracts=None):
        return 1.5

    propose_calls: list[int] = []

    async def _fake_propose(**kwargs):
        propose_calls.append(1)
        raise AssertionError("trade_service.propose must not be called when gates reject")

    with (
        patch.object(_mod.player_repo, "get_by_id", AsyncMock(side_effect=lambda pool, pid: {
            10: outgoing_player, 20: incoming_player
        }.get(pid))),
        patch.object(_mod.player_repo, "get_active_contract", AsyncMock(side_effect=lambda pool, pid: {
            10: outgoing_contract, 20: incoming_contract
        }.get(pid))),
        patch.object(_mod.player_repo, "get_roster", AsyncMock(return_value=[outgoing_player])),
        patch("services.cpu_trade_proposal_runner._league_scan_counterparties",
              AsyncMock(return_value=[(team_b, 1.0, "fit")])),
        patch("services.cpu_trade_proposal_runner._derive_return_from_b", _fake_derive_return),
        patch("services.cpu_trade_proposal_runner._score_outgoing_pair", _fake_score),
        # Force all gate calls to reject with a named reason.
        patch("services.cpu_trade_proposal_runner._apply_final_trade_gates",
              AsyncMock(return_value=(False, "test_gate_fail: lateral swap with lost pick"))),
        patch.object(_mod.trade_service, "propose", _fake_propose),
    ):
        result = await _mod._attempt_outgoing_first_offer(
            pool=pool,
            league=league,
            season=SEASON,
            team_a=team_a,
            cpu_teams=[team_a, team_b],
            block_by_team={1: [10]},
            used_pairs=set(),
            taken_player_ids=set(),
            deadline_game_index=50,
            recently_signed_ids=set(),
            guild=None,
            postures=postures,
            cp_plans={1: plan_a, 2: None},
            cp_contexts={1: {"current_payroll": int(SALARY_CAP * 0.85)}},
            cp_r1_counts={},
            plan_a=plan_a,
            posture_a="contending",
        )

    assert result == 0, (
        f"_attempt_outgoing_first_offer must return 0 when all candidates fail gates; got {result}"
    )
    assert len(propose_calls) == 0, (
        "trade_service.propose must not be called when gates reject all candidates"
    )


# ---------------------------------------------------------------------------
# Test 5: BLOCKER 1 — outgoing-first tries next candidate when first is rejected
# ---------------------------------------------------------------------------


async def test_outgoing_first_tries_next_candidate_when_first_rejected():
    """Two candidates: first fails gate (archetype-redundant), second passes.

    Assert that propose is called with the SECOND candidate's team, not the first,
    proving the loop does not stop at index [0] on a gate failure.
    """
    league = _make_league()
    team_a = team_repo.Team(
        id=1, league_id=LEAGUE_ID, nba_team_code="DEN", name="Denver",
        city="Denver", conference="West", division="Northwest",
        manager_user_id=None, cpu_mode="contending",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )
    team_b_first = team_repo.Team(
        id=2, league_id=LEAGUE_ID, nba_team_code="HOU", name="Houston",
        city="Houston", conference="West", division="Southwest",
        manager_user_id=None, cpu_mode="rebuilding",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )
    team_b_second = team_repo.Team(
        id=3, league_id=LEAGUE_ID, nba_team_code="OKC", name="OKC",
        city="OKC", conference="West", division="Northwest",
        manager_user_id=None, cpu_mode="rebuilding",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )

    outgoing_player = player_repo.Player(
        id=10, league_id=LEAGUE_ID, external_id=None,
        first_name="Aaron", last_name="Gordon", position="PF",
        height_in=79, weight_lb=220, birth_date=datetime.date(1995, 9, 16),
        years_pro=8, is_rookie=False, team_id=1,
        roster_status="active", overall=76,
        speed=78, shooting_2pt=72, shooting_3pt=72, shooting_mid=72,
        finishing=76, playmaking=70, defense=82, rebounding=78,
        iq=76, potential=76, peak_age_start=26, peak_age_end=30,
        loyalty=60, money_drive=40, win_drive=70, market_pref="any",
        star_leverage=40,
    )
    outgoing_contract = player_repo.Contract(
        id=1, league_id=LEAGUE_ID, player_id=10, team_id=1,
        salary=20_000_000, years_remaining=2, total_years=4,
        contract_type="standard", signed_in_season=SEASON - 2,
        is_active=True, terminated_reason=None,
    )
    incoming_player_first = player_repo.Player(
        id=20, league_id=LEAGUE_ID, external_id=None,
        first_name="Player", last_name="FirstCandidate", position="SF",
        height_in=77, weight_lb=215, birth_date=datetime.date(1996, 1, 22),
        years_pro=6, is_rookie=False, team_id=2,
        roster_status="active", overall=76,
        speed=77, shooting_2pt=72, shooting_3pt=74, shooting_mid=72,
        finishing=72, playmaking=70, defense=80, rebounding=68,
        iq=74, potential=76, peak_age_start=26, peak_age_end=30,
        loyalty=50, money_drive=50, win_drive=60, market_pref="any",
        star_leverage=30,
    )
    incoming_player_second = player_repo.Player(
        id=30, league_id=LEAGUE_ID, external_id=None,
        first_name="Player", last_name="SecondCandidate", position="PG",
        height_in=75, weight_lb=200, birth_date=datetime.date(1997, 3, 10),
        years_pro=5, is_rookie=False, team_id=3,
        roster_status="active", overall=77,
        speed=80, shooting_2pt=74, shooting_3pt=76, shooting_mid=74,
        finishing=70, playmaking=78, defense=74, rebounding=65,
        iq=76, potential=78, peak_age_start=26, peak_age_end=31,
        loyalty=55, money_drive=45, win_drive=65, market_pref="any",
        star_leverage=25,
    )
    incoming_contract_first = player_repo.Contract(
        id=2, league_id=LEAGUE_ID, player_id=20, team_id=2,
        salary=18_000_000, years_remaining=2, total_years=3,
        contract_type="standard", signed_in_season=SEASON - 1,
        is_active=True, terminated_reason=None,
    )
    incoming_contract_second = player_repo.Contract(
        id=3, league_id=LEAGUE_ID, player_id=30, team_id=3,
        salary=17_000_000, years_remaining=2, total_years=3,
        contract_type="standard", signed_in_season=SEASON - 1,
        is_active=True, terminated_reason=None,
    )

    pool = MagicMock()

    async def _fetch(sql, *args):
        if "pending_counterparty" in sql.lower():
            return []
        return []

    async def _fetchval(sql, *args):
        return None

    async def _fetchrow(sql, *args):
        return None

    pool.fetch = _fetch
    pool.fetchval = _fetchval
    pool.fetchrow = _fetchrow

    plan_a = {
        "goal": "win_now",
        "surplus_player_ids": [10],
        "flex_player_ids": [],
        "core_player_ids": [],
        "asset_targets": [],
        "derived_from_record": {},
    }
    postures = {
        1: {"mode": "contending", "urgency": "comfortable", "avg_age": 28.0,
            "projected_wins": 50, "conf_rank": 2},
        2: {"mode": "rebuilding", "urgency": "tanking", "avg_age": 24.0,
            "projected_wins": 25, "conf_rank": 14},
        3: {"mode": "rebuilding", "urgency": "tanking", "avg_age": 23.0,
            "projected_wins": 22, "conf_rank": 15},
    }

    # _derive_return_from_b: first team returns player 20, second team returns player 30.
    async def _fake_derive_return(pool, league, team_b, outgoing_player,
                                   asset_targets_a, taken_player_ids,
                                   recently_signed_ids, plan_b, posture_b,
                                   cp_contexts, cp_r1_counts):
        if team_b.id == 2:
            return ([20], [], 35.0, {20: incoming_contract_first})
        return ([30], [], 37.0, {30: incoming_contract_second})

    # Score both candidates as positive (gate, not score, is the differentiator).
    # First candidate scores higher so it sorts first and hits the gate rejection first.
    def _fake_score(team_a, outgoing_pid, team_b, speculative_return_player_ids,
                    speculative_return_pick_ids, speculative_return_players,
                    plan_a, posture_a, roster_a, cp_contexts, cp_r1_counts=None,
                    incoming_contracts=None):
        return 2.0 if team_b.id == 2 else 1.5

    propose_calls: list[int] = []

    async def _fake_propose(**kwargs):
        propose_calls.append(kwargs["counterparty_team"].id)

        class _FakeTrade:
            id = 999
            status = "pending_commissioner"

        return _FakeTrade()

    async def _fake_auto_approve(pool, league, trade, guild):
        pass

    gate_call_count = [0]

    async def _fake_gates(**kwargs):
        gate_call_count[0] += 1
        # First call (highest-scored candidate — team 2): reject.
        # Second call (team 3): approve.
        if gate_call_count[0] == 1:
            return False, "test_gate_fail: B6_arch redundant"
        return True, ""

    with (
        patch.object(_mod.player_repo, "get_by_id", AsyncMock(side_effect=lambda pool, pid: {
            10: outgoing_player, 20: incoming_player_first, 30: incoming_player_second,
        }.get(pid))),
        patch.object(_mod.player_repo, "get_active_contract", AsyncMock(side_effect=lambda pool, pid: {
            10: outgoing_contract, 20: incoming_contract_first, 30: incoming_contract_second,
        }.get(pid))),
        # Realistic-depth roster (2+ at every core position, 3 at PF) so the
        # B9 roster-hole check (#4) doesn't spuriously fire on this otherwise-
        # unrelated candidate-retry test — outgoing_player (PF) leaving still
        # clears the hole floor since PF has depth beyond just outgoing_player.
        patch.object(_mod.player_repo, "get_roster", AsyncMock(return_value=[
            outgoing_player,
            *[_FakePlayer(100 + i, position=pos)
              for i, pos in enumerate(["PF", "PF", "PG", "PG", "SG", "SG", "SF", "SF", "C", "C"])],
        ])),
        patch("services.cpu_trade_proposal_runner._league_scan_counterparties",
              AsyncMock(return_value=[(team_b_first, 1.0, "fit"), (team_b_second, 0.9, "fit")])),
        patch("services.cpu_trade_proposal_runner._derive_return_from_b", _fake_derive_return),
        patch("services.cpu_trade_proposal_runner._score_outgoing_pair", _fake_score),
        patch("services.cpu_trade_proposal_runner._apply_final_trade_gates", _fake_gates),
        patch("services.cpu_trade_proposal_runner._team_archetype_counts", return_value={}),
        patch("services.cpu_trade_proposal_runner.trade_grading._player_archetype", return_value=None),
        patch.object(_mod.trade_service, "propose", _fake_propose),
        patch("services.cpu_trade_proposal_runner._maybe_auto_approve", _fake_auto_approve),
    ):
        result = await _mod._attempt_outgoing_first_offer(
            pool=pool,
            league=league,
            season=SEASON,
            team_a=team_a,
            cpu_teams=[team_a, team_b_first, team_b_second],
            block_by_team={1: [10]},
            used_pairs=set(),
            taken_player_ids=set(),
            deadline_game_index=50,
            recently_signed_ids=set(),
            guild=None,
            postures=postures,
            cp_plans={1: plan_a, 2: None, 3: None},
            cp_contexts={1: {"current_payroll": int(SALARY_CAP * 0.85)}},
            cp_r1_counts={},
            plan_a=plan_a,
            posture_a="contending",
        )

    assert result == 1, (
        f"_attempt_outgoing_first_offer must return 1 when second candidate passes gates; got {result}"
    )
    assert len(propose_calls) == 1, (
        f"trade_service.propose must be called exactly once; got {len(propose_calls)} calls"
    )
    assert propose_calls[0] == 3, (
        f"propose must fire for the second candidate (team 3), not the first (team 2); "
        f"got team_id={propose_calls[0]}"
    )


# ---------------------------------------------------------------------------
# Test 6: BLOCKER 4 — B6 is soft-penalty in incoming-first (NOT hard-reject)
# ---------------------------------------------------------------------------


async def test_incoming_first_b6_remains_soft_penalty():
    """B6 archetype redundancy does NOT hard-reject via _apply_final_trade_gates.

    _apply_final_trade_gates no longer contains a B6 gate; B6 is applied only
    as a soft penalty in pass-2 scoring.  So a trade where the incoming player
    has a redundant archetype (count >= 2) must still pass the gate helper.

    This mirrors the pre-B8 behavior: incoming-first downscores B6 candidates
    rather than outright rejecting them, so high-value redundant-archetype
    targets can still surface if no better option exists.
    """
    team_a = _FakeTeam(1, cpu_mode="contending")
    team_b = _FakeTeam(2)
    league = _make_league()

    # Outgoing: OVR-79 player (no archetype impact on the test)
    outgoing_p = _FakePlayer(10, overall=79, position="PF")
    incoming_p = _FakePlayer(
        20, overall=82, position="SG",
        # High 3pt tendency → would be "3pt_specialist" archetype
        tendency_3pt=90,
    )

    # Team A's post-trade roster already has 2 players with 3pt_specialist archetype.
    # This simulates the B6 redundancy condition that WOULD hard-reject in outgoing-first.
    arch_player_1 = _FakePlayer(30, overall=76, position="SG", tendency_3pt=85)
    arch_player_2 = _FakePlayer(31, overall=75, position="SF", tendency_3pt=88)
    roster_a = (
        [_FakePlayer(i, overall=74) for i in range(40, 50)]
        + [outgoing_p, arch_player_1, arch_player_2]
    )

    pool, _get_by_id, _get_active_contract = _make_pool(
        players={10: outgoing_p, 20: incoming_p},
        contracts={10: _FakeContract(salary=14_000_000), 20: _FakeContract(salary=18_000_000)},
    )

    with (
        patch.object(_mod.player_repo, "get_by_id", _get_by_id),
        patch.object(_mod.player_repo, "get_active_contract", _get_active_contract),
    ):
        ok, reason = await _apply_final_trade_gates(
            pool=pool,
            league=league,
            team_a=team_a,
            team_b=team_b,
            outgoing_pids_a=[10],
            incoming_pids_a=[20],
            outgoing_picks_a=[],
            incoming_picks_a=[],
            package_value=50.0,   # ratio ≈ 1.05, above floor
            target_value=48.0,
            posture_a="contending",
            cp_contexts={1: {"current_payroll": int(SALARY_CAP * 0.85)}},
            cp_r1_counts={},
            roster_a=roster_a,
            postures=_default_postures(1, "contending"),
        )

    # B6 is NOT a hard gate here — the helper should approve.
    # The downscoring happens in pass-2, not in the gate layer.
    assert ok is True, (
        f"_apply_final_trade_gates must NOT hard-reject B6 archetype redundancy "
        f"(soft-penalty belongs in pass-2 scoring); got ok=False reason={reason!r}"
    )
    assert "B6" not in reason, (
        f"No B6 rejection expected from gate helper; got: {reason!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: B9 (#4) roster-hole downweight causes outgoing-first to try the
# next candidate, exactly like a real gate rejection would.
# ---------------------------------------------------------------------------


async def test_outgoing_first_skips_candidate_that_creates_a_roster_hole():
    """Team A's roster has exactly one PF (the outgoing player) and thin depth
    (2) everywhere else. _apply_final_trade_gates is mocked to always approve
    (isolates the test to B9, not B1/B5/sanity-floor). Candidate 1 (team 2)
    returns a non-PF player — doesn't backfill, B9 downweight drops the ratio
    below the sanity floor, candidate rejected. Candidate 2 (team 3) returns a
    PF — backfills the hole, B9 finds no hole, trade proceeds.
    """
    league = _make_league()
    team_a = team_repo.Team(
        id=1, league_id=LEAGUE_ID, nba_team_code="DEN", name="Denver",
        city="Denver", conference="West", division="Northwest",
        manager_user_id=None, cpu_mode="contending",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )
    team_b_first = team_repo.Team(
        id=2, league_id=LEAGUE_ID, nba_team_code="HOU", name="Houston",
        city="Houston", conference="West", division="Southwest",
        manager_user_id=None, cpu_mode="rebuilding",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )
    team_b_second = team_repo.Team(
        id=3, league_id=LEAGUE_ID, nba_team_code="OKC", name="OKC",
        city="OKC", conference="West", division="Northwest",
        manager_user_id=None, cpu_mode="rebuilding",
        team_offense_rating=None, team_defense_rating=None, pace=None,
    )

    outgoing_player = player_repo.Player(
        id=10, league_id=LEAGUE_ID, external_id=None,
        first_name="Aaron", last_name="Gordon", position="PF",
        height_in=79, weight_lb=220, birth_date=datetime.date(1995, 9, 16),
        years_pro=8, is_rookie=False, team_id=1,
        roster_status="active", overall=76,
        speed=78, shooting_2pt=72, shooting_3pt=72, shooting_mid=72,
        finishing=76, playmaking=70, defense=82, rebounding=78,
        iq=76, potential=76, peak_age_start=26, peak_age_end=30,
        loyalty=60, money_drive=40, win_drive=70, market_pref="any",
        star_leverage=40,
    )
    outgoing_contract = player_repo.Contract(
        id=1, league_id=LEAGUE_ID, player_id=10, team_id=1,
        salary=20_000_000, years_remaining=2, total_years=4,
        contract_type="standard", signed_in_season=SEASON - 2,
        is_active=True, terminated_reason=None,
    )
    # Candidate 1's return: an SF — does NOT backfill the vacated PF slot.
    incoming_player_first = player_repo.Player(
        id=20, league_id=LEAGUE_ID, external_id=None,
        first_name="Player", last_name="NonBackfill", position="SF",
        height_in=77, weight_lb=215, birth_date=datetime.date(1996, 1, 22),
        years_pro=6, is_rookie=False, team_id=2,
        roster_status="active", overall=76,
        speed=77, shooting_2pt=72, shooting_3pt=74, shooting_mid=72,
        finishing=72, playmaking=70, defense=80, rebounding=68,
        iq=74, potential=76, peak_age_start=26, peak_age_end=30,
        loyalty=50, money_drive=50, win_drive=60, market_pref="any",
        star_leverage=30,
    )
    # Candidate 2's return: a PF — DOES backfill the vacated slot.
    incoming_player_second = player_repo.Player(
        id=30, league_id=LEAGUE_ID, external_id=None,
        first_name="Player", last_name="Backfill", position="PF",
        height_in=81, weight_lb=230, birth_date=datetime.date(1997, 3, 10),
        years_pro=5, is_rookie=False, team_id=3,
        roster_status="active", overall=76,
        speed=72, shooting_2pt=74, shooting_3pt=68, shooting_mid=72,
        finishing=74, playmaking=60, defense=78, rebounding=76,
        iq=74, potential=76, peak_age_start=26, peak_age_end=31,
        loyalty=55, money_drive=45, win_drive=65, market_pref="any",
        star_leverage=25,
    )
    incoming_contract_first = player_repo.Contract(
        id=2, league_id=LEAGUE_ID, player_id=20, team_id=2,
        salary=18_000_000, years_remaining=2, total_years=3,
        contract_type="standard", signed_in_season=SEASON - 1,
        is_active=True, terminated_reason=None,
    )
    incoming_contract_second = player_repo.Contract(
        id=3, league_id=LEAGUE_ID, player_id=30, team_id=3,
        salary=18_000_000, years_remaining=2, total_years=3,
        contract_type="standard", signed_in_season=SEASON - 1,
        is_active=True, terminated_reason=None,
    )

    pool = MagicMock()

    async def _fetch(sql, *args):
        if "pending_counterparty" in sql.lower():
            return []
        return []

    async def _fetchval(sql, *args):
        return None

    async def _fetchrow(sql, *args):
        return None

    pool.fetch = _fetch
    pool.fetchval = _fetchval
    pool.fetchrow = _fetchrow

    plan_a = {
        "goal": "win_now",
        "surplus_player_ids": [10],
        "flex_player_ids": [],
        "core_player_ids": [],
        "asset_targets": [],
        "derived_from_record": {},
    }
    postures = {
        1: {"mode": "contending", "urgency": "comfortable", "avg_age": 28.0,
            "projected_wins": 50, "conf_rank": 2},
        2: {"mode": "rebuilding", "urgency": "tanking", "avg_age": 24.0,
            "projected_wins": 25, "conf_rank": 14},
        3: {"mode": "rebuilding", "urgency": "tanking", "avg_age": 23.0,
            "projected_wins": 22, "conf_rank": 15},
    }

    async def _fake_derive_return(pool, league, team_b, outgoing_player,
                                   asset_targets_a, taken_player_ids,
                                   recently_signed_ids, plan_b, posture_b,
                                   cp_contexts, cp_r1_counts):
        if team_b.id == 2:
            return ([20], [], 35.0, {20: incoming_contract_first})
        return ([30], [], 35.0, {30: incoming_contract_second})

    # Both candidates score equally so team 2 (inserted first / higher fit)
    # sorts first — the retry must be driven by B9, not the score order.
    def _fake_score(team_a, outgoing_pid, team_b, speculative_return_player_ids,
                    speculative_return_pick_ids, speculative_return_players,
                    plan_a, posture_a, roster_a, cp_contexts, cp_r1_counts=None,
                    incoming_contracts=None):
        return 2.0 if team_b.id == 2 else 1.5

    propose_calls: list[int] = []

    async def _fake_propose(**kwargs):
        propose_calls.append(kwargs["counterparty_team"].id)

        class _FakeTrade:
            id = 999
            status = "pending_commissioner"

        return _FakeTrade()

    async def _fake_auto_approve(pool, league, trade, guild):
        pass

    # Always approve — isolates the test to B9's own retry-on-hole behavior,
    # not B1/B5/sanity-floor (already covered by tests 2/3 above).
    async def _fake_gates_always_ok(**kwargs):
        return True, ""

    # Thin roster: exactly two PF (the outgoing player + one backup) and 2 at
    # every other core position — matches _roster_hole_penalty's hole_floor(2)/
    # surplus_floor(3) defaults. Losing the outgoing PF with no PF coming back
    # drops PF to 1 (a hole); a PF coming back keeps it at 2 (no hole).
    _thin_roster = [
        outgoing_player,
        *[_FakePlayer(100 + i, position=pos)
          for i, pos in enumerate(["PF", "PG", "PG", "SG", "SG", "SF", "SF", "C", "C"])],
    ]

    with (
        patch.object(_mod.player_repo, "get_by_id", AsyncMock(side_effect=lambda pool, pid: {
            10: outgoing_player, 20: incoming_player_first, 30: incoming_player_second,
        }.get(pid))),
        patch.object(_mod.player_repo, "get_active_contract", AsyncMock(side_effect=lambda pool, pid: {
            10: outgoing_contract, 20: incoming_contract_first, 30: incoming_contract_second,
        }.get(pid))),
        patch.object(_mod.player_repo, "get_roster", AsyncMock(return_value=_thin_roster)),
        patch("services.cpu_trade_proposal_runner._league_scan_counterparties",
              AsyncMock(return_value=[(team_b_first, 1.0, "fit"), (team_b_second, 0.9, "fit")])),
        patch("services.cpu_trade_proposal_runner._derive_return_from_b", _fake_derive_return),
        patch("services.cpu_trade_proposal_runner._score_outgoing_pair", _fake_score),
        patch("services.cpu_trade_proposal_runner._apply_final_trade_gates", _fake_gates_always_ok),
        patch("services.cpu_trade_proposal_runner._team_archetype_counts", return_value={}),
        patch("services.cpu_trade_proposal_runner.trade_grading._player_archetype", return_value=None),
        patch.object(_mod.trade_service, "propose", _fake_propose),
        patch("services.cpu_trade_proposal_runner._maybe_auto_approve", _fake_auto_approve),
    ):
        result = await _mod._attempt_outgoing_first_offer(
            pool=pool,
            league=league,
            season=SEASON,
            team_a=team_a,
            cpu_teams=[team_a, team_b_first, team_b_second],
            block_by_team={1: [10]},
            used_pairs=set(),
            taken_player_ids=set(),
            deadline_game_index=50,
            recently_signed_ids=set(),
            guild=None,
            postures=postures,
            cp_plans={1: plan_a, 2: None, 3: None},
            cp_contexts={1: {"current_payroll": int(SALARY_CAP * 0.85)}},
            cp_r1_counts={},
            plan_a=plan_a,
            posture_a="contending",
        )

    assert result == 1, (
        f"_attempt_outgoing_first_offer must fall through to the backfilling "
        f"candidate after B9 skips the hole-creating one; got {result}"
    )
    assert propose_calls == [3], (
        f"propose must fire only for the backfilling candidate (team 3); got {propose_calls}"
    )
