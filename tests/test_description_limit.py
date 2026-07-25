"""Characterization tests for _description_limit (Phase 1 fix A6) and
_embed_from_article (Phase 2 B1 follow-up).

Pins the per-category Discord embed description truncation ceiling that
replaced the old flat [:2000] slice used at every columnist post call site,
and the generic-vs-EmbedData branch _maybe_post_columnist/
_maybe_post_playoff_columnist now share for building the posted embed.
"""
from __future__ import annotations

from services.announcer_protocol import EmbedData, EmbedField
from services.sim_content_pipeline import _description_limit, _embed_from_article


def test_sunday_column_keeps_the_generous_long_form_ceiling():
    assert _description_limit("sunday_column") == 2000


def test_short_form_categories_get_the_tighter_default_ceiling():
    for category in (
        "power_rankings", "rookie_watch", "front_office_grade", "award_race",
        "injury_report", "game_recap", "tank_watch", "series_preview",
        "playoff_recap", "player_of_the_month", "coaching_beat",
    ):
        limit = _description_limit(category)
        assert limit == 800, f"{category} expected 800, got {limit}"
        assert limit < 2000


def test_unknown_category_falls_back_to_default_not_the_old_2000():
    assert _description_limit("some_new_category") == 800


class _FakePersona:
    def __init__(self, display_name, byline):
        self.display_name = display_name
        self.byline = byline


def test_embed_from_article_without_embed_data_uses_description_slice():
    """No 'embed_data' key (every persona except Keisha Williams) -- same
    description=body[:limit] shape as before this fix."""
    article = {"headline": "Lakers Win Big", "body": "A" * 900}
    persona = _FakePersona("Jordan Rivera", "The Reaction")
    embed = _embed_from_article(article, persona, (100, 100, 100), "game_recap")
    assert embed.title == "Lakers Win Big"
    assert embed.description == "A" * 800  # game_recap's 800-char ceiling
    assert embed.footer == "by Jordan Rivera · The Reaction"


def test_embed_from_article_with_embed_data_reuses_fields_overrides_title_and_footer():
    """A persona dispatched through _EMBED_RENDERERS (Keisha Williams/'index')
    carries a real EmbedData under 'embed_data' -- its description/fields are
    kept verbatim; only title/color/footer are overridden to match this call
    site's usual conventions (the renderer doesn't know the persona's byline
    or the site's color table)."""
    embed_data = EmbedData(
        title="placeholder-from-renderer",
        description="NET RATING\n+21.4",
        fields=[EmbedField(name="Marcus Davis", value="68% TS", inline=True)],
        footer="Keisha Williams",  # _assemble_index's own footer= is just persona_display
    )
    article = {"headline": "Net Rating Tells the Real Story", "body": "flattened text", "embed_data": embed_data}
    persona = _FakePersona("Keisha Williams", "The Index — DBA Stats Desk")
    embed = _embed_from_article(article, persona, (0, 128, 255), "game_recap")

    assert embed.title == "Net Rating Tells the Real Story"
    assert embed.footer == "by Keisha Williams · The Index — DBA Stats Desk"
    assert embed.color == (0 << 16) | (128 << 8) | 255
    # Description/fields come through untouched from the renderer.
    assert embed.description == "NET RATING\n+21.4"
    assert embed.fields == [EmbedField(name="Marcus Davis", value="68% TS", inline=True)]


def test_embed_from_article_none_persona_gives_none_footer():
    article = {"headline": "Rerolled", "body": "..."}
    embed = _embed_from_article(article, None, (100, 100, 100), "game_recap")
    assert embed.footer is None
