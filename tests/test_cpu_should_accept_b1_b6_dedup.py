"""
Unit tests for two accept-path gaps closed together (same file, cpu_should_accept):

1. B6 archetype redundancy — previously only checked on the propose/search side
   (trade_proposal_scoring._team_archetype_counts). cpu_should_accept now hard-
   rejects when an incoming player's archetype is already 2+ deep on the
   accepting team's post-trade roster (mirrors the outgoing-first hard-reject
   threshold in cpu_trade_proposal_runner.py).

2. B1 dedup — cpu_should_accept now calls the SAME
   trade_proposal_scoring._team_a_wants_player helper the propose-side self-check
   (trade_gates._apply_final_trade_gates Gate 4) uses, instead of re-deriving a
   second set of age/OVR cutoffs. Only enforced when a live posture dict is
   supplied via context_kwargs["posture"] — existing callers that only pass a
   bare mode string are unaffected (verified by the "skip" tests below).
"""
from __future__ import annotations

from services import cpu_trade_acceptance

SALARY_CAP = 140_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_player_asset(
    overall: int,
    age: int = 27,
    salary_frac: float = 0.15,
    years: int = 2,
    first_name: str = "Player",
    last_name: str = "Test",
    player_id: int | None = None,
    tendency_3pt: int = 50,
    tendency_drive: int = 50,
    tendency_pass: int = 50,
    ast_tendency: int = 50,
    reb_tendency: int = 50,
    blk_tendency: int = 50,
    stl_tendency: int = 50,
    position: str = "SG",
) -> dict:
    return {
        "asset_type": "player",
        "player": {
            "id": player_id,
            "overall": overall,
            "age": age,
            "first_name": first_name,
            "last_name": last_name,
            "position": position,
            "tendency_3pt": tendency_3pt,
            "tendency_drive": tendency_drive,
            "tendency_pass": tendency_pass,
            "ast_tendency": ast_tendency,
            "reb_tendency": reb_tendency,
            "blk_tendency": blk_tendency,
            "stl_tendency": stl_tendency,
        },
        "contract": {
            "salary": int(SALARY_CAP * salary_frac),
            "years_remaining": years,
        },
    }


def _build_evaluation(score_a: float, score_b: float) -> dict:
    differential = abs(score_a - score_b)
    max_side = max(score_a, score_b, 1.0)
    return {
        "score_a": score_a,
        "score_b": score_b,
        "differential": differential,
        "is_fair": differential < max_side * 0.20,
        "rationale": "test",
    }


class _FakeRosterPlayer:
    """Minimal roster-player stand-in for _team_archetype_counts (dict-or-Player duck type)."""

    def __init__(
        self,
        player_id: int,
        position: str = "SG",
        tendency_3pt: int = 50,
        tendency_drive: int = 50,
        tendency_pass: int = 50,
        ast_tendency: int = 50,
        reb_tendency: int = 50,
        blk_tendency: int = 50,
        stl_tendency: int = 50,
    ):
        self.id = player_id
        self.position = position
        self.tendency_3pt = tendency_3pt
        self.tendency_drive = tendency_drive
        self.tendency_pass = tendency_pass
        self.ast_tendency = ast_tendency
        self.reb_tendency = reb_tendency
        self.blk_tendency = blk_tendency
        self.stl_tendency = stl_tendency


def _neutral_filler(start_id: int, count: int) -> list[_FakeRosterPlayer]:
    """Roster filler with no clear archetype (all tendencies neutral at 50)."""
    return [_FakeRosterPlayer(i) for i in range(start_id, start_id + count)]


# ---------------------------------------------------------------------------
# B6: archetype redundancy on the accept path
# ---------------------------------------------------------------------------


async def test_b6_accept_path_rejects_third_same_archetype():
    """Team already rosters 2 shooter-archetype players; incoming is a 3rd shooter.

    Outgoing player is a neutral-archetype filler, so the redundancy comes purely
    from the incoming player's archetype overlapping the roster that stays.
    """
    outgoing = _make_player_asset(overall=75, age=27, player_id=999)
    incoming = _make_player_asset(
        overall=78, age=24, player_id=500, tendency_3pt=80,
    )

    roster = _neutral_filler(1, 10) + [
        _FakeRosterPlayer(999, tendency_3pt=50),  # the outgoing player itself
        _FakeRosterPlayer(30, tendency_3pt=85),   # shooter #1 (stays)
        _FakeRosterPlayer(31, tendency_3pt=88),   # shooter #2 (stays)
    ]

    evaluation = _build_evaluation(score_a=50.0, score_b=48.0)

    accept, reason = await cpu_trade_acceptance.cpu_should_accept(
        cpu_team_mode="developing",
        assets_receiving=[incoming],
        assets_giving=[outgoing],
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.60),
        receiving_team_roster=roster,
    )

    assert accept is False, f"Expected B6 accept-path reject; got accept=True reason={reason}"
    assert "B6" in reason, f"Expected B6 rejection reason; got: {reason}"


async def test_b6_accept_path_skipped_without_roster():
    """Same shape as above but no receiving_team_roster passed — must not reject via B6.

    Matches the giving_role_map silent-skip precedent: callers that haven't been
    updated to pass roster data aren't penalised.
    """
    outgoing = _make_player_asset(overall=75, age=27, player_id=999)
    incoming = _make_player_asset(
        overall=78, age=24, player_id=500, tendency_3pt=80,
    )
    evaluation = _build_evaluation(score_a=50.0, score_b=48.0)

    accept, reason = await cpu_trade_acceptance.cpu_should_accept(
        cpu_team_mode="developing",
        assets_receiving=[incoming],
        assets_giving=[outgoing],
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.60),
    )

    assert "B6" not in reason, f"B6 should not fire without receiving_team_roster; got: {reason}"


async def test_b6_excludes_outgoing_player_from_pretrade_count():
    """Only 2 shooter-archetype players total, and one of them IS the outgoing player.

    Post-trade roster (after removing the outgoing player) has only 1 shooter left —
    below the >=2 redundancy threshold — so B6 must NOT fire here.
    """
    outgoing = _make_player_asset(
        overall=75, age=27, player_id=999, tendency_3pt=85,  # outgoing IS a shooter
    )
    incoming = _make_player_asset(
        overall=78, age=24, player_id=500, tendency_3pt=80,
    )

    roster = _neutral_filler(1, 10) + [
        _FakeRosterPlayer(999, tendency_3pt=85),  # outgoing shooter (leaves roster)
        _FakeRosterPlayer(30, tendency_3pt=88),   # the only shooter that stays
    ]

    evaluation = _build_evaluation(score_a=50.0, score_b=48.0)

    accept, reason = await cpu_trade_acceptance.cpu_should_accept(
        cpu_team_mode="developing",
        assets_receiving=[incoming],
        assets_giving=[outgoing],
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.60),
        receiving_team_roster=roster,
    )

    assert "B6" not in reason, (
        f"B6 must exclude the outgoing player from the pre-trade archetype count; got: {reason}"
    )


# ---------------------------------------------------------------------------
# B1 dedup: cpu_should_accept calls the shared _team_a_wants_player helper
# ---------------------------------------------------------------------------


async def test_b1_dedup_rejects_young_low_ovr_throwin_for_contender():
    """Contender posture supplied; incoming player is a raw developmental throw-in
    (age 22, OVR 70) that _team_a_wants_player's contending branch rejects
    (age < 25 and OVR < 77). Scores are balanced so no other gate fires first.
    """
    outgoing = _make_player_asset(overall=75, age=28, player_id=1)
    incoming = _make_player_asset(overall=70, age=22, player_id=2)
    evaluation = _build_evaluation(score_a=50.0, score_b=50.0)

    accept, reason = await cpu_trade_acceptance.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=[incoming],
        assets_giving=[outgoing],
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.80),
        context_kwargs={"posture": {"mode": "contending", "urgency": "comfortable", "avg_age": 27.0}},
    )

    assert accept is False, f"Expected B1 dedup reject; got accept=True reason={reason}"
    assert reason.startswith("B1:"), f"Expected B1-prefixed rejection reason; got: {reason}"


async def test_b1_dedup_skipped_without_posture_context():
    """Same asset shape as above, but no context_kwargs/posture supplied.

    The new shared-helper gate must not fire (existing callers unaffected); the
    trade may still be rejected by an existing rule, just not by this one.
    """
    outgoing = _make_player_asset(overall=75, age=28, player_id=1)
    incoming = _make_player_asset(overall=70, age=22, player_id=2)
    evaluation = _build_evaluation(score_a=50.0, score_b=50.0)

    accept, reason = await cpu_trade_acceptance.cpu_should_accept(
        cpu_team_mode="contending",
        assets_receiving=[incoming],
        assets_giving=[outgoing],
        evaluation=evaluation,
        salary_cap=SALARY_CAP,
        current_cap_used=int(SALARY_CAP * 0.80),
    )

    assert not reason.startswith("B1:"), (
        f"B1 dedup gate should be skipped without a posture dict; got: {reason}"
    )
