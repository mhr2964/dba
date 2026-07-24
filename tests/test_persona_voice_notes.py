"""
Regression tests pinning specific documented-feedback prompt rules in persona
voice_notes (Finding #7).

7a: pat_chen.py ships the "no league-wide arms race" rule documented in
Brain/Note Pad/dba/pat-chen-feedback-eval.md (P5) — previously proposed but
never shipped.

7b: marcus_cole.py already ships every rule documented in
Brain/Note Pad/dba/marcus-cole-feedback-eval.md (A1-A8) — this locks that in
so a future edit can't silently drop one without a test noticing.

Also locks in the light differentiation added to keisha_williams.py (a
persistent stated bias, distinguishing her from Marcus Brooks/Pat Chen who
were otherwise flavors of the same analytics viewpoint).
"""
from __future__ import annotations

from services.personas import PERSONAS


def test_pat_chen_ships_no_arms_race_rule():
    voice_notes = PERSONAS["pat_chen"].voice_notes.lower()
    assert "arms race" in voice_notes
    assert "first season" in voice_notes


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
