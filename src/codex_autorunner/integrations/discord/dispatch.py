from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ...core.logging_utils import log_event
from .adapter import allowlist_allows

if TYPE_CHECKING:
    from .service import DiscordBotService

logger = logging.getLogger("codex_autorunner.integrations.discord.dispatch")


async def dispatch_interaction(service: "DiscordBotService", interaction: Any) -> None:
    """Route a Discord interaction (slash command, button, select, modal) to the appropriate handler."""
    try:
        interaction_type = interaction.type

        # Application commands (slash commands) are handled by the command tree
        # registered in DiscordCommandHandlers._register_slash_commands().
        # discord.py processes them automatically via the Bot's CommandTree.
        # Only component and modal interactions need manual routing here.

        # Handle component interactions (buttons, selects)
        # interaction.type == InteractionType.component (value 3)
        if hasattr(interaction_type, "value") and interaction_type.value == 3:
            from .handlers.callbacks import dispatch_component_interaction

            await dispatch_component_interaction(service, interaction)
            return

        # Handle modal submissions
        # interaction.type == InteractionType.modal_submit (value 5)
        if hasattr(interaction_type, "value") and interaction_type.value == 5:
            from .handlers.callbacks import dispatch_modal_submit

            await dispatch_modal_submit(service, interaction)
            return

    except Exception as exc:
        log_event(logger, logging.ERROR, "discord.dispatch.interaction_error", exc=exc)


async def dispatch_message(service: "DiscordBotService", message: Any) -> None:
    """Route a free-text Discord message to the appropriate handler."""
    try:
        if not allowlist_allows(message, service._allowlist):
            return

        # Skip bot's own messages
        if message.author == service._bot.bot.user:
            return

        from .handlers.messages import handle_message

        await handle_message(service, message)
    except Exception as exc:
        log_event(logger, logging.ERROR, "discord.dispatch.message_error", exc=exc)
