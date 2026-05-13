import discord
from discord import app_commands
from core.logging import get_logger

log = get_logger(__name__)


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
    msg = "Something went wrong."

    if isinstance(error, app_commands.CommandInvokeError):
        cause = error.original
        if isinstance(cause, DBAError):
            msg = cause.message
        else:
            log.error(f"Unhandled error in /{interaction.command}: {cause}", exc_info=cause)
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down — try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to do that."
    else:
        log.error(f"App command error: {error}", exc_info=error)

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
