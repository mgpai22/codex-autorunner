from __future__ import annotations

import logging
from typing import Any, Optional

from ....core.logging_utils import log_event
from ..adapter import build_selection_view
from ..types import SelectionState

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.selections")


class DiscordSelectionHandlers:
    """Mixin providing paginated selection handling."""

    async def _send_selection(
        self,
        channel_id: int,
        items: list[tuple[str, str]],
        *,
        thread_id: Optional[int] = None,
        prompt: str = "Select an option:",
        kind: str = "selection",
        page: int = 0,
        state_key: Optional[str] = None,
        button_labels: Optional[dict[str, str]] = None,
    ) -> Optional[int]:
        """Send a paginated selection menu."""
        view, page_items = build_selection_view(
            items, page, kind=kind, button_labels=button_labels
        )
        msg_id = await self._send_message(
            channel_id, prompt, view=view, thread_id=thread_id
        )
        return msg_id

    async def _handle_selection_page(
        self,
        interaction: Any,
        state: SelectionState,
        page: int,
        *,
        kind: str = "selection",
        prompt: str = "Select an option:",
    ) -> None:
        """Handle pagination button click."""
        state.page = page
        view, page_items = build_selection_view(
            state.items, page, kind=kind, button_labels=state.button_labels
        )
        try:
            await interaction.response.edit_message(content=prompt, view=view)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.selection.page_failed",
                exc=exc,
            )
