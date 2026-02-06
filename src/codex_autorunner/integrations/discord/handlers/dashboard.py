from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ....core.logging_utils import log_event
from ....core.state import now_iso

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.dashboard")

_DASHBOARD_DEBOUNCE_SECONDS = 2.0
_CONTROL_PLANE_WORKSPACE_ID = "__control_plane__"


class DiscordDashboardMixin:
    """Live dashboard that shows real-time workspace status."""

    async def _ensure_dashboard(self, guild_id: int) -> None:
        """Create or update the pinned dashboard embed in the dashboard channel."""
        if not HAS_DISCORD:
            return

        channel_id = await self._resolve_dashboard_channel_id(guild_id)
        if channel_id is None:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.dashboard.channel_missing",
                guild_id=guild_id,
            )
            return

        from ..rendering import build_dashboard_embed

        workspaces_status = await self._collect_workspaces_status()
        embed = build_dashboard_embed(workspaces_status)

        record = None
        try:
            record = await self._store.get_dashboard(guild_id)  # type: ignore[attr-defined]
        except Exception:
            record = None

        message_id: Optional[int] = None
        if isinstance(record, tuple) and len(record) >= 2:
            raw_message_id = record[1]
            if isinstance(raw_message_id, int) and not isinstance(raw_message_id, bool):
                message_id = raw_message_id
        elif record is not None:
            message_id = getattr(record, "message_id", None)
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                message_id = None

        if message_id is not None:
            ok = await self._edit_message(channel_id, message_id, embed=embed)
            if not ok:
                message_id = None

        if message_id is None:
            message_id = await self._send_message(channel_id, embed=embed)
            if message_id is None:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.dashboard.send_failed",
                    guild_id=guild_id,
                    channel_id=channel_id,
                )
                return

        await self._pin_dashboard_message(channel_id, message_id)

        try:
            await self._store.save_dashboard(guild_id, channel_id, message_id)
        except Exception:
            pass

        log_event(
            self._logger,
            logging.INFO,
            "discord.dashboard.ensured",
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
        )

    async def _update_dashboard(self, guild_id: int) -> None:
        """Refresh the dashboard embed with current workspace status."""
        if not HAS_DISCORD:
            return

        record = None
        try:
            record = await self._store.get_dashboard(guild_id)  # type: ignore[attr-defined]
        except Exception:
            record = None

        if record is None:
            await self._ensure_dashboard(guild_id)
            return

        channel_id = None
        message_id = None
        if isinstance(record, tuple) and len(record) >= 2:
            channel_id = record[0]
            message_id = record[1]
        else:
            channel_id = getattr(record, "channel_id", None)
            message_id = getattr(record, "message_id", None)
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return

        from ..rendering import build_dashboard_embed

        workspaces_status = await self._collect_workspaces_status()
        embed = build_dashboard_embed(workspaces_status)

        ok = await self._edit_message(channel_id, message_id, embed=embed)
        if not ok:
            # Message may have been deleted; recreate
            await self._ensure_dashboard(guild_id)
            return

        try:
            await self._store.update_dashboard_timestamp(guild_id)
        except Exception:
            pass

        log_event(
            self._logger,
            logging.DEBUG,
            "discord.dashboard.updated",
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
        )

    async def _mark_dashboard_dirty(self, guild_id: int) -> None:
        """Schedule a debounced dashboard refresh."""
        if not HAS_DISCORD:
            return

        if not hasattr(self, "_dashboard_dirty_flags"):
            self._dashboard_dirty_flags: set[int] = set()
        if not hasattr(self, "_dashboard_refresh_tasks"):
            self._dashboard_refresh_tasks: dict[int, asyncio.Task[None]] = {}

        self._dashboard_dirty_flags.add(guild_id)
        task = self._dashboard_refresh_tasks.get(guild_id)
        if task is not None and not task.done():
            return

        async def _delayed() -> None:
            try:
                await asyncio.sleep(_DASHBOARD_DEBOUNCE_SECONDS)
                if guild_id not in self._dashboard_dirty_flags:
                    return
                self._dashboard_dirty_flags.discard(guild_id)
                await self._update_dashboard(guild_id)
            finally:
                self._dashboard_refresh_tasks.pop(guild_id, None)

        self._dashboard_refresh_tasks[guild_id] = self._spawn_task(_delayed())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_dashboard_channel_id(self, guild_id: int) -> Optional[int]:
        """Resolve the dashboard channel ID.

        Prefer scaffolded state if available; fall back to scanning for a channel
        named "dashboard" to keep behavior resilient before scaffold state exists.
        """
        configured = getattr(self._config, "dashboard_channel_id", None)
        if isinstance(configured, int) and not isinstance(configured, bool):
            return configured

        try:
            channel_id = await self._store.get_scaffolded_channel(  # type: ignore[attr-defined]
                guild_id,
                _CONTROL_PLANE_WORKSPACE_ID,
                "dashboard",
            )
            if isinstance(channel_id, int) and not isinstance(channel_id, bool):
                return channel_id
        except Exception:
            pass

        try:
            guild = self._bot.bot.get_guild(guild_id)
            if guild is None:
                return None
            if not isinstance(guild, discord.Guild):
                return None
            for ch in getattr(guild, "channels", []):
                if getattr(ch, "name", None) == "dashboard":
                    ch_id = getattr(ch, "id", None)
                    if isinstance(ch_id, int) and not isinstance(ch_id, bool):
                        return ch_id
        except Exception:
            return None

        return None

    async def _pin_dashboard_message(self, channel_id: int, message_id: int) -> None:
        try:
            channel = self._bot.get_channel(channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(channel_id)
            if channel is None or not hasattr(channel, "fetch_message"):
                return
            msg = await channel.fetch_message(message_id)
            if msg is None or not hasattr(msg, "pin"):
                return
            if getattr(msg, "pinned", False):
                return
            await msg.pin(reason="codex-autorunner dashboard")
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.dashboard.pin_failed",
                channel_id=channel_id,
                message_id=message_id,
                exc=exc,
            )

    async def _collect_workspaces_status(self) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []

        # Prefer hub supervisor snapshots when available (covers all workspaces).
        if self._hub_supervisor is not None:
            try:
                repos = self._hub_supervisor.list_repos()
            except Exception:
                repos = []
            for repo in repos:
                status = getattr(repo, "status", None)
                status_value = (
                    status.value if hasattr(status, "value") else str(status or "")
                )
                last_activity = (
                    getattr(repo, "last_run_started_at", None)
                    or getattr(repo, "last_run_finished_at", None)
                )
                active_tasks = 1 if status_value == "running" else 0
                statuses.append(
                    {
                        "name": getattr(repo, "display_name", None)
                        or getattr(repo, "id", "workspace"),
                        "active_tasks": active_tasks,
                        "last_activity": last_activity,
                        "status": status_value,
                    }
                )
            return statuses

        # Fallback: summarize bound topics.
        try:
            topics = await self._store.list_topics()
        except Exception:
            topics = {}

        by_workspace: dict[str, dict[str, Any]] = {}
        for record in topics.values():
            workspace_path = getattr(record, "workspace_path", None)
            if not isinstance(workspace_path, str) or not workspace_path:
                continue
            state = by_workspace.setdefault(
                workspace_path,
                {
                    "name": workspace_path.rsplit("/", 1)[-1] or workspace_path,
                    "active_tasks": 0,
                    "last_activity": None,
                    "status": "idle",
                },
            )
            if getattr(record, "active_turn_id", None):
                state["active_tasks"] += 1
                state["status"] = "running"
            last_turn_at = getattr(record, "last_turn_at", None)
            if isinstance(last_turn_at, str) and last_turn_at:
                state["last_activity"] = last_turn_at

        statuses.extend(by_workspace.values())
        return statuses
