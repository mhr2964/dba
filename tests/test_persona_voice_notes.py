"""
Regression tests pinning specific documented-feedback prompt rules in persona
voice_notes (Finding #7).

7a: pat_chen.py ships the "no league-wide arms race" rule documented in
Brain/Note Pad/dba/pat-chen-feedback-eval.md (P5) — previously proposed but
never shipped. Phase 2 fix D3 made this rule conditional (real history data
can now justify a genuine comparison) instead of an unconditional ban — the
test below was updated alongside that rewrite; it still pins that the
season-1 humility language survives as the fallback branch.

7b: marcus_cole.py already ships every rule documented in
Brain/Note Pad/dba/marcus-cole-feedback-eval.md (A1-A8) — this locks that in
so a future edit can't silently drop one without a test noticing.

Also locks in the light differentiation added to keisha_williams.py (a
persistent stated bias, distinguishing her from Marcus Brooks/Pat Chen who
were otherwise flavors of the same analytics viewpoint).
"""
from __future__ import annotations

from services import columnist_service
from services.personas import PERSONAS


def test_pat_chen_ships_no_arms_race_rule():
    voice_notes = PERSONAS["pat_chen"].voice_notes.lower()
    assert "arms race" in voice_notes
    assert "first season" in voice_notes


def test_pat_chen_arms_race_rule_is_conditional_on_real_history_data():
    """D3: the rule bans manufactured narratives, but explicitly carves out
    a real historical comparison when all_time_records data supports it."""
    voice_notes = PERSONAS["pat_chen"].voice_notes.lower()
    assert "all_time_records" in voice_notes
    assert "unless real historical context data actually" in voice_notes
    assert "all_time_records" in PERSONAS["pat_chen"].context_keys


def test_pat_chen_has_explicit_word_cap_in_prompt_text():
    """A4: the ~120-word target lives in the LLM-facing prompt text itself,
    not just the code-side _PERSONA_WORD_TARGET enforcement table."""
    voice_notes = PERSONAS["pat_chen"].voice_notes.lower()
    assert "120 words" in voice_notes


def test_pat_chen_has_no_sidebar_field():
    """P1a: the sidebar was already removed before this pass — pin that it
    stays gone (no sidebar field/reference anywhere in the persona shape)."""
    voice_notes = PERSONAS["pat_chen"].voice_notes.lower()
    assert "sidebar" not in voice_notes


def test_pat_chen_has_player_detail_rules():
    """P4: name the defender(s) on a defensive failure and a teammate who
    benefited from playmaking, with a stat — never shipped until now."""
    voice_notes = PERSONAS["pat_chen"].voice_notes.lower()
    assert "name the defender" in voice_notes
    assert "name at least one teammate" in voice_notes


def test_coach_beat_has_explicit_word_cap_in_prompt_text():
    """A5: coach_beat had no firm code-enforced cap pre-Phase-1 (only an
    inferred estimate from its stated 2-paragraphs-plus-closer format) — this
    pins an explicit hard word count now stated in the prompt text itself."""
    voice_notes = PERSONAS["coach_beat"].voice_notes.lower()
    assert "120 words" in voice_notes


def test_coach_beat_has_scheme_history_awareness():
    """C3: coach_beat's context_keys declares scheme_history and its
    voice_notes references it (has this team run this scheme before, did it
    work), degrading gracefully when the data isn't there yet."""
    assert "scheme_history" in PERSONAS["coach_beat"].context_keys
    voice_notes = PERSONAS["coach_beat"].voice_notes.lower()
    assert "scheme_history" in voice_notes
    assert "w-l record" in voice_notes


def test_marcus_brooks_has_real_stat_computation_mechanic():
    """C1: Marcus Brooks was one of the two thinnest personas (near-generic
    voice, no domain-specific mechanics) — this pins the added eFG%/margin
    computation rule that backs his "hot/struggling" claims with real math."""
    voice_notes = PERSONAS["marcus_brooks"].voice_notes
    assert "REAL MECHANICS" in voice_notes
    assert "eFG%" in voice_notes
    assert "average scoring margin" in voice_notes


def test_ren_takahashi_has_real_transaction_mechanic():
    """C1: Ren Takahashi was the other thinnest persona — this pins the added
    rental-vs-commitment / roster-fit heuristic, scoped to fit his tight
    2-sentence/40-word cap (ren_takahashi is NOT in the word-target edit
    exception list — only voice_notes text changed, target stays 40)."""
    voice_notes = PERSONAS["ren_takahashi"].voice_notes.lower()
    assert "rental" in voice_notes
    assert "logjam" in voice_notes
    assert columnist_service._resolve_word_target("ren_takahashi") == 40


def test_keisha_williams_has_a_stated_standing_bias():
    voice_notes = PERSONAS["keisha_williams"].voice_notes.lower()
    assert "standing bias" in voice_notes
    assert "two-way" in voice_notes


def test_marcus_cole_ships_all_documented_rules():
    voice_notes = PERSONAS["marcus_cole"].voice_notes
    # A1 — asymmetric trade logic, name the loser side.
    assert "ASYMMETRY" in voice_notes
    # A2 / A8 — trajectory/cycle position stated explicitly up front.
    assert "TRAJECTORY FIRST" in voice_notes
    # A2 / A7 — posture mode woven in, and questioned when inconsistent with the roster.
    assert "POSTURE RULE" in voice_notes
    assert "INCONSISTENT" in voice_notes
    # A3 — asset upside beyond OVR (age premium).
    assert "ASSET UPSIDE RULE" in voice_notes
    # A4 — speculate about a followup move.
    assert "FOLLOWUP MOVE RULE" in voice_notes
    # A5 — core continuity (recently drafted picks).
    assert "CORE CONTINUITY RULE" in voice_notes
    # A6 — archetype/role fit, not just position.
    assert "ARCHETYPE RULE" in voice_notes
