"""
TB3 -- trade_block_team_embed/trade_block_league_embed rendered every
asking_price with a hardcoded "$" prefix and thousands-separator, whether the
entry came from a human /block add (a real dollar figure) or from
cpu_block_service.refresh_team_block (an unscaled player_trade_value score,
post-TB2). Fix: gate the asking-price line on team.manager_user_id is not
None (human-managed team) so CPU-generated entries show just the player +
note, no price line. See docs/design/trade-block-logic-rules.md TB3.
"""
from __future__ import annotations

from bot.embeds.trade_embeds import trade_block_league_embed, trade_block_team_embed
from data.repositories.team_repo import Team


def _make_team(
    team_id: int,
    manager_user_id: int | None,
    code: str = "TST",
    city: str = "City",
    name: str = "Team",
) -> Team:
    return Team(
        id=team_id,
        league_id=1,
        nba_team_code=code,
        name=name,
        city=city,
        conference="East",
        division="Atlantic",
        manager_user_id=manager_user_id,
        cpu_mode="developing",
        team_offense_rating=None,
        team_defense_rating=None,
        pace=None,
    )


def test_team_embed_cpu_team_hides_asking_price_but_keeps_note():
    cpu_team = _make_team(1, manager_user_id=None)
    entries = [
        {"player_id": 100, "asking_price": 27, "note": "Rebuilding — veteran asset available"},
    ]
    players_by_id = {100: {"full_name": "Jane Doe", "overall": 75}}

    embed = trade_block_team_embed(cpu_team, entries, players_by_id)

    assert "$" not in embed.description
    assert "asking" not in embed.description
    assert "Rebuilding — veteran asset available" in embed.description
    assert "Jane Doe" in embed.description


def test_team_embed_human_team_keeps_formatted_asking_price():
    human_team = _make_team(2, manager_user_id=555)
    entries = [
        {"player_id": 200, "asking_price": 18_000_000, "note": None},
    ]
    players_by_id = {200: {"full_name": "John Smith", "overall": 88}}

    embed = trade_block_team_embed(human_team, entries, players_by_id)

    assert "— asking $18,000,000" in embed.description


def test_league_embed_mixed_cpu_and_human_teams():
    cpu_team = _make_team(1, manager_user_id=None, code="CPU", city="Rebuild", name="Tankers")
    human_team = _make_team(2, manager_user_id=555, code="HUM", city="Human", name="Managers")
    entries_by_team = {
        1: [{"player_id": 100, "asking_price": 27, "note": "Rebuilding — veteran asset available"}],
        2: [{"player_id": 200, "asking_price": 18_000_000, "note": None}],
    }
    teams_by_id = {1: cpu_team, 2: human_team}
    players_by_id = {
        100: {"full_name": "Jane Doe", "overall": 75},
        200: {"full_name": "John Smith", "overall": 88},
    }

    embed = trade_block_league_embed(entries_by_team, teams_by_id, players_by_id)

    fields_by_name = {f.name: f.value for f in embed.fields}
    cpu_field = fields_by_name[cpu_team.full_name]
    human_field = fields_by_name[human_team.full_name]

    assert "$" not in cpu_field
    assert "Jane Doe" in cpu_field
    assert "Rebuilding — veteran asset available" in cpu_field

    assert "— $18,000,000" in human_field
    assert "John Smith" in human_field


def test_asking_price_none_still_omits_price_line_for_both_team_types():
    cpu_team = _make_team(1, manager_user_id=None)
    human_team = _make_team(2, manager_user_id=555)
    entries_no_price = [{"player_id": 300, "asking_price": None, "note": None}]
    players_by_id = {300: {"full_name": "No Price Guy", "overall": 60}}

    cpu_embed = trade_block_team_embed(cpu_team, entries_no_price, players_by_id)
    human_embed = trade_block_team_embed(human_team, entries_no_price, players_by_id)

    assert "$" not in cpu_embed.description
    assert "asking" not in cpu_embed.description
    assert "$" not in human_embed.description
    assert "asking" not in human_embed.description
