import discord
from discord import app_commands
from core.logging import get_logger

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
    try:
        await interaction.response.defer(ephemeral=ephemeral)
    except (discord.NotFound, discord.HTTPException) as exc:
        log.warning(f"defer() failed for interaction {interaction.id} ({exc}) — continuing")


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
