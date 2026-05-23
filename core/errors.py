from typing import TYPE_CHECKING

import discord
from discord import app_commands
from core.logging import get_logger

if TYPE_CHECKING:
    from services.feedback_log import FeedbackContext

log = get_logger(__name__)

# Track interaction IDs that have already been handled to prevent double-dispatch.
_handled_interaction_ids: set[int] = set()


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = False) -> None:
    """Call defer() and swallow 404/rate-limit errors.

    Discord occasionally returns 404 Unknown Interaction if the 3-second
    window narrowly elapsed before defer() was sent. The interaction webhook
    token is still valid for followup.send() calls, so we log a warning and
    continue rather than propagating the exception and aborting the handler.
    """
    # Measure how old the interaction is by the time we try to defer.
    # Discord's 3-second ack window starts at interaction.created_at, so an
    # interaction arriving at >2.5s is doomed regardless of our handler speed.
    from datetime import datetime, timezone
    age_ms: float | None = None
    try:
        age_ms = (datetime.now(timezone.utc) - interaction.created_at).total_seconds() * 1000
    except Exception:
        pass

    try:
        await interaction.response.defer(ephemeral=ephemeral)
        if age_ms is not None and age_ms > 1500:
            log.warning(
                f"defer() OK but interaction {interaction.id} was already "
                f"{age_ms:.0f}ms old on arrival (3000ms window) — gateway lag"
            )
    except discord.InteractionResponded:
        # Already responded (e.g. legacy alias deferred before calling canonical handler).
        pass
    except (discord.NotFound, discord.HTTPException) as exc:
        age_str = f"{age_ms:.0f}ms" if age_ms is not None else "?"
        log.warning(
            f"defer() failed for interaction {interaction.id} ({exc}) — "
            f"age at defer attempt: {age_str} (3000ms window) — continuing"
        )


async def safe_respond(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    embeds: list[discord.Embed] | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
    feedback_context: "FeedbackContext | None" = None,
) -> None:
    """Send the first response to an interaction, editing the deferred
    placeholder if defer() was called, otherwise responding fresh.

    Goal: avoid the "DBA is thinking..." -> separate result message UX.
    The result appears in the SAME slot as the placeholder.

    Subsequent messages on the same interaction should still use
    interaction.followup.send() — this helper is only for the FIRST reply.

    Args mirror discord.py's response methods; pass only what you need.
    `ephemeral` is honored on the initial response path; if the interaction
    is already deferred, ephemerality was fixed by the defer() call and
    this parameter is ignored (Discord's API limitation).

    `feedback_context` (opt-in) anchors the sent message in `bot_message_log`
    so a user reply can be joined back to its league/sim/entity context.
    Only non-ephemeral sends are registered — Discord doesn't permit replies
    to ephemeral messages.
    """
    kwargs: dict = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if embeds is not None:
        kwargs["embeds"] = embeds
    if view is not None:
        kwargs["view"] = view

    send_ok = False
    try:
        if interaction.response.is_done():
            # Deferred — edit the placeholder in place
            await interaction.edit_original_response(**kwargs)
        else:
            # Not deferred — send a fresh response
            await interaction.response.send_message(ephemeral=ephemeral, **kwargs)
        send_ok = True
    except discord.NotFound as exc:
        # Expected when the invocation channel/message was deleted between defer and respond
        # (e.g. /admin purge-server destroys its own context). Caller is responsible for
        # any user-visible fallback; nothing actionable here.
        log.info(f"safe_respond: original interaction gone for {interaction.id} ({exc}) — continuing")
    except discord.HTTPException as exc:
        log.warning(f"safe_respond failed for interaction {interaction.id} ({exc}) — attempting fallback")
        # If the payload was too large (400/50035), the placeholder is still
        # showing "DBA is thinking…".  Resolve it to a plain error message so
        # the user isn't left staring at a spinner indefinitely.
        if exc.status == 400 and exc.code == 50035:
            try:
                await interaction.edit_original_response(
                    content="Result too large to display — check logs or narrow your query.",
                    embed=None,
                    embeds=None,
                    view=None,
                )
            except Exception as inner:
                log.warning(f"safe_respond fallback also failed for interaction {interaction.id} ({inner})")

    if send_ok and feedback_context is not None and not ephemeral:
        await _register_interaction_response(interaction, feedback_context)


async def _register_interaction_response(
    interaction: discord.Interaction,
    feedback_context: "FeedbackContext",
) -> None:
    """Resolve the sent message and register it for feedback capture.

    Imports are deferred so the optional dependency on services.feedback_log
    doesn't pull DB modules at import time for environments that don't need it
    (e.g. lint-only runs of cogs in isolation).
    """
    try:
        sent = await interaction.original_response()
    except discord.HTTPException as exc:
        log.warning(f"safe_respond: could not fetch original response for tracking ({exc})")
        return
    from data.db import get_pool
    from services import feedback_log
    pool = await get_pool()
    await feedback_log.register_bot_post(pool, sent, feedback_context)


async def post_to_channel_or_respond(
    interaction: discord.Interaction,
    target_channel_id: int | None,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    ephemeral_ack: str = "Posted.",
    feedback_context: "FeedbackContext | None" = None,
) -> None:
    """Post to a dedicated channel, deduping if the user is already there.

    - target_channel_id set, user IS in that channel: channel.send once, then
      ack ephemerally so the user knows the command ran without seeing the
      message twice.
    - target_channel_id set, user is ELSEWHERE: post to target AND respond
      visibly (they can't see the target channel from here).
    - No target channel: respond visibly, same as before.

    `feedback_context` registers the channel-side post (never the ephemeral
    ack or the user-visible mirror in their own channel — only the canonical
    post in the target channel is anchored).
    """
    channel = interaction.guild.get_channel(target_channel_id) if target_channel_id else None

    if channel is None:
        # No target channel — respond visibly to the user.
        kwargs: dict = {}
        if embed is not None:
            kwargs["embed"] = embed
        if content is not None:
            kwargs["content"] = content
        await safe_respond(interaction, feedback_context=feedback_context, **kwargs)
        return

    # Post to the target channel unconditionally.
    post_kwargs: dict = {}
    if embed is not None:
        post_kwargs["embed"] = embed
    if content is not None:
        post_kwargs["content"] = content
    sent = await channel.send(**post_kwargs)

    if feedback_context is not None:
        from data.db import get_pool
        from services import feedback_log
        pool = await get_pool()
        await feedback_log.register_bot_post(pool, sent, feedback_context)

    if interaction.channel_id == target_channel_id:
        # User is already in the target channel — they see the channel.send.
        # Ack ephemerally so they know the command completed without duplicating.
        await safe_respond(interaction, content=ephemeral_ack, ephemeral=True)
    else:
        # User is elsewhere — show them the same content in their channel too.
        await safe_respond(interaction, **post_kwargs)


class DBAError(Exception):
    """Base domain exception — caught by the error handler and shown to the user."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class PhaseError(DBAError):
    """Raised when a command is invalid in the current league phase."""


class PermissionError(DBAError):
    """Raised when a user lacks the role for an action."""


async def handle_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if interaction.id in _handled_interaction_ids:
        return
    _handled_interaction_ids.add(interaction.id)
    if len(_handled_interaction_ids) > 1000:
        _handled_interaction_ids.clear()

    msg: str | None = "Something went wrong."

    if isinstance(error, app_commands.CommandInvokeError):
        cause = error.original
        if isinstance(cause, DBAError):
            msg = cause.message
        else:
            log.error(f"Unhandled error in /{interaction.command}: {cause}", exc_info=cause)
            # If the command already sent a response, the user saw the real result.
            # Sending a second "Something went wrong" followup would be confusing —
            # suppress the user-visible message and rely on the log above.
            if interaction.response.is_done():
                return
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down — try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to do that."
    else:
        log.error(f"App command error: {error}", exc_info=error)
        if interaction.response.is_done():
            return

    if msg is None:
        return

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except (discord.NotFound, discord.HTTPException) as e:
        if isinstance(e, discord.HTTPException) and e.status not in (404, 403):
            log.warning(f"Failed to send error message for interaction {interaction.id}: {e}")
        # Token expired or interaction no longer valid — silently discard.
