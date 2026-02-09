from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .....core.logging_utils import log_event
from ...constants import DEFAULT_INTERRUPT_TIMEOUT_SECONDS, TurnKey
from ...types import TurnContext

logger = logging.getLogger(
    "codex_autorunner.integrations.discord.handlers.commands.shared"
)


class SharedHelpers:
    """Mixin providing shared turn execution helpers."""

    def _resolve_turn_context(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[TurnContext]:
        """Find a TurnContext by turn_id or codex_thread_id."""
        if turn_id:
            for tk, ctx in self._turn_contexts.items():
                if tk[1] == str(turn_id):
                    return ctx
        if thread_id:
            for _tk, ctx in self._turn_contexts.items():
                if ctx.codex_thread_id == thread_id:
                    return ctx
        if len(self._turn_contexts) == 1:
            return next(iter(self._turn_contexts.values()))
        return None

    def _resolve_turn_key(
        self, turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> Optional[TurnKey]:
        if turn_id:
            for tk in self._turn_contexts:
                if tk[1] == str(turn_id):
                    return tk
        if thread_id:
            for tk, ctx in self._turn_contexts.items():  # noqa: B007
                if ctx.codex_thread_id == thread_id:
                    return tk
        if len(self._turn_contexts) == 1:
            return next(iter(self._turn_contexts.keys()))
        return None

    def _is_turn_active(self, topic_key: str) -> bool:
        for tk in self._turn_contexts:
            if tk[0] == topic_key:
                return True
        return False

    async def _interrupt_turn(
        self,
        topic_key: str,
        *,
        timeout: float = DEFAULT_INTERRUPT_TIMEOUT_SECONDS,
    ) -> bool:
        """Interrupt an active turn for the given topic."""
        turn_key = None
        for tk in self._turn_contexts:
            if tk[0] == topic_key:
                turn_key = tk
                break
        if turn_key is None:
            return False

        ctx = self._turn_contexts.get(turn_key)
        if ctx is None:
            return False

        try:
            client = await self._client_for_workspace(
                (await self._store.get_topic(topic_key)).workspace_path
                if await self._store.get_topic(topic_key)
                else None
            )
            if client is None:
                return False
            await asyncio.wait_for(client.interrupt(), timeout=timeout)
            return True
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.turn.interrupt_failed",
                topic_key=topic_key,
                exc=exc,
            )
            return False
