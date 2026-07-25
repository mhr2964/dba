"""
RO10 -- rollover_complete_embed used to read summary["players_progressed"],
a key removed from rollover_service.run_rollover's return dict in a prior
refactor, causing an uncaught KeyError on every real /offseason rollover
invocation. This is a plain unit test (no DB) against a dict shaped exactly
like the real current run_rollover return value.
"""
from __future__ import annotations

from bot.embeds.history_embeds import rollover_complete_embed


def _real_shaped_summary() -> dict:
    """Matches services.rollover_service.run_rollover's actual return dict
    after RO1/RO3/RO6 (no players_progressed, no hof_inducted -- HOF
    induction is surfaced via progression_embeds instead, see RO3)."""
    return {
        "season_archived": 2025,
        "next_season": 2026,
        "contracts_expired": 3,
        "extensions_activated": 1,
        "picks_seeded": 1,
        "new_salary_cap": 144_200_000,
    }


def test_rollover_complete_embed_does_not_raise():
    embed = rollover_complete_embed(_real_shaped_summary())
    assert embed is not None


def test_rollover_complete_embed_contains_expected_fields():
    embed = rollover_complete_embed(_real_shaped_summary())

    field_names = {f.name for f in embed.fields}
    assert "Season Archived" in field_names
    assert "Next Season" in field_names
    assert "Contracts Expired" in field_names
    assert "Extensions Activated" in field_names
    assert "Picks Seeded" in field_names
    assert "New Salary Cap" in field_names

    field_values = {f.name: f.value for f in embed.fields}
    assert field_values["Season Archived"] == "2025"
    assert field_values["Next Season"] == "2026"
    assert field_values["New Salary Cap"] == "$144,200,000"


def test_rollover_complete_embed_footer_reflects_real_next_phase():
    embed = rollover_complete_embed(_real_shaped_summary())

    # The real next phase after rollover is PROGRESSION_PENDING (set by
    # run_rollover's UPDATE leagues SET current_phase = ...), not the stale
    # "PRESEASON_READY" this footer previously (and incorrectly) said.
    assert "PROGRESSION_PENDING" in embed.footer.text
    assert "PRESEASON_READY" not in embed.footer.text
