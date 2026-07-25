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
# _marcus_cole_summary_text / _marcus_cole_asset_fields (B4)
# ---------------------------------------------------------------------------

def test_marcus_cole_summary_text_strips_asset_blocks_and_byline():
    parsed = {"headline": "H", "body": "[TEAM_A] Lakers get their guy. [TEAM_B] Celtics bank picks."}
    ctx = {
        "teams": [
            {"name": "Lakers", "gets": [{"type": "player", "name": "Star Guy", "ovr": 88}]},
            {"name": "Celtics", "gets": [{"type": "pick", "name": "2027 1st"}]},
        ]
    }
    body = cs._assemble_trade_report(parsed, "Marcus Cole", ctx=ctx)
    blurbs, grade_line = cs._marcus_cole_summary_text(body)
    assert "Lakers get their guy." in blurbs
    assert "Celtics bank picks." in blurbs
    assert ">" not in blurbs  # asset blockquote blocks are dropped
    assert "Marcus Cole" not in blurbs  # byline dropped
    assert grade_line == ""


def test_marcus_cole_summary_text_extracts_grade_line():
    parsed = {"headline": "H", "body": "[TEAM_A] take A. [TEAM_B] take B.", "grade_a": "A", "grade_b": "C+"}
    ctx = {"teams": [{"name": "Lakers", "gets": []}, {"name": "Celtics", "gets": []}]}
    body = cs._assemble_trade_report(parsed, "Marcus Cole", ctx=ctx)
    blurbs, grade_line = cs._marcus_cole_summary_text(body)
    assert "**Grade:**" not in blurbs
    assert "Lakers:** A" in grade_line
    assert "Celtics:** C+" in grade_line


def test_marcus_cole_summary_text_empty_body_returns_empty():
    blurbs, grade_line = cs._marcus_cole_summary_text("")
    assert blurbs == ""
    assert grade_line == ""


def test_marcus_cole_asset_fields_builds_one_field_per_team():
    teams = [
        {"name": "Lakers", "gets": [
            {"type": "player", "name": "Star Guy", "position": "PG", "age": 27, "ovr": 88},
        ]},
        {"name": "Celtics", "gets": [{"type": "pick", "name": "2027 1st", "via": "LAL"}]},
    ]
    fields = cs._marcus_cole_asset_fields(teams)
    assert len(fields) == 2
    assert fields[0].name == "🔄 Lakers receives"
    assert "Star Guy (PG, 27, OVR 88)" in fields[0].value
    assert fields[0].inline is True
    assert fields[1].name == "🔄 Celtics receives"
    assert "2027 1st (via LAL)" in fields[1].value


def test_marcus_cole_asset_fields_empty_gets_shows_placeholder():
    fields = cs._marcus_cole_asset_fields([{"name": "Lakers", "gets": []}])
    assert len(fields) == 1
    assert fields[0].value == "*(nothing)*"


def test_marcus_cole_asset_fields_empty_teams_returns_empty_list():
    assert cs._marcus_cole_asset_fields([]) == []
    assert cs._marcus_cole_asset_fields(None) == []


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


# ---------------------------------------------------------------------------
# B2 — list-style persona field parsers (_power_list_fields, _the_ledger_fields,
# _the_race_fields, _triage_report_fields, _rookie_watch_fields)
# ---------------------------------------------------------------------------
# Fixtures below are each persona's own _SHAPE example body from its persona
# module (power_list.py, the_ledger.py, the_race.py, triage_report.py,
# rookie_watch.py) -- representative of real LLM output, not synthetic text.

_POWER_LIST_BODY = (
    "> **1.** OKC ↑2 — five-game win streak, defense locked in\n"
    "> **2.** BOS — — still the class of the East\n"
    "> **3.** DEN ↓1 — Jokic doing Jokic things, but road record slipping\n"
    "> **4.** MIL ↑1 — won 4 of 5 despite Giannis missing a game\n"
    "> **5.** PHX NEW — healthy again and it shows\n"
    "> **6.** MEM ↓2 — young legs, never quit, but the losses are mounting\n"
    "> **7.** ATL ↑3 — three straight wins out of nowhere\n"
    "> **8.** CHI — — two losses to the lottery smells bad\n"
    "> **9.** TOR ↓1 — only thing keeping them alive is schedule\n"
    "> **10.** ORL ↓2 — lost three straight and it looks structural\n\n"
    "**Biggest mover:** ATL (↑3)"
)


def test_power_list_fields_two_rank_clusters_plus_mover():
    desc, fields = cs._power_list_fields(_POWER_LIST_BODY)
    assert desc == ""
    field_names = [f.name for f in fields]
    assert field_names == ["Ranks 1-5", "Ranks 6-10", "Biggest Mover"]
    ranks_1_5 = next(f for f in fields if f.name == "Ranks 1-5")
    assert ranks_1_5.value.count("\n") == 4  # 5 rows -> 4 newlines
    assert "OKC" in ranks_1_5.value
    assert "PHX" in ranks_1_5.value
    ranks_6_10 = next(f for f in fields if f.name == "Ranks 6-10")
    assert "MEM" in ranks_6_10.value
    assert "ORL" in ranks_6_10.value
    assert next(f for f in fields if f.name == "Biggest Mover").value == "ATL (↑3)"


def test_power_list_fields_falls_back_to_raw_body_when_unparseable():
    desc, fields = cs._power_list_fields("Something completely off-template.")
    assert len(fields) == 1
    assert fields[0].name == "Rankings"
    assert fields[0].value == "Something completely off-template."


def test_power_list_fields_empty_body_returns_no_fields():
    desc, fields = cs._power_list_fields("")
    assert fields == []


_LEDGER_BODY = (
    "*Window: Trade Deadline*\n\n"
    "```\n"
    "TEAM    | MOVE                          | GRADE\n"
    "──────  | ───────────────────────────── | ─────\n"
    "BOS     | Traded for Harden, freed cap  | A\n"
    "MIL     | Held at deadline, roster set  | B+\n"
    "ORL     | Waived veteran depth          | C-\n"
    "HOU     | Signed two G-League long shots| F\n"
    "```\n\n"
    "**The Verdict:** Boston is the only front office that knows what it's building. Everyone else is reacting."
)


def test_the_ledger_fields_one_field_per_graded_move():
    desc, fields = cs._the_ledger_fields(_LEDGER_BODY)
    assert desc == "Window: Trade Deadline"
    field_names = [f.name for f in fields]
    assert field_names == ["BOS — A", "MIL — B+", "ORL — C-", "HOU — F", "The Verdict"]
    assert next(f for f in fields if f.name == "BOS — A").value == "Traded for Harden, freed cap"
    assert "Boston is the only front office" in next(f for f in fields if f.name == "The Verdict").value


def test_the_ledger_fields_falls_back_to_raw_body_when_unparseable():
    desc, fields = cs._the_ledger_fields("No table here.")
    assert fields[0].name == "Grades"
    assert fields[0].value == "No table here."


_RACE_BODY = (
    "*MVP Race — Current Pulse*\n\n"
    "> 🥇 **Joel Embiid** — 34/12/4 last week on 62% TS; Philly is 8-2 in his last 10 and he's the reason\n"
    "> 🥈 **Giannis Antetokounmpo** — his case is winning percentage; MIL went 9-1 last month\n"
    "> 🥉 **Luka Doncic** — triple-double machine but the losses are piling up against him\n\n"
    "**Eliminated this week:** Trae Young — three bad turnover games ended his dark-horse run\n\n"
    "**Sleeper:** Ja Morant — if Memphis makes the 3 seed, voters will have to look twice"
)


def test_the_race_fields_one_field_per_candidate_plus_eliminated_and_sleeper():
    desc, fields = cs._the_race_fields(_RACE_BODY)
    assert desc == "MVP Race — Current Pulse"
    field_names = [f.name for f in fields]
    assert field_names == [
        "🥇 Joel Embiid", "🥈 Giannis Antetokounmpo", "🥉 Luka Doncic",
        "Eliminated This Week", "Sleeper",
    ]
    assert "34/12/4" in next(f for f in fields if f.name == "🥇 Joel Embiid").value
    assert "Trae Young" in next(f for f in fields if f.name == "Eliminated This Week").value
    assert "Ja Morant" in next(f for f in fields if f.name == "Sleeper").value


def test_the_race_fields_falls_back_to_raw_body_when_unparseable():
    desc, fields = cs._the_race_fields("No medals here.")
    assert fields[0].name == "Candidates"
    assert fields[0].value == "No medals here."


_TRIAGE_BODY = (
    "🩹 **Marcus Williams** (LAL) — out 14 games\n\n"
    "**Filling in:** Davis Nguyen slides into the starting two-guard slot — averaged 11.4 PPG off the bench\n\n"
    "**Impact:** LAL loses their best corner-three shooter; team eFG% drops ~3 pts without his gravity"
)


def test_triage_report_fields_status_filling_in_impact():
    desc, fields = cs._triage_report_fields(_TRIAGE_BODY)
    assert desc == ""
    field_names = [f.name for f in fields]
    assert field_names == ["🩹 Marcus Williams (LAL)", "Filling In", "Impact"]
    assert next(f for f in fields if f.name == "🩹 Marcus Williams (LAL)").value == "out 14 games"
    assert "Davis Nguyen" in next(f for f in fields if f.name == "Filling In").value
    assert "corner-three shooter" in next(f for f in fields if f.name == "Impact").value


def test_triage_report_fields_falls_back_to_raw_body_when_unparseable():
    desc, fields = cs._triage_report_fields("No injury marker here.")
    assert fields[0].name == "Injury Report"
    assert fields[0].value == "No injury marker here."


_ROOKIE_BODY = (
    "🥇 **Victor Wembanyama** (SAS) — 18.2 / 9.1 / 3.7 bpg\n"
    "🥈 **Zach Edey** (MEM) — 16.4 / 11.0 / 1.2 bpg\n\n"
    "Wemby on the gap, asked postgame: some banter line.\n\n"
    "**Posterize of the week:** Edey put Sarr on a milk carton in the 3rd."
)


def test_rookie_watch_fields_one_field_per_rookie_plus_posterize():
    desc, fields = cs._rookie_watch_fields(_ROOKIE_BODY)
    assert desc == "Wemby on the gap, asked postgame: some banter line."
    field_names = [f.name for f in fields]
    assert field_names == ["🥇 Victor Wembanyama (SAS)", "🥈 Zach Edey (MEM)", "Posterize Of The Week"]
    assert next(f for f in fields if f.name == "🥇 Victor Wembanyama (SAS)").value == "18.2 / 9.1 / 3.7 bpg"
    assert "milk carton" in next(f for f in fields if f.name == "Posterize Of The Week").value


def test_rookie_watch_fields_tolerates_missing_team_code_parens():
    """rookie_watch.py's own worked example omits the (TEAM) parens its FORMAT
    instructions require -- the parser must survive both shapes."""
    body_without_team_codes = (
        "🥇 **Victor Wembanyama** — 18.2 / 9.1 / 3.7 bpg\n"
        "🥈 **Zach Edey** — 16.4 / 11.0 / 1.2 bpg\n\n"
        "**Stat of the week:** Wemby's 18.2 PPG leads all rookies."
    )
    desc, fields = cs._rookie_watch_fields(body_without_team_codes)
    field_names = [f.name for f in fields]
    assert field_names == ["🥇 Victor Wembanyama", "🥈 Zach Edey", "Stat Of The Week"]


def test_rookie_watch_fields_falls_back_to_raw_body_when_unparseable():
    desc, fields = cs._rookie_watch_fields("No rookies mentioned here.")
    assert fields[0].name == "Rookies"
    assert fields[0].value == "No rookies mentioned here."
