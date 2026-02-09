from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ....core.lifecycle_events import LifecycleEventType
from ....core.logging_utils import log_event

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.agent_bus")

_DEFAULT_AGENT_IDENTITIES: dict[str, dict[str, Optional[str]]] = {
    "Project Manager": {"avatar_url": None},
    "Codex Agent": {"avatar_url": None},
    "Autorunner": {"avatar_url": None},
}
_CONTROL_PLANE_WORKSPACE_ID = "__control_plane__"


class DiscordAgentBusMixin:
    """Agent communication bus - posts coordination as structured embeds."""

    async def _post_to_agent_bus(self, guild_id: int, agent_name: str, embed: Any) -> None:
        """Post an embed to the agent-bus channel, using a webhook if available."""
        if not HAS_DISCORD:
            return

        channel_id = await self._resolve_agent_bus_channel_id(guild_id)
        if channel_id is None:
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.agent_bus.channel_missing",
                guild_id=guild_id,
            )
            return

        webhook_id: Optional[int] = None
        webhook_token: Optional[str] = None
        try:
            webhook_id, webhook_token = await self._ensure_agent_webhook(
                guild_id, channel_id, agent_name
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.agent_bus.webhook.ensure_failed",
                guild_id=guild_id,
                channel_id=channel_id,
                agent_name=agent_name,
                exc=exc,
            )

        if webhook_id and webhook_token:
            try:
                webhook = discord.Webhook.partial(
                    int(webhook_id),
                    str(webhook_token),
                    client=self._bot.bot,
                )
                identity = _DEFAULT_AGENT_IDENTITIES.get(agent_name) or {}
                await webhook.send(
                    embed=embed,
                    username=agent_name,
                    avatar_url=identity.get("avatar_url"),
                    wait=False,
                )
                return
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.agent_bus.webhook.send_failed",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    agent_name=agent_name,
                    exc=exc,
                )

        # Fallback: post directly as the bot.
        await self._send_message(channel_id, embed=embed)

    async def _ensure_agent_webhook(
        self, guild_id: int, channel_id: int, agent_name: str
    ) -> tuple[Optional[int], Optional[str]]:
        """Create a webhook for the agent if one doesn't exist; return (id, token)."""
        if not HAS_DISCORD:
            return None, None

        if not hasattr(self, "_agent_webhook_cache"):
            self._agent_webhook_cache: dict[tuple[int, int, str], tuple[int, str]] = {}

        cache_key = (guild_id, channel_id, agent_name)
        cached = self._agent_webhook_cache.get(cache_key)
        if cached is not None:
            return cached[0], cached[1]

        record = None
        try:
            record = await self._store.get_webhook(guild_id, channel_id, agent_name)
        except Exception:
            record = None

        if record is not None:
            webhook_id: Optional[int] = None
            webhook_token: Optional[str] = None
            if isinstance(record, tuple) and len(record) >= 2:
                raw_id = record[0]
                raw_token = record[1]
                if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                    webhook_id = raw_id
                if isinstance(raw_token, str) and raw_token:
                    webhook_token = raw_token
            if (
                isinstance(webhook_id, int)
                and not isinstance(webhook_id, bool)
                and isinstance(webhook_token, str)
                and webhook_token
            ):
                self._agent_webhook_cache[cache_key] = (webhook_id, webhook_token)
                return webhook_id, webhook_token

        # Create a new webhook and persist.
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            channel = await self._bot.fetch_channel(channel_id)
        if channel is None or not hasattr(channel, "create_webhook"):
            return None, None

        webhook = await channel.create_webhook(name=agent_name)
        webhook_id = getattr(webhook, "id", None)
        webhook_token = getattr(webhook, "token", None)
        if not isinstance(webhook_id, int) or isinstance(webhook_id, bool):
            return None, None
        if not isinstance(webhook_token, str) or not webhook_token:
            return None, None

        try:
            await self._store.save_webhook(
                guild_id,
                channel_id,
                agent_name,
                webhook_id,
                webhook_token,
            )
        except Exception:
            pass

        self._agent_webhook_cache[cache_key] = (webhook_id, webhook_token)
        log_event(
            self._logger,
            logging.INFO,
            "discord.agent_bus.webhook.created",
            guild_id=guild_id,
            channel_id=channel_id,
            agent_name=agent_name,
            webhook_id=webhook_id,
        )
        return webhook_id, webhook_token

    async def _on_lifecycle_event(self, event: Any) -> None:
        """Bridge lifecycle events to the agent bus."""
        if not HAS_DISCORD:
            return

        from ..rendering import build_agent_bus_embed, build_handoff_embed

        event_type = getattr(event, "event_type", None)
        event_type_value = None
        if hasattr(event_type, "value"):
            event_type_value = event_type.value
        elif isinstance(event_type, str):
            event_type_value = event_type

        details: dict[str, Any] = {}
        if hasattr(event, "repo_id"):
            details["repo_id"] = getattr(event, "repo_id", None)
        if hasattr(event, "run_id"):
            details["run_id"] = getattr(event, "run_id", None)
        if hasattr(event, "origin"):
            details["origin"] = getattr(event, "origin", None)
        if hasattr(event, "timestamp"):
            details["timestamp"] = getattr(event, "timestamp", None)
        data = getattr(event, "data", None)
        if isinstance(data, dict) and data:
            details["data"] = data

        embeds: list[tuple[str, Any]] = []

        if event_type == LifecycleEventType.DISPATCH_CREATED:
            embeds.append(
                (
                    "Project Manager",
                    build_agent_bus_embed("Project Manager", "dispatch_created", details),
                )
            )
        elif event_type_value:
            agent = "Autorunner"
            embeds.append((agent, build_agent_bus_embed(agent, event_type_value, details)))

        # Optional: detect cross-workspace handoff metadata.
        if isinstance(data, dict):
            source_ws = data.get("source_workspace") or data.get("sourceWorkspace")
            target_ws = data.get("target_workspace") or data.get("targetWorkspace")
            reason = data.get("reason") or data.get("handoff_reason") or data.get(
                "handoffReason"
            )
            if (
                isinstance(source_ws, str)
                and isinstance(target_ws, str)
                and (isinstance(reason, str) or reason is None)
            ):
                embeds.append(
                    (
                        "Project Manager",
                        build_handoff_embed(source_ws, target_ws, str(reason or "")),
                    )
                )

        if not embeds:
            return

        guild_ids = getattr(self._config, "allowed_guild_ids", set())
        for guild_id in guild_ids:
            for agent_name, embed in embeds:
                try:
                    await self._post_to_agent_bus(int(guild_id), agent_name, embed)
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.agent_bus.post_failed",
                        guild_id=guild_id,
                        agent_name=agent_name,
                        exc=exc,
                    )

    async def _register_lifecycle_listener(self) -> None:
        """Register a LifecycleEventEmitter listener that posts into Discord."""
        if not HAS_DISCORD:
            return

        if self._hub_supervisor is None:
            return

        if bool(getattr(self, "_agent_bus_listener_registered", False)):
            return

        loop = asyncio.get_running_loop()
        emitter = self._hub_supervisor.lifecycle_emitter

        def _listener(event: Any) -> None:
            try:
                asyncio.run_coroutine_threadsafe(self._on_lifecycle_event(event), loop)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.agent_bus.listener_failed",
                    exc=exc,
                )

        emitter.add_listener(_listener)
        self._agent_bus_listener_registered = True
        self._agent_bus_listener = _listener
        log_event(
            self._logger,
            logging.INFO,
            "discord.agent_bus.listener_registered",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_agent_bus_channel_id(self, guild_id: int) -> Optional[int]:
        configured = getattr(self._config, "agent_bus_channel_id", None)
        if isinstance(configured, int) and not isinstance(configured, bool):
            return configured

        try:
            channel_id = await self._store.get_scaffolded_channel(  # type: ignore[attr-defined]
                guild_id,
                _CONTROL_PLANE_WORKSPACE_ID,
                "agent_bus",
            )
            if isinstance(channel_id, int) and not isinstance(channel_id, bool):
                return channel_id
        except Exception:
            pass

        try:
            guild = self._bot.bot.get_guild(guild_id)
            if guild is None:
                return None
            for ch in getattr(guild, "channels", []):
                if getattr(ch, "name", None) == "agent-bus":
                    ch_id = getattr(ch, "id", None)
                    if isinstance(ch_id, int) and not isinstance(ch_id, bool):
                        return ch_id
        except Exception:
            return None

        return None
