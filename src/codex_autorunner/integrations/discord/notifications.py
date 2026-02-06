from __future__ import annotations

import logging
from typing import Any, Optional

from ...core.logging_utils import log_event
from .constants import (
    TOKEN_USAGE_CACHE_LIMIT,
    TOKEN_USAGE_TURN_CACHE_LIMIT,
)

logger = logging.getLogger("codex_autorunner.integrations.discord.notifications")


class DiscordNotificationHandlers:
    """Mixin providing notification handling for the Discord bot service.

    Handles lifecycle events from the app server (SSE event stream) and
    delivers status updates to Discord channels/threads.
    """

    def _cache_token_usage(
        self,
        token_usage: dict[str, Any],
        *,
        turn_id: Optional[str],
        thread_id: Optional[str],
    ) -> None:
        if not isinstance(token_usage, dict):
            return
        if isinstance(thread_id, str) and thread_id:
            self._token_usage_by_thread[thread_id] = token_usage
            self._token_usage_by_thread.move_to_end(thread_id)
            while len(self._token_usage_by_thread) > TOKEN_USAGE_CACHE_LIMIT:
                self._token_usage_by_thread.popitem(last=False)
        if isinstance(turn_id, str) and turn_id:
            self._token_usage_by_turn[turn_id] = token_usage
            self._token_usage_by_turn.move_to_end(turn_id)
            while len(self._token_usage_by_turn) > TOKEN_USAGE_TURN_CACHE_LIMIT:
                self._token_usage_by_turn.popitem(last=False)

    async def _handle_app_server_notification(self, message: dict[str, Any]) -> None:
        """Process a notification from the app server."""
        method = message.get("method")
        params_raw = message.get("params")
        params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}

        if method == "car/app_server/oversizedMessageDropped":
            turn_id = params.get("turnId")
            thread_id = params.get("threadId")
            log_event(
                self._logger,
                logging.WARNING,
                "discord.app_server.oversize",
                turn_id=turn_id,
                thread_id=thread_id,
            )
            return

        if method == "car/lifecycle/flow_paused":
            flow_id = params.get("flowId")
            flow_name = params.get("flowName")
            log_event(
                self._logger,
                logging.INFO,
                "discord.lifecycle.flow_paused",
                flow_id=flow_id,
                flow_name=flow_name,
            )
            # Deliver notification to default channel if configured
            notification_channel = getattr(
                self._config, "default_notification_channel_id", None
            )
            if notification_channel:
                await self._send_lifecycle_notification(
                    notification_channel,
                    title="Flow Paused",
                    description=f"Flow **{flow_name or flow_id}** has been paused.",
                )
            return

        if method == "car/lifecycle/flow_completed":
            flow_id = params.get("flowId")
            flow_name = params.get("flowName")
            log_event(
                self._logger,
                logging.INFO,
                "discord.lifecycle.flow_completed",
                flow_id=flow_id,
                flow_name=flow_name,
            )
            notification_channel = getattr(
                self._config, "default_notification_channel_id", None
            )
            if notification_channel:
                await self._send_lifecycle_notification(
                    notification_channel,
                    title="Flow Completed",
                    description=f"Flow **{flow_name or flow_id}** has completed.",
                )
            return

        if method == "car/lifecycle/flow_failed":
            flow_id = params.get("flowId")
            flow_name = params.get("flowName")
            error = params.get("error", "Unknown error")
            log_event(
                self._logger,
                logging.WARNING,
                "discord.lifecycle.flow_failed",
                flow_id=flow_id,
                flow_name=flow_name,
                error=error,
            )
            notification_channel = getattr(
                self._config, "default_notification_channel_id", None
            )
            if notification_channel:
                await self._send_lifecycle_notification(
                    notification_channel,
                    title="Flow Failed",
                    description=f"Flow **{flow_name or flow_id}** failed: {error}",
                    error=True,
                )
            return

        log_event(
            self._logger,
            logging.DEBUG,
            "discord.notification.unhandled",
            method=method,
        )

    async def _send_lifecycle_notification(
        self,
        channel_id: int,
        *,
        title: str,
        description: str,
        error: bool = False,
    ) -> None:
        """Send a lifecycle event notification embed to a channel."""
        try:
            from .constants import EMBED_COLOR_INFO
            from .rendering import build_error_embed, build_response_embed

            if error:
                embed = build_error_embed(description, title=title)
            else:
                embed = build_response_embed(
                    description, color=EMBED_COLOR_INFO, title=title
                )
            await self._send_message(channel_id, embed=embed)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.notification.send_failed",
                channel_id=channel_id,
                title=title,
                exc=exc,
            )
