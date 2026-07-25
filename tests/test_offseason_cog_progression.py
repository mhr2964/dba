"""
Live smoke test for the season off-by-one fix (P1) -- drives the ACTUAL
`/offseason progression` cog command (not progression_service.run_progression
called directly), the one path the plan flagged as needing end-to-end
verification since no unit test calling run_progression directly could ever
have caught the bug in the first place.

tests/test_rollover.py::test_rollover_then_progression_uses_correct_season
already proves the service-level handoff (run_rollover ->
league.pending_progression_season -> run_progression) is correct. This test
goes one layer further and proves the bot command itself reads that column,
falls back sanely, and clears it after success -- none of which the
service-level test touches.
"""
from __future__ import annotations

from datetime import datetime, timezone

from bot.cogs.offseason_cog import OffseasonGroup
from data.repositories import league_repo
from phase.states import Phase
from services import rollover_service


async def test_progression_command_uses_pending_progression_season(
    db_pool, mock_guild, mock_interaction
):
    league_row = await db_pool.fetchrow(
        """
        INSERT INTO leagues (
            discord_guild_id, name, start_season_year, current_season,
            current_phase, commissioner_user_id
        ) VALUES ($1, 'Progression Smoke League', 2025, 2025, 'REGULAR_SEASON_ACTIVE', $2)
        RETURNING id
        """,
        mock_guild.id,
        mock_interaction.user.id,
    )
    league_id: int = league_row["id"]

    team_row = await db_pool.fetchrow(
        """
        INSERT INTO teams (league_id, nba_team_code, name, city, conference, division)
        VALUES ($1, 'SMK', 'Smoke Testers', 'Testville', 'East', 'Atlantic')
        RETURNING id
        """,
        league_id,
    )
    team_id: int = team_row["id"]

    player_row = await db_pool.fetchrow(
        """
        INSERT INTO players (
            league_id, team_id, first_name, last_name, position,
            birth_date, years_pro, roster_status,
            overall, speed, shooting_2pt, shooting_3pt, shooting_mid,
            finishing, playmaking, defense, rebounding, iq,
            potential, peak_age_start, peak_age_end,
            loyalty, money_drive, win_drive
        ) VALUES (
            $1, $2, 'Smoke', 'Test', 'SF',
            '1987-01-01', 9, 'active',
            72, 65, 60, 55, 60,
            65, 60, 65, 65, 70,
            72, 26, 31,
            50, 50, 50
        )
        RETURNING id
        """,
        league_id,
        team_id,
    )
    player_id: int = player_row["id"]

    # Known low-minutes game log recorded under season 2025 -- the season
    # about to be rolled over, not the post-increment 2026.
    for i in range(12):
        game_row = await db_pool.fetchrow(
            """
            INSERT INTO games (
                league_id, season, season_type, game_index,
                home_team_id, away_team_id, scheduled_date,
                status, home_score, away_score, winner_team_id
            ) VALUES ($1, 2025, 'regular', $2, $3, $3, '2025-01-01', 'simmed', 100, 95, $3)
            RETURNING id
            """,
            league_id,
            i + 1,
            team_id,
        )
        await db_pool.execute(
            """
            INSERT INTO game_box_scores (game_id, player_id, team_id, minutes, points)
            VALUES ($1, $2, $3, 8, 4)
            """,
            game_row["id"],
            player_id,
            team_id,
        )

    await db_pool.execute(
        """
        INSERT INTO injuries (league_id, player_id, season, severity, games_missed)
        VALUES ($1, $2, 2025, 'season_ending', 40)
        """,
        league_id,
        player_id,
    )

    # Real rollover -- increments current_season to 2026 and sets
    # pending_progression_season = 2025, PROGRESSION_PENDING phase.
    await rollover_service.run_rollover(league_id)

    league_before = await league_repo.get_by_guild(db_pool, mock_guild.id)
    assert league_before.current_phase == Phase.PROGRESSION_PENDING.value
    assert league_before.pending_progression_season == 2025
    assert league_before.current_phase != "REGULAR_SEASON_ACTIVE"

    # safe_defer measures interaction age off created_at -- a bare MagicMock
    # fails the datetime subtraction/comparison, so give it a real timestamp.
    mock_interaction.created_at = datetime.now(timezone.utc)

    # The actual Discord command, via the same callback convention this
    # repo's other cog tests use (see tests/test_setup_cog.py).
    group = OffseasonGroup()
    await group.progression.callback(group, mock_interaction)

    # The command must have responded successfully, not with a failure message.
    # safe_respond picks edit_original_response vs. followup.send/send_message
    # based on interaction.response.is_done() -- a MagicMock's is_done() returns
    # a truthy (unawaited) coroutine either way, so check whichever response
    # method actually fired rather than assuming one specific path.
    responded_mocks = [
        mock_interaction.edit_original_response,
        mock_interaction.followup.send,
        mock_interaction.response.send_message,
    ]
    fired = [m for m in responded_mocks if m.await_count > 0]
    assert fired, "Progression command never sent any response"
    call_kwargs = fired[0].call_args.kwargs
    call_content = call_kwargs.get("content") or ""
    assert "failed" not in call_content.lower(), f"Progression command reported failure: {call_content!r}"

    reasons = await db_pool.fetch(
        "SELECT reason FROM progression_log WHERE league_id = $1 AND player_id = $2",
        league_id,
        player_id,
    )
    reason_set = {r["reason"] for r in reasons}
    assert "low_minutes" in reason_set, f"Expected low_minutes penalty via the real cog path, got {reason_set}"
    assert "injury_setback" in reason_set, f"Expected injury_setback penalty via the real cog path, got {reason_set}"

    league_after = await league_repo.get_by_guild(db_pool, mock_guild.id)
    assert league_after.pending_progression_season is None, (
        "pending_progression_season should be cleared after a successful "
        "progression run so a stale season number can't be reused"
    )
    assert league_after.current_phase == Phase.FA_OPEN.value
