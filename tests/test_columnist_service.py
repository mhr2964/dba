"""
Characterization + new-behavior tests for columnist_service.generate().

Covers the grounding-check retry loop added for Finding #1 (no fact-checking
of LLM output): a well-formed draft with a numeric claim that doesn't match
anything in the context dict should trigger exactly one regeneration attempt,
and the article that eventually gets posted/persisted should be whichever
draft's claims are grounded (or the second draft, if grounding never clears).
Also pins the pre-existing happy path (single grounded draft, one API call)
so the retry logic doesn't regress normal behavior.

No real Anthropic API calls are made — anthropic.AsyncAnthropic is patched
with a fake client whose messages.create() is scripted per test.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services import columnist_service
from services.personas.base import Persona

# asyncio_mode = auto (pytest.ini) already covers the async tests below --
# no blanket pytestmark, since this file also has plain sync tests for the
# pure-function helpers (_count_words, _resolve_word_target).


def _persona(**overrides) -> Persona:
    defaults = dict(
        id="test_persona",
        display_name="Test Persona",
        byline="Test Desk",
        avatar_emoji="🧪",
        voice_notes="Test voice notes.",
        categories=("game_recap",),
    )
    defaults.update(overrides)
    return Persona(**defaults)


def _fake_message(text: str):
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


def _fake_client(*texts: str):
    """Return a fake anthropic.AsyncAnthropic() instance yielding `texts` in
    order across successive messages.create() calls (one per API call)."""
    client = MagicMock()
    responses = [_fake_message(t) for t in texts]
    client.messages.create = AsyncMock(side_effect=responses)
    return client


async def _run_generate(persona, context, texts, **kwargs):
    fake_anthropic_module = SimpleNamespace(
        AsyncAnthropic=MagicMock(return_value=_fake_client(*texts))
    )
    pool = MagicMock()
    insert_calls = []

    async def _fake_insert(pool_, **insert_kwargs):
        insert_calls.append(insert_kwargs)
        return 1

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.dict("services.personas.PERSONAS", {persona.id: persona}, clear=True),
        patch("services.columnist_ride_along.should_fire_for", return_value=True),
        patch("data.repositories.article_repo.insert", _fake_insert),
        patch.dict("sys.modules", {"anthropic": fake_anthropic_module}),
    ):
        result = await columnist_service.generate(
            pool, league_id=1, season=2025,
            persona_id=persona.id, category="game_recap", context=context,
            **kwargs,
        )
    return result, insert_calls


async def test_happy_path_grounded_draft_posts_on_first_attempt():
    """A draft whose only numeric claim matches context should NOT retry —
    exactly one API call, one DB insert."""
    persona = _persona()
    context = {"team": "OKC", "win_streak": 8}
    body_json = json.dumps({
        "headline": "OKC Riding an 8-Game Win Streak",
        "lede": "Thunder win their 8th straight.",
        "key_stats": [{"label": "Win streak", "value": "8 straight"}],
        "bullets": ["OKC has won 8 straight games."],
        "verdict": "The streak continues.",
    })
    result, insert_calls = await _run_generate(persona, context, [body_json])

    assert result is not None
    assert "8 straight" in result["body"] or "8" in result["body"]
    assert len(insert_calls) == 1


async def test_ungrounded_percentage_triggers_one_retry_then_posts_grounded_draft():
    """First draft invents a 61.4% claim not in context; second (retried) draft
    is grounded. Exactly 2 API calls, exactly 1 DB insert (the grounded one)."""
    persona = _persona()
    context = {"team": "OKC", "fgm": 40, "fga": 80}  # real FG% = 50.0
    bad_body = json.dumps({
        "headline": "OKC Shoots a Blistering 61.4% From the Field",
        "lede": "Thunder torched the nets at 61.4% shooting.",
        "key_stats": [{"label": "FG%", "value": "61.4%"}],
        "bullets": ["A 61.4% shooting night is elite."],
        "verdict": "Efficiency won the night.",
    })
    good_body = json.dumps({
        "headline": "OKC Shoots an Even 50% From the Field",
        "lede": "Thunder shot 50% from the field.",
        "key_stats": [{"label": "FG%", "value": "50.0%"}],
        "bullets": ["A 50% shooting night is solid."],
        "verdict": "Efficiency won the night.",
    })
    result, insert_calls = await _run_generate(persona, context, [bad_body, good_body])

    assert result is not None
    assert "50" in result["headline"]
    assert len(insert_calls) == 1
    assert "50" in insert_calls[0]["headline"]


async def test_ungrounded_claim_persists_after_retry_still_posts():
    """Both attempts invent an ungrounded percentage — grounding never clears,
    but the article still posts (graceful fallback, not a silent drop) using
    the second attempt's content, after exactly 2 API calls."""
    persona = _persona()
    context = {"team": "OKC", "fgm": 40, "fga": 80}  # real FG% = 50.0
    bad_body_1 = json.dumps({
        "headline": "OKC Shoots a Blistering 61.4% From the Field",
        "lede": "Thunder torched the nets.",
        "key_stats": [{"label": "FG%", "value": "61.4%"}],
        "bullets": ["A 61.4% shooting night is elite."],
        "verdict": "Efficiency won the night.",
    })
    bad_body_2 = json.dumps({
        "headline": "OKC Shoots a Ridiculous 72.9% From the Field",
        "lede": "Thunder torched the nets even harder.",
        "key_stats": [{"label": "FG%", "value": "72.9%"}],
        "bullets": ["A 72.9% shooting night is absurd."],
        "verdict": "Efficiency won the night.",
    })
    result, insert_calls = await _run_generate(persona, context, [bad_body_1, bad_body_2])

    assert result is not None
    assert "72.9" in result["headline"]
    assert len(insert_calls) == 1


async def test_resolve_max_tokens_short_form_category():
    """A1: rookie_watch states '~80 words total' in its own voice_notes — gets
    the tightest tier, well below the old flat 1400."""
    assert columnist_service._resolve_max_tokens("rookie_watch") == 500


async def test_resolve_max_tokens_long_form_category_keeps_high_budget():
    """A1: sunday_column (Big Picture) is the one long-form category that keeps
    the prior generous ceiling."""
    assert columnist_service._resolve_max_tokens("sunday_column") == 1400


async def test_resolve_max_tokens_unknown_category_uses_medium_default():
    assert columnist_service._resolve_max_tokens("some_new_category") == 1000


async def test_generate_uses_per_category_max_tokens_in_api_call():
    """The actual messages.create() call must receive the category's resolved
    budget, not a flat constant — pins A1 end-to-end through generate()."""
    persona = _persona()
    context = {"team": "OKC"}
    body_json = json.dumps({
        "headline": "OKC Wins",
        "lede": "Thunder win.",
        "key_stats": [{"label": "Score", "value": "100-90"}],
        "bullets": ["OKC led wire to wire."],
        "verdict": "Statement win.",
    })

    fake_anthropic_module = SimpleNamespace(
        AsyncAnthropic=MagicMock(return_value=_fake_client(body_json))
    )
    pool = MagicMock()

    async def _fake_insert(pool_, **insert_kwargs):
        return 1

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.dict("services.personas.PERSONAS", {persona.id: persona}, clear=True),
        patch("services.columnist_ride_along.should_fire_for", return_value=True),
        patch("data.repositories.article_repo.insert", _fake_insert),
        patch.dict("sys.modules", {"anthropic": fake_anthropic_module}),
    ):
        await columnist_service.generate(
            pool, league_id=1, season=2025,
            persona_id=persona.id, category="rookie_watch", context=context,
        )

    fake_client = fake_anthropic_module.AsyncAnthropic.return_value
    _, call_kwargs = fake_client.messages.create.call_args
    assert call_kwargs["max_tokens"] == 500


async def test_narrative_rule_included_for_sunday_column_and_game_recap():
    """A2: the 'zoom out to team and league context' rule should reach the
    system prompt only for the two wide-angle categories."""
    persona = _persona(voice_notes="Tight single-focus voice.")
    for category in ("sunday_column", "game_recap"):
        captured: dict = {}
        body_json = json.dumps({
            "headline": "H", "lede": "L", "key_stats": [], "bullets": ["b"], "verdict": "V",
        })
        fake_anthropic_module = SimpleNamespace(
            AsyncAnthropic=MagicMock(return_value=_fake_client(body_json))
        )
        pool = MagicMock()

        async def _fake_insert(pool_, **insert_kwargs):
            return 1

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch.dict("services.personas.PERSONAS", {persona.id: persona}, clear=True),
            patch("services.columnist_ride_along.should_fire_for", return_value=True),
            patch("data.repositories.article_repo.insert", _fake_insert),
            patch.dict("sys.modules", {"anthropic": fake_anthropic_module}),
        ):
            await columnist_service.generate(
                pool, league_id=1, season=2025,
                persona_id=persona.id, category=category, context={},
                _capture_prompt=captured,
            )
        assert "NARRATIVE RULE" in captured["system"], f"expected NARRATIVE RULE for {category}"


async def test_narrative_rule_omitted_for_tight_single_focus_categories():
    """A2: single-focus categories (e.g. rookie_watch, award_race, hot_take)
    must NOT get the wide-angle instruction — it directly fights their
    voice_notes' brevity requirements."""
    persona = _persona(voice_notes="Tight single-focus voice.")
    for category in ("rookie_watch", "award_race", "hot_take", "power_rankings"):
        captured: dict = {}
        body_json = json.dumps({
            "headline": "H", "lede": "L", "key_stats": [], "bullets": ["b"], "verdict": "V",
        })
        fake_anthropic_module = SimpleNamespace(
            AsyncAnthropic=MagicMock(return_value=_fake_client(body_json))
        )
        pool = MagicMock()

        async def _fake_insert(pool_, **insert_kwargs):
            return 1

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch.dict("services.personas.PERSONAS", {persona.id: persona}, clear=True),
            patch("services.columnist_ride_along.should_fire_for", return_value=True),
            patch("data.repositories.article_repo.insert", _fake_insert),
            patch.dict("sys.modules", {"anthropic": fake_anthropic_module}),
        ):
            await columnist_service.generate(
                pool, league_id=1, season=2025,
                persona_id=persona.id, category=category, context={},
                _capture_prompt=captured,
            )
        assert "NARRATIVE RULE" not in captured["system"], f"unexpected NARRATIVE RULE for {category}"


def test_count_words_ignores_markdown_punctuation():
    text = "**Bold** > blockquote line ─────────── `code` | table | cell"
    # Words: Bold, blockquote, line, code, table, cell = 6
    assert columnist_service._count_words(text) == 6


def test_resolve_word_target_known_persona():
    assert columnist_service._resolve_word_target("rookie_watch") == 80


def test_resolve_word_target_unknown_persona_uses_default():
    assert columnist_service._resolve_word_target("some_new_persona") == 150


def test_resolve_word_target_pat_chen_matches_prompt_hard_cap():
    """A4: code-side target lowered from 130 to 120 to match the explicit
    "Hard cap: total body <=120 words" instruction now in pat_chen's voice_notes."""
    assert columnist_service._resolve_word_target("pat_chen") == 120


async def test_over_length_draft_triggers_one_retry_then_posts_trimmed_draft():
    """A3: a draft that blows past rookie_watch's 80-word target by >50% should
    trigger exactly one retry with a trim-to-N-words correction; the trimmed
    second draft is what gets posted."""
    persona = _persona(id="rookie_watch")
    context = {"team": "OKC"}
    long_words = " ".join(["word"] * 200)
    long_body = json.dumps({
        "headline": "Rookie Battle",
        "body": f"🥇 **A** — stats\n🥈 **B** — stats\n\n{long_words}",
    })
    short_body = json.dumps({
        "headline": "Rookie Battle",
        "body": "🥇 **A** — stats\n🥈 **B** — stats\n\nShort banter line.",
    })
    result, insert_calls = await _run_generate(persona, context, [long_body, short_body])

    assert result is not None
    assert "word word word" not in result["body"]
    assert len(insert_calls) == 1


async def test_length_never_exceeded_no_retry_single_api_call():
    """A3: a draft already within the target's overage threshold should NOT
    trigger a retry — exactly one API call."""
    persona = _persona(id="rookie_watch")
    context = {"team": "OKC"}
    body_json = json.dumps({
        "headline": "Rookie Battle",
        "body": "🥇 **A** — stats\n🥈 **B** — stats\n\nShort banter line.",
    })
    fake_anthropic_module = SimpleNamespace(
        AsyncAnthropic=MagicMock(return_value=_fake_client(body_json))
    )
    pool = MagicMock()

    async def _fake_insert(pool_, **insert_kwargs):
        return 1

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
        patch.dict("services.personas.PERSONAS", {persona.id: persona}, clear=True),
        patch("services.columnist_ride_along.should_fire_for", return_value=True),
        patch("data.repositories.article_repo.insert", _fake_insert),
        patch.dict("sys.modules", {"anthropic": fake_anthropic_module}),
    ):
        await columnist_service.generate(
            pool, league_id=1, season=2025,
            persona_id=persona.id, category="game_recap", context=context,
        )
    fake_client = fake_anthropic_module.AsyncAnthropic.return_value
    assert fake_client.messages.create.call_count == 1


async def test_still_over_length_after_retry_still_posts():
    """A3: both attempts blow past the target — grounding never clears, but the
    article still posts (graceful fallback) after exactly 2 API calls."""
    persona = _persona(id="rookie_watch")
    context = {"team": "OKC"}
    long_words_1 = " ".join(["word"] * 200)
    long_words_2 = " ".join(["term"] * 200)
    bad_1 = json.dumps({"headline": "Rookie Battle", "body": f"Intro. {long_words_1}"})
    bad_2 = json.dumps({"headline": "Rookie Battle", "body": f"Intro. {long_words_2}"})
    result, insert_calls = await _run_generate(persona, context, [bad_1, bad_2])

    assert result is not None
    assert "term term term" in result["body"]
    assert len(insert_calls) == 1


async def test_old_shape_headline_body_also_gets_grounding_checked():
    """The legacy {"headline","body"} shape (no 'lede') goes through the same
    grounding gate as the structured shape."""
    persona = _persona()
    context = {"team": "OKC", "win_streak": 5}
    bad = json.dumps({
        "headline": "OKC Extends Its Streak to 9 Straight",
        "body": "The Thunder have now won 9 straight games, a franchise best.",
    })
    good = json.dumps({
        "headline": "OKC Extends Its Streak to 5 Straight",
        "body": "The Thunder have now won 5 straight games.",
    })
    result, insert_calls = await _run_generate(persona, context, [bad, good])

    assert result is not None
    assert "5 Straight" in result["headline"]
    assert len(insert_calls) == 1
