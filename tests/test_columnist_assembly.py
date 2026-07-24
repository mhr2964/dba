"""
Characterization tests for the pure article-assembly/parsing functions now
in columnist_assembly.py (originally written against columnist_service.py
before the Phase 3 split; re-run unchanged afterward -- see HANDOFF.md).

Pure functions -- no DB, no async, no LLM calls -- so no fakes/mocks
needed. Zero coverage existed for any of these before this file, despite
them being what actually renders every columnist article's final text.
"""
from __future__ import annotations

from services import columnist_assembly as cs
from services.announcer_protocol import EmbedData


# ---------------------------------------------------------------------------
# _dedupe_headline
# ---------------------------------------------------------------------------

def test_dedupe_headline_exact_match_strips_first_line():
    # lstrip("\n") on the remainder means leading blank lines are also removed.
    result = cs._dedupe_headline("Lakers Win Big", "Lakers Win Big\n\nThe rest of the story.")
    assert result == "The rest of the story."


def test_dedupe_headline_markdown_bold_match_strips_first_line():
    result = cs._dedupe_headline("Lakers Win Big", "**Lakers Win Big**\n\nThe rest of the story.")
    assert result == "The rest of the story."


def test_dedupe_headline_prefix_match_strips_chaser():
    result = cs._dedupe_headline(
        "Lakers Win Big On The Road",
        "Lakers Win Big On The Road — and here's why it matters.\n\nMore body.",
    )
    assert result == "More body."


def test_dedupe_headline_no_match_returns_body_unchanged():
    body = "Something completely different.\n\nMore body."
    result = cs._dedupe_headline("Lakers Win Big", body)
    assert result == body


def test_dedupe_headline_empty_headline_or_body_returns_body():
    assert cs._dedupe_headline("", "some body") == "some body"
    assert cs._dedupe_headline("headline", "") == ""


# ---------------------------------------------------------------------------
# _assemble_passthrough
# ---------------------------------------------------------------------------

def test_assemble_passthrough_normal():
    result = cs._assemble_passthrough({"headline": "H", "body": "Body text."}, "Maya Chen")
    assert result == "Body text.\n\n— *Maya Chen*"


def test_assemble_passthrough_dedupes_headline():
    result = cs._assemble_passthrough({"headline": "H", "body": "H\n\nBody text."}, "Maya Chen")
    assert result == "Body text.\n\n— *Maya Chen*"


def test_assemble_passthrough_empty_body_returns_none():
    assert cs._assemble_passthrough({"headline": "H", "body": ""}, "Maya Chen") is None


# ---------------------------------------------------------------------------
# _assemble_tank_watch
# ---------------------------------------------------------------------------

def test_assemble_tank_watch_normal():
    result = cs._assemble_tank_watch({"headline": "H", "body": "Ladder here."}, "Darius Cole")
    assert result == "Ladder here.\n\n— *Darius Cole*"


def test_assemble_tank_watch_falls_back_to_analytics_when_no_body():
    result = cs._assemble_tank_watch(
        {"lede": "Lede.", "key_stats": [], "bullets": [], "verdict": "V"}, "Darius Cole",
    )
    assert "Lede." in result
    assert "Bottom line" in result


# ---------------------------------------------------------------------------
# _assemble_default
# ---------------------------------------------------------------------------

def test_assemble_default_body_template_shape():
    result = cs._assemble_default({"headline": "H", "body": "Body text."}, "Persona")
    assert result == "Body text.\n\n— *Persona*"


def test_assemble_default_structured_fields_shape():
    parsed = {
        "lede": "Lede line.",
        "key_stats": [{"label": "PPG", "value": "30"}],
        "bullets": ["First bullet"],
        "verdict": "Great game.",
    }
    result = cs._assemble_default(parsed, "Persona")
    assert "Lede line." in result
    assert "## Key Numbers" in result
    assert "**PPG**: 30" in result
    assert "## The Read" in result
    assert "First bullet" in result
    assert "**Verdict:** Great game." in result
    assert "— *Persona*" in result


# ---------------------------------------------------------------------------
# _assemble_analytics
# ---------------------------------------------------------------------------

def test_assemble_analytics_renders_bold_stats_and_bottom_line():
    parsed = {
        "lede": "Lede.",
        "key_stats": [{"label": "PPG", "value": "25"}],
        "bullets": ["Bullet one"],
        "verdict": "Verdict text.",
    }
    result = cs._assemble_analytics(parsed, "Marcus Brooks")
    assert "**PPG:** 25" in result
    assert "Lede." in result
    assert "• Bullet one" in result
    assert "**Bottom line:** Verdict text." in result


# ---------------------------------------------------------------------------
# _assemble_hot_take
# ---------------------------------------------------------------------------

def test_assemble_hot_take_turns_shape():
    parsed = {"turns": [{"speaker": "dave", "line": "I love this team."},
                         {"speaker": "tony", "line": "You're wrong."}]}
    result = cs._assemble_hot_take(parsed, "Dave & Tony")
    assert "**DAVE:** I love this team." in result
    assert "**TONY:** You're wrong." in result


def test_assemble_hot_take_legacy_body_shape_bolds_speakers():
    parsed = {"body": "DAVE: I love this team.\n\nTONY: You're wrong."}
    result = cs._assemble_hot_take(parsed, "Dave & Tony")
    assert "**DAVE:**" in result
    assert "**TONY:**" in result


def test_assemble_hot_take_no_content_returns_none():
    assert cs._assemble_hot_take({}, "Dave & Tony") is None


# ---------------------------------------------------------------------------
# _assemble_tactical (B1 — revived as EmbedData; not wired into _RENDERERS)
# ---------------------------------------------------------------------------

def test_assemble_tactical_splits_bullets_into_worked_and_didnt():
    parsed = {
        "headline": "Coaching Read",
        "lede": "Lede.",
        "key_stats": [{"label": "AST", "value": "12"}],
        "bullets": ["Good thing", "Bad thing"],
        "verdict": "Adjust X.",
    }
    result = cs._assemble_tactical(parsed, "Quinn Park")
    assert isinstance(result, EmbedData)
    assert result.title == "Coaching Read"
    assert "Lede." in result.description
    field_names = [f.name for f in result.fields]
    assert "✅ What Worked" in field_names
    assert "❌ What Didn't" in field_names
    assert "🔧 The Adjustment" in field_names
    worked_field = next(f for f in result.fields if f.name == "✅ What Worked")
    assert "Good thing" in worked_field.value
    assert worked_field.inline is True
    adjustment_field = next(f for f in result.fields if f.name == "🔧 The Adjustment")
    assert "Adjust X." in adjustment_field.value


# ---------------------------------------------------------------------------
# _assemble_recap (B1 — revived as EmbedData; not wired into _RENDERERS)
# ---------------------------------------------------------------------------

def test_assemble_recap_tight_format():
    parsed = {"lede": "Lede.", "bullets": ["Beat one", "Beat two", "Beat three"], "verdict": "Final word."}
    result = cs._assemble_recap(parsed, "Keisha Williams")
    assert isinstance(result, EmbedData)
    assert result.description == "Lede."
    field_names = [f.name for f in result.fields]
    assert field_names == ["Beat 1", "Beat 2", "Final Word"]
    assert result.fields[0].value == "Beat one"
    assert result.fields[1].value == "Beat two"
    # Only the first 2 bullets are used.
    assert not any("Beat three" in f.value for f in result.fields)
    assert result.fields[2].value == "Final word."
    assert result.footer == "Keisha Williams"


# ---------------------------------------------------------------------------
# _assemble_moment (B1 — revived as EmbedData; not wired into _RENDERERS)
# ---------------------------------------------------------------------------

def test_assemble_moment_full_shape():
    parsed = {"headline": "The Moment", "scene": "Scene setter.", "moment": "Play by play.", "meaning": "Why it matters."}
    result = cs._assemble_moment(parsed, "Maya Chen")
    assert isinstance(result, EmbedData)
    assert result.title == "The Moment"
    assert result.description == "*Scene setter.*"
    field_names = [f.name for f in result.fields]
    assert "The Play" in field_names
    assert "Why It Matters" in field_names
    assert next(f for f in result.fields if f.name == "The Play").value == "Play by play."
    assert next(f for f in result.fields if f.name == "Why It Matters").value == "Why it matters."


def test_assemble_moment_headline_only_fallback():
    result = cs._assemble_moment({"headline": "The Moment"}, "Maya Chen")
    assert len(result.fields) == 1
    assert "Maya Chen on The Moment." in result.fields[0].value


# ---------------------------------------------------------------------------
# _assemble_verdict (B1 — revived as EmbedData; not wired into _RENDERERS)
# ---------------------------------------------------------------------------

def test_assemble_verdict_full_shape():
    parsed = {
        "headline": "The Verdict", "case": "Case text.", "argument": "Argument text.",
        "receipts": ["Receipt one"], "verdict": "Guilty.",
    }
    result = cs._assemble_verdict(parsed, "Jordan Rivera")
    assert isinstance(result, EmbedData)
    assert result.title == "The Verdict"
    assert result.description == "Argument text."
    field_names = [f.name for f in result.fields]
    assert "⚖️ The Case" in field_names
    assert "The Receipts" in field_names
    assert "🔨 VERDICT" in field_names
    assert next(f for f in result.fields if f.name == "The Receipts").value == "• Receipt one"
    assert next(f for f in result.fields if f.name == "🔨 VERDICT").value == "Guilty."


def test_assemble_verdict_headline_only_fallback():
    result = cs._assemble_verdict({"headline": "The Verdict"}, "Jordan Rivera")
    assert len(result.fields) == 1
    assert "verdict is in on The Verdict" in result.fields[0].value


# ---------------------------------------------------------------------------
# _assemble_index (B1 — revived as EmbedData; not wired into _RENDERERS)
# ---------------------------------------------------------------------------

def test_assemble_index_full_shape():
    parsed = {
        "headline": "The Index", "metric_name": "Efficiency", "headline_value": "112.5",
        "definition": "Def text.", "standouts": [{"name": "Player X", "value": "30 PTS", "note": "career high"}],
        "implication": "This matters.",
    }
    result = cs._assemble_index(parsed, "Keisha Williams")
    assert isinstance(result, EmbedData)
    assert result.title == "The Index"
    assert "Efficiency" in result.description
    assert "112.5" in result.description
    assert "Def text." in result.description
    field_names = [f.name for f in result.fields]
    assert "Player X" in field_names
    assert "Why It Matters" in field_names
    standout_field = next(f for f in result.fields if f.name == "Player X")
    assert "30 PTS" in standout_field.value
    assert "career high" in standout_field.value
    # Single standout -> not inline (only 2+ standouts sit side by side).
    assert standout_field.inline is False
    assert next(f for f in result.fields if f.name == "Why It Matters").value == "This matters."


def test_assemble_index_multiple_standouts_are_inline():
    parsed = {
        "headline": "The Index",
        "standouts": [
            {"name": "Player X", "value": "30 PTS"},
            {"name": "Player Y", "value": "12 AST"},
        ],
    }
    result = cs._assemble_index(parsed, "Keisha Williams")
    standout_fields = [f for f in result.fields if f.name in ("Player X", "Player Y")]
    assert len(standout_fields) == 2
    assert all(f.inline for f in standout_fields)


def test_assemble_index_headline_only_fallback():
    result = cs._assemble_index({"headline": "The Index"}, "Keisha Williams")
    assert len(result.fields) == 1
    assert "numbers tell the story on The Index" in result.fields[0].value


# ---------------------------------------------------------------------------
# _truncate_field / _truncate_text (B1 safety helpers)
# ---------------------------------------------------------------------------

def test_truncate_field_under_limit_joins_unchanged():
    lines = ["• one", "• two"]
    assert cs._truncate_field(lines) == "• one\n• two"


def test_truncate_field_over_limit_drops_trailing_lines():
    lines = [f"• line {i} " + "x" * 100 for i in range(20)]
    result = cs._truncate_field(lines, limit=300)
    assert len(result) <= 300 + len("\n…(20 more)")
    assert "more)" in result


def test_truncate_text_under_limit_unchanged():
    assert cs._truncate_text("short text") == "short text"


def test_truncate_text_over_limit_truncates_with_ellipsis():
    long_text = "x" * 2000
    result = cs._truncate_text(long_text, limit=100)
    assert len(result) == 100
    assert result.endswith("…")


# ---------------------------------------------------------------------------
# _parse_trade_body / _parse_marcus_cole_body / _parse_potm_body
# ---------------------------------------------------------------------------

def test_parse_trade_body_with_sentinels():
    body = "[FRAMING] Framing text. [ANALYSIS] Analysis text."
    framing, analysis = cs._parse_trade_body(body)
    assert framing == "Framing text."
    assert analysis == "Analysis text."


def test_parse_trade_body_no_sentinels_returns_raw_as_framing():
    framing, analysis = cs._parse_trade_body("Just plain text.")
    assert framing == "Just plain text."
    assert analysis == ""


def test_parse_marcus_cole_body_with_sentinels():
    body = "[TEAM_A] A gets better. [TEAM_B] B takes a risk."
    a, b = cs._parse_marcus_cole_body(body)
    assert a == "A gets better."
    assert b == "B takes a risk."


def test_parse_marcus_cole_body_no_sentinels_returns_empty():
    a, b = cs._parse_marcus_cole_body("No markers here.")
    assert a == ""
    assert b == ""


def test_parse_potm_body_with_sentinels():
    body = "[EAST] East take. [WEST] West take. [CLOSER] Closing line."
    east, west, closer = cs._parse_potm_body(body)
    assert east == "East take."
    assert west == "West take."
    assert closer == "Closing line."


def test_parse_potm_body_no_sentinels_returns_raw_body_as_east():
    east, west, closer = cs._parse_potm_body("Just plain text.")
    assert east == "Just plain text."
    assert west == ""
    assert closer == ""


# ---------------------------------------------------------------------------
# _assemble_trade_report
# ---------------------------------------------------------------------------

def test_assemble_trade_report_new_scheme_with_teams():
    parsed = {"headline": "H", "body": "[TEAM_A] A's take. [TEAM_B] B's take."}
    ctx = {
        "teams": [
            {"name": "Lakers", "gets": [{"type": "player", "name": "Star Guy", "position": "PG", "age": 27, "ovr": 88}]},
            {"name": "Celtics", "gets": [{"type": "pick", "name": "2027 1st", "via": "LAL"}]},
        ]
    }
    result = cs._assemble_trade_report(parsed, "Marcus Cole", ctx=ctx)
    assert "Lakers** receives" in result
    assert "Star Guy (PG, 27, OVR 88)" in result
    assert "A's take." in result
    assert "Celtics** receives" in result
    assert "2027 1st (via LAL)" in result
    assert "B's take." in result


def test_assemble_trade_report_legacy_scheme_framing_analysis():
    parsed = {"headline": "H", "body": "[FRAMING] Framing here. [ANALYSIS] Analysis here."}
    ctx = {"teams": [{"name": "Lakers", "gets": []}]}
    result = cs._assemble_trade_report(parsed, "Marcus Cole", ctx=ctx)
    assert "Framing here." in result
    assert "Analysis here." in result
    assert "*(nothing)*" in result


def test_assemble_trade_report_includes_grades():
    parsed = {"headline": "H", "body": "No markers.", "grade_a": "A", "grade_b": "B"}
    ctx = {"teams": [{"name": "Lakers", "gets": []}, {"name": "Celtics", "gets": []}]}
    result = cs._assemble_trade_report(parsed, "Marcus Cole", ctx=ctx)
    assert "**Grade:**" in result
    assert "**Lakers:** A" in result
    assert "**Celtics:** B" in result


# ---------------------------------------------------------------------------
# _assemble_potm
# ---------------------------------------------------------------------------

def test_assemble_potm_full_shape():
    parsed = {"headline": "H", "body": "[EAST] East blurb. [WEST] West blurb. [CLOSER] Closer text."}
    ctx = {
        "month_label": "January 2026",
        "east_winner": {"player": "East Star", "team": "BOS", "ppg": 28.5, "rpg": 7.0, "apg": 6.0, "games": 15},
        "west_winner": {"player": "West Star", "team": "LAL", "ppg": 30.0, "rpg": 5.0, "apg": 8.0, "games": 14},
    }
    result = cs._assemble_potm(parsed, "Pat Chen", ctx=ctx)
    assert "January 2026" in result
    assert "EAST — East Star" in result
    assert "East blurb." in result
    assert "WEST — West Star" in result
    assert "West blurb." in result
    assert "Closer text." in result


def test_assemble_potm_body_fallback_when_sentinels_missing():
    parsed = {"headline": "H", "body": "Just plain prose, no markers."}
    ctx = {"east_winner": {"player": "East Star", "team": "BOS"}}
    result = cs._assemble_potm(parsed, "Pat Chen", ctx=ctx)
    assert "Just plain prose, no markers." in result


# ---------------------------------------------------------------------------
# _assemble_article (dispatcher)
# ---------------------------------------------------------------------------

def test_assemble_article_dispatches_to_analytics():
    parsed = {"lede": "L", "key_stats": [], "bullets": [], "verdict": "V"}
    result = cs._assemble_article(parsed, "Persona", format_style="analytics")
    assert "**Bottom line:** V" in result


def test_assemble_article_unrecognised_style_falls_back_to_default():
    parsed = {"headline": "H", "body": "Body."}
    result = cs._assemble_article(parsed, "Persona", format_style="not_a_real_style")
    assert result == "Body.\n\n— *Persona*"


def test_assemble_article_potm_passes_ctx():
    parsed = {"headline": "H", "body": "[EAST] E [WEST] W [CLOSER] C"}
    ctx = {"month_label": "Jan"}
    result = cs._assemble_article(parsed, "Persona", format_style="potm", ctx=ctx)
    assert "Jan" in result


