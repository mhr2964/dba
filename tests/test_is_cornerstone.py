"""
Unit tests for cpu_trade_posture.is_cornerstone.

No prior test coverage existed for this function — the first block below is a
characterization of its CURRENT (pre-#10-fix) behavior using the static
team.cpu_mode column. The second block covers the new live_mode override (#10:
is_cornerstone previously always read team.cpu_mode directly, bypassing the
B7 live-posture fix every other gate already uses).
"""
from __future__ import annotations

from services.cpu_trade_posture import is_cornerstone


class _FakeTeam:
    def __init__(self, cpu_mode: str = "developing"):
        self.cpu_mode = cpu_mode


class _FakePlayer:
    def __init__(self, player_id: int, overall: int):
        self.id = player_id
        self.overall = overall


def _roster(*overalls: int) -> list[_FakePlayer]:
    return [_FakePlayer(i, ovr) for i, ovr in enumerate(overalls)]


# ---------------------------------------------------------------------------
# Characterization: static team.cpu_mode (pre-existing behavior, live_mode=None)
# ---------------------------------------------------------------------------


def test_ovr_92_always_untouchable_regardless_of_mode():
    team = _FakeTeam(cpu_mode="rebuilding")
    roster = _roster(92, 70, 65)
    player = roster[0]  # the OVR-92 top player
    assert is_cornerstone(team, player, roster) is True


def test_rebuilding_team_does_not_protect_sub_92_top_player():
    team = _FakeTeam(cpu_mode="rebuilding")
    roster = _roster(89, 70, 65)
    player = roster[0]  # top player by OVR, but below the 92 hard floor
    assert is_cornerstone(team, player, roster) is False


def test_contending_top_player_ovr_88_is_untouchable():
    team = _FakeTeam(cpu_mode="contending")
    roster = _roster(88, 80, 75)
    player = roster[0]  # top player by OVR
    assert is_cornerstone(team, player, roster) is True


def test_contending_top_player_ovr_86_is_untouchable():
    team = _FakeTeam(cpu_mode="contending")
    roster = _roster(86, 80, 75)
    player = roster[0]  # top player by OVR
    assert is_cornerstone(team, player, roster) is True


def test_developing_top_player_ovr_86_not_protected():
    """OVR 86-87 protection only applies in contending/play_in_fringe modes."""
    team = _FakeTeam(cpu_mode="developing")
    roster = _roster(86, 80, 75)
    player = roster[0]  # top player by OVR
    assert is_cornerstone(team, player, roster) is False


def test_non_top_player_never_protected_below_92():
    team = _FakeTeam(cpu_mode="contending")
    roster = _roster(90, 88, 75)
    player = roster[1]  # #2 by OVR, not #1 — not protected regardless of OVR<92
    assert is_cornerstone(team, player, roster) is False


# ---------------------------------------------------------------------------
# New (#10): live_mode override takes precedence over team.cpu_mode
# ---------------------------------------------------------------------------


def test_live_mode_overrides_stale_static_column_to_protect():
    """team.cpu_mode says 'developing' (would NOT protect an OVR-86 top player),
    but live_mode says 'contending' — live_mode must win, protecting the player.
    This is the exact staleness scenario #10 describes: static column lags the
    live posture the rest of the trade pipeline already uses."""
    team = _FakeTeam(cpu_mode="developing")
    roster = _roster(86, 80, 75)
    player = roster[0]  # top player by OVR
    assert is_cornerstone(team, player, roster, live_mode="developing") is False
    assert is_cornerstone(team, player, roster, live_mode="contending") is True


def test_live_mode_overrides_stale_static_column_to_unprotect():
    """team.cpu_mode says 'contending' (would protect), but live_mode says
    'rebuilding' — live_mode must win, and only the true rebuild exception
    (OVR >= 92) still applies."""
    team = _FakeTeam(cpu_mode="contending")
    roster = _roster(88, 80, 75)
    player = roster[0]  # top player by OVR
    assert is_cornerstone(team, player, roster, live_mode="contending") is True
    assert is_cornerstone(team, player, roster, live_mode="rebuilding") is False


def test_live_mode_none_falls_back_to_static_column():
    """Omitting live_mode (existing call sites that haven't been updated)
    preserves the pre-#10 behavior exactly."""
    team = _FakeTeam(cpu_mode="contending")
    roster = _roster(86, 80, 75)
    player = roster[0]  # top player by OVR
    assert is_cornerstone(team, player, roster) is True
    assert is_cornerstone(team, player, roster, live_mode=None) is True
