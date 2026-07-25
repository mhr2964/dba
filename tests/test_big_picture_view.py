"""Tests for BigPictureExpandView (B3) -- the "Read full column" expand button.

Verifies the swap-in-place behavior: clicking the button edits the message to
the full embed, disables the button, and on_timeout disables the button on
whatever message it was last attached to.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bot.ui.big_picture_view import BigPictureExpandView
from services.announcer_protocol import EmbedData


def _full_embed_data() -> EmbedData:
    return EmbedData(
        title="Two-Tier League",
        description="The full column text, including the Case Study and What It Means.",
        footer="by The Big Picture",
    )


async def test_expand_button_edits_message_to_full_embed_and_disables():
    view = BigPictureExpandView(_full_embed_data())
    # view.expand is the real discord.ui.Button item (the decorator's callback
    # binds self/button for us) -- .callback(interaction) is how discord.py
    # itself invokes it on a real press.
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.message = MagicMock()

    await view.expand.callback(interaction)

    interaction.response.edit_message.assert_awaited_once()
    _args, kwargs = interaction.response.edit_message.call_args
    assert kwargs["embed"].title == "Two-Tier League"
    assert kwargs["embed"].description == view._full_embed.description
    assert kwargs["view"] is view
    assert view.expand.disabled is True
    assert view.expand.label == "Full column"
    assert view.message is interaction.message


async def test_on_timeout_disables_button_and_edits_message_when_present():
    view = BigPictureExpandView(_full_embed_data())
    fake_message = MagicMock()
    fake_message.edit = AsyncMock()
    view.message = fake_message

    await view.on_timeout()

    assert view.expand.disabled is True
    fake_message.edit.assert_awaited_once_with(view=view)


async def test_on_timeout_noop_without_a_message():
    view = BigPictureExpandView(_full_embed_data())
    # message is None (never posted/interacted) -- must not raise.
    await view.on_timeout()
    assert view.expand.disabled is True
