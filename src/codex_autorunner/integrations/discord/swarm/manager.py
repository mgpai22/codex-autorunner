"""SwarmManager — Discord bridge between SwarmController and forum threads.

Orchestrates the full lifecycle of a swarm session: spawning agents,
creating Discord forum threads per agent, routing messages between
agents and their threads, and health-checking the swarm.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from ..constants import (
    EMBED_COLOR_SWARM,
    SWARM_AGENT_TIMEOUT_SECONDS,
    SWARM_FORUM_TAG_NAME,
    SWARM_HEALTH_CHECK_INTERVAL_SECONDS,
    SWARM_MAX_AGENTS,
    SWARM_POLL_INTERVAL_SECONDS,
    SWARM_SHUTDOWN_GRACE_SECONDS,
    SWARM_THREAD_NAME_PREFIX,
    SWARM_TIMEOUT_SECONDS,
)
from .controller import SwarmController
from .presets import get_preset, list_presets
from .types import SwarmAgentState

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

logger = logging.getLogger("codex_autorunner.integrations.discord.swarm.manager")


class SwarmManager:
    """Manage active swarm sessions and their Discord integration."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._sessions: dict[str, _ActiveSwarm] = {}
        self._log = getattr(service, "_logger", None) or logger

    def get_active_agent_count(self) -> int:
        """Return the number of currently-running swarm agent processes.

        Used for bot presence ("Watching N tasks"). Each swarm agent counts as a task.
        """
        active_statuses = {"spawning", "running"}

        total = 0
        for active in list(self._sessions.values()):
            # Prefer semantic status (idle agents should not count as "tasks").
            if getattr(active, "agent_statuses", None):
                for status in active.agent_statuses.values():
                    if isinstance(status, str) and status in active_statuses:
                        total += 1
                continue

            # Fallback for safety (should be rare; e.g. in-memory sessions created
            # before agent_statuses existed).
            try:
                total += len(active.controller.running_agents())
            except Exception:
                continue

        return total

    # ------------------------------------------------------------------
    # Discord helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _split_for_discord(text: str, *, limit: int) -> list[str]:
        """Split a long message into <=limit chunks for Discord.

        Preserves content exactly (no loss), prefers breaking on newlines/spaces.
        """
        if not text:
            return []
        if len(text) <= limit:
            return [text]

        chunks: list[str] = []
        start = 0
        n = len(text)
        while start < n:
            end = min(start + limit, n)
            if end >= n:
                chunks.append(text[start:end])
                break

            window = text[start:end]
            cut = window.rfind("\n")
            if cut == -1:
                cut = window.rfind(" ")

            # If the best break is at the beginning (or not found), hard-cut.
            if cut <= 0:
                cut = limit
            else:
                # Include the break character to preserve content exactly.
                cut = cut + 1

            chunk = text[start : start + cut]
            if chunk:
                chunks.append(chunk)
            start = start + cut

        return chunks

    async def _send_text(self, thread: Any, text: str) -> None:
        from ..constants import DISCORD_MAX_MESSAGE_LENGTH

        for chunk in self._split_for_discord(text, limit=DISCORD_MAX_MESSAGE_LENGTH):
            # Discord rejects empty strings; also avoid pure-whitespace spam.
            if not chunk or not chunk.strip():
                continue
            await thread.send(chunk)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def start_swarm(
        self,
        guild: Any,
        *,
        workspace_id: str,
        workspace_path: str,
        forum_channel_id: int,
        preset_name: str,
        prompt: str,
        user_id: int,
    ) -> str:
        """Start a new swarm. Returns swarm_id."""
        swarm_config = self._service._config.swarm
        preset = get_preset(preset_name, custom_presets=swarm_config.custom_presets)
        if preset is None:
            raise ValueError(
                f"Unknown preset '{preset_name}'. "
                f"Available: {', '.join(list_presets(swarm_config.custom_presets).keys())}"
            )

        if len(preset.roles) > swarm_config.max_agents:
            raise ValueError(
                f"Preset '{preset_name}' requires {len(preset.roles)} agents, "
                f"max allowed is {swarm_config.max_agents}."
            )

        swarm_id = str(uuid.uuid4())
        team_name = f"swarm-{swarm_id[:8]}"

        controller = SwarmController(
            team_name=team_name,
            cwd=workspace_path,
            claude_binary=swarm_config.claude_binary,
            logger=self._log,
        )
        # Find the lead role name so we can set leadAgentId correctly
        lead_name = "lead"
        for role in preset.roles:
            if role.is_lead:
                lead_name = role.name
                break
        await controller.initialize(lead_name=lead_name)

        # Save swarm to SQLite
        from ....core.state import now_iso

        store = self._service._store
        await store.save_swarm(
            swarm_id=swarm_id,
            guild_id=guild.id,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            forum_channel_id=forum_channel_id,
            team_name=team_name,
            preset_name=preset_name,
            prompt=prompt,
            user_id=user_id,
            status="starting",
        )

        active = _ActiveSwarm(
            swarm_id=swarm_id,
            team_name=team_name,
            controller=controller,
            guild_id=guild.id,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            forum_channel_id=forum_channel_id,
            preset_name=preset_name,
            prompt=prompt,
            user_id=user_id,
        )
        self._sessions[swarm_id] = active

        # Create forum threads and spawn agents
        try:
            forum_channel = await self._service._bot.fetch_channel(forum_channel_id)
        except Exception as exc:
            self._log.error("Failed to fetch forum channel %d: %s", forum_channel_id, exc)
            await store.update_swarm_status(swarm_id, "failed")
            self._sessions.pop(swarm_id, None)
            raise

        swarm_tag = await self._resolve_swarm_tag(forum_channel, guild.id, workspace_id)

        # Register callbacks BEFORE spawning so early inbox messages are captured.
        #
        # Important: Do NOT route PTY/stdout to Discord. Claude Code renders a
        # full-screen TUI to the terminal; user-visible output must come from
        # teammate inbox messages (SendMessage).
        controller.on_message(self._make_message_callback(swarm_id))

        for role in preset.roles:
            agent_name = role.name
            model = role.model
            agent_prompt = prompt
            if role.prompt_template:
                agent_prompt = role.prompt_template.format(prompt=prompt)

            # Create forum thread for this agent
            from ..rendering import build_swarm_agent_card_embed

            agent_info = {
                "agent_name": agent_name,
                "role_name": role.name,
                "model": model,
                "status": "spawning",
                "is_lead": role.is_lead,
            }
            embed = build_swarm_agent_card_embed(agent_info)
            applied_tags = [swarm_tag] if swarm_tag else []

            thread_name = f"{SWARM_THREAD_NAME_PREFIX} {agent_name} — {swarm_id[:8]}"
            if len(thread_name) > 100:
                thread_name = thread_name[:100]

            try:
                thread_with_message = await forum_channel.create_thread(
                    name=thread_name,
                    embed=embed,
                    applied_tags=applied_tags,
                )
                thread = thread_with_message.thread
                root_message = thread_with_message.message
                thread_id = thread.id
                root_message_id = root_message.id if root_message else None
            except Exception as exc:
                self._log.error("Failed to create thread for agent %s: %s", agent_name, exc)
                thread_id = None
                root_message_id = None

            # Spawn the agent process
            spawn_exc: Optional[BaseException] = None
            try:
                agent_id = await controller.spawn_agent(
                    agent_name,
                    model=model,
                    agent_type=role.agent_type,
                    prompt=agent_prompt,
                )
            except Exception as exc:
                spawn_exc = exc
                self._log.error("Failed to spawn agent %s: %s", agent_name, exc)
                agent_id = f"{agent_name}@{team_name}"

            pid = controller.get_agent_pid(agent_name)
            agent_status = "running" if (spawn_exc is None and pid is not None) else "failed"

            # Save agent to SQLite
            await store.save_swarm_agent(
                swarm_id=swarm_id,
                agent_name=agent_name,
                agent_id=agent_id,
                role_name=role.name,
                model=model,
                is_lead=role.is_lead,
                discord_thread_id=thread_id,
                discord_root_message_id=root_message_id,
                status=agent_status,
                pid=pid,
            )

            active.agent_statuses[agent_name] = agent_status
            active.agent_threads[agent_name] = thread_id
            active.agent_root_messages[agent_name] = root_message_id

            # If spawn failed, surface the error in the thread (if any) so users
            # don't see a silent "running" state forever.
            if spawn_exc is not None and thread_id:
                try:
                    thread_obj = thread  # from create_thread above
                except Exception:
                    thread_obj = None
                try:
                    if thread_obj is None:
                        thread_obj = await self._service._bot.fetch_channel(thread_id)
                    if thread_obj is not None:
                        msg = f"Failed to spawn agent `{agent_name}`: {spawn_exc}"
                        await self._send_text(thread_obj, msg)
                except Exception as exc:
                    self._log.warning(
                        "Failed to post spawn error for %s to thread %s: %s",
                        agent_name,
                        thread_id,
                        exc,
                    )

            # Update the embed from "spawning" to final status
            await self._update_agent_embed(
                swarm_id, agent_name, agent_status,
                role_name=role.name, model=model, is_lead=role.is_lead,
            )

        # Post navigation links into the lead agent thread so users can quickly
        # jump between agents.
        lead_thread_id = active.agent_threads.get(lead_name)
        if isinstance(lead_thread_id, int):
            try:
                lead_thread = await self._service._bot.fetch_channel(lead_thread_id)
                if lead_thread is not None:
                    lines: list[str] = ["**Sub-agents**"]
                    for role in preset.roles:
                        if role.name == lead_name:
                            continue
                        tid = active.agent_threads.get(role.name)
                        if isinstance(tid, int):
                            lines.append(f"- `{role.name}`: <#{tid}>")
                        else:
                            lines.append(f"- `{role.name}`: (thread unavailable)")
                    await self._send_text(lead_thread, "\n".join(lines))
            except Exception as exc:
                self._log.debug(
                    "Failed to post swarm navigation links for %s: %s", swarm_id, exc
                )

        # Start polling + health monitoring + output flush timer
        await controller.start_polling(
            interval=swarm_config.poll_interval_seconds
        )
        active.monitor_task = asyncio.create_task(
            self._monitor_swarm(swarm_id)
        )
        active.flush_task = asyncio.create_task(
            self._periodic_flush(swarm_id)
        )

        await store.update_swarm_status(swarm_id, "running")

        # Update bot presence ("Watching N tasks") now that swarm agents are live.
        try:
            if hasattr(self._service, "_update_presence"):
                await self._service._update_presence()
        except Exception as exc:
            self._log.debug("Presence update failed after swarm start: %s", exc)

        self._log.info(
            "Swarm started: id=%s team=%s preset=%s agents=%d",
            swarm_id,
            team_name,
            preset_name,
            len(preset.roles),
        )

        return swarm_id

    async def stop_swarm(self, swarm_id: str) -> None:
        """Stop an active swarm with graceful shutdown."""
        active = self._sessions.get(swarm_id)
        if active is None:
            return

        swarm_config = self._service._config.swarm
        store = self._service._store

        await store.update_swarm_status(swarm_id, "stopping")

        # Request graceful shutdown
        for name in active.controller.running_agents():
            try:
                await active.controller.request_shutdown(name)
            except Exception as exc:
                self._log.warning("Shutdown request failed for %s: %s", name, exc)

        # Wait grace period
        await asyncio.sleep(swarm_config.shutdown_grace_seconds)

        # Kill remaining
        await active.controller.stop_polling()
        await active.controller.kill_all()

        for task in (active.monitor_task, active.flush_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Cleanup filesystem
        from .protocol import cleanup_team

        await cleanup_team(active.team_name)

        await store.update_swarm_status(swarm_id, "stopped")
        self._sessions.pop(swarm_id, None)

        try:
            if hasattr(self._service, "_update_presence"):
                await self._service._update_presence()
        except Exception as exc:
            self._log.debug("Presence update failed after swarm stop: %s", exc)

        self._log.info("Swarm stopped: id=%s", swarm_id)

    async def stop_all(self) -> None:
        """Stop all active swarms."""
        ids = list(self._sessions.keys())
        for sid in ids:
            try:
                await self.stop_swarm(sid)
            except Exception as exc:
                self._log.error("Error stopping swarm %s: %s", sid, exc)

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def _make_message_callback(self, swarm_id: str) -> Any:
        async def _callback(from_agent: str, msg: dict[str, Any]) -> None:
            await self._on_controller_message(swarm_id, from_agent, msg)

        return _callback

    def _make_output_callback(self, swarm_id: str) -> Any:
        async def _callback(from_agent: str, msg: dict[str, Any]) -> None:
            await self._on_agent_output(swarm_id, from_agent, msg)

        return _callback

    async def _on_agent_output(
        self, swarm_id: str, from_agent: str, msg: dict[str, Any]
    ) -> None:
        """Route stdout output from an agent to its Discord forum thread."""
        active = self._sessions.get(swarm_id)
        if active is None:
            return

        thread_id = active.agent_threads.get(from_agent)
        if not thread_id:
            return

        text = msg.get("text", "")
        if not text:
            return

        # Buffer lines and batch-send to avoid Discord rate limits
        buf = active.output_buffers.setdefault(from_agent, [])
        buf.append(text)

        # Flush if buffer is large enough or enough time has passed
        import time

        now = time.monotonic()
        last_flush = active.last_flush_times.get(from_agent, 0.0)
        total_len = sum(len(line) for line in buf)

        if total_len < 200 and (now - last_flush) < 2.0:
            # Buffer more — not enough content or too soon
            return

        # Flush
        combined = "\n".join(buf)
        buf.clear()
        active.last_flush_times[from_agent] = now

        if not combined.strip():
            return

        try:
            thread = await self._service._bot.fetch_channel(thread_id)
            if thread is not None:
                await self._send_text(thread, combined)
        except Exception as exc:
            self._log.warning(
                "Failed to post output from %s to thread %d: %s",
                from_agent, thread_id, exc,
            )

    async def _on_controller_message(
        self, swarm_id: str, from_agent: str, msg: dict[str, Any]
    ) -> None:
        """Route a message from an agent to its Discord forum thread."""
        active = self._sessions.get(swarm_id)
        if active is None:
            return

        thread_id = active.agent_threads.get(from_agent)
        if not thread_id:
            return

        text = msg.get("text", "")
        if not text:
            return

        # Try to parse structured messages
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                msg_type = parsed.get("type", "")
                if msg_type == "idle_notification":
                    active.agent_statuses[from_agent] = "idle"
                    await self._service._store.update_swarm_agent_status(
                        swarm_id, from_agent, "idle"
                    )
                    await self._update_agent_embed(swarm_id, from_agent, "idle")
                    try:
                        if hasattr(self._service, "_update_presence"):
                            await self._service._update_presence()
                    except Exception as exc:
                        self._log.debug(
                            "Presence update failed after idle notification: %s", exc
                        )
                    return
                if msg_type == "shutdown_approved":
                    active.agent_statuses[from_agent] = "completed"
                    await self._service._store.update_swarm_agent_status(
                        swarm_id, from_agent, "completed"
                    )
                    await self._update_agent_embed(swarm_id, from_agent, "completed")
                    try:
                        if hasattr(self._service, "_update_presence"):
                            await self._service._update_presence()
                    except Exception as exc:
                        self._log.debug(
                            "Presence update failed after shutdown approval: %s", exc
                        )
                    return
                if msg_type == "task_completed":
                    text = parsed.get("summary", text)
        except (json.JSONDecodeError, TypeError):
            pass

        # Post to the agent's forum thread via webhook or message
        try:
            thread = await self._service._bot.fetch_channel(thread_id)
            if thread is not None:
                await self._send_text(thread, text)
        except Exception as exc:
            self._log.warning(
                "Failed to post message from %s to thread %d: %s",
                from_agent,
                thread_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    async def _monitor_swarm(self, swarm_id: str) -> None:
        """Health check loop for a swarm session."""
        swarm_config = self._service._config.swarm
        interval = swarm_config.health_check_interval_seconds
        timeout = swarm_config.swarm_timeout_seconds
        agent_timeout = swarm_config.agent_timeout_seconds

        import time

        start_time = time.monotonic()
        store = self._service._store

        try:
            while True:
                await asyncio.sleep(interval)

                active = self._sessions.get(swarm_id)
                if active is None:
                    break

                elapsed = time.monotonic() - start_time

                # Check swarm-level timeout
                if elapsed > timeout:
                    self._log.warning(
                        "Swarm %s timed out after %.0fs", swarm_id, elapsed
                    )
                    await self.stop_swarm(swarm_id)
                    break

                # Check agent health
                running = active.controller.running_agents()
                if not running:
                    self._log.info("All agents in swarm %s have exited", swarm_id)
                    await store.update_swarm_status(swarm_id, "completed")
                    self._sessions.pop(swarm_id, None)
                    try:
                        if hasattr(self._service, "_update_presence"):
                            await self._service._update_presence()
                    except Exception as exc:
                        self._log.debug(
                            "Presence update failed after swarm completion: %s", exc
                        )
                    break

                # Update agent statuses
                presence_dirty = False
                for name in list(active.agent_threads.keys()):
                    if name in active.agent_finished:
                        continue
                    if not active.controller.is_agent_running(name):
                        # Flush remaining output buffer
                        await self._flush_output_buffer(swarm_id, name)

                        exit_code = active.controller.agent_exit_code(name)
                        status = "completed" if exit_code == 0 else "failed"
                        active.agent_statuses[name] = status
                        await store.update_swarm_agent_status(
                            swarm_id, name, status
                        )
                        await self._update_agent_embed(swarm_id, name, status)
                        active.agent_finished.add(name)
                        presence_dirty = True

                if presence_dirty:
                    try:
                        if hasattr(self._service, "_update_presence"):
                            await self._service._update_presence()
                    except Exception as exc:
                        self._log.debug(
                            "Presence update failed after agent exit: %s", exc
                        )

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error("Monitor error for swarm %s: %s", swarm_id, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_swarm_tag(
        self, forum_channel: Any, guild_id: int, workspace_id: str
    ) -> Any:
        """Resolve the 'swarm' ForumTag from a channel."""
        if not HAS_DISCORD:
            return None
        for tag in getattr(forum_channel, "available_tags", []):
            if getattr(tag, "name", "") == SWARM_FORUM_TAG_NAME:
                return tag
        return None

    async def _periodic_flush(self, swarm_id: str) -> None:
        """Periodically flush output buffers so short messages don't get stuck."""
        import time

        try:
            while True:
                await asyncio.sleep(3.0)
                active = self._sessions.get(swarm_id)
                if active is None:
                    break
                now = time.monotonic()
                for agent_name in list(active.output_buffers.keys()):
                    buf = active.output_buffers.get(agent_name)
                    if not buf:
                        continue
                    last_flush = active.last_flush_times.get(agent_name, 0.0)
                    if (now - last_flush) >= 2.0:
                        await self._flush_output_buffer(swarm_id, agent_name)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error("Periodic flush error for swarm %s: %s", swarm_id, exc)

    async def _flush_output_buffer(self, swarm_id: str, agent_name: str) -> None:
        """Flush any remaining buffered output for an agent."""
        active = self._sessions.get(swarm_id)
        if active is None:
            return

        buf = active.output_buffers.get(agent_name)
        if not buf:
            return

        import time

        combined = "\n".join(buf)
        buf.clear()
        active.last_flush_times[agent_name] = time.monotonic()

        if not combined.strip():
            return

        thread_id = active.agent_threads.get(agent_name)
        if not thread_id:
            return

        try:
            thread = await self._service._bot.fetch_channel(thread_id)
            if thread is not None:
                await self._send_text(thread, combined)
        except Exception as exc:
            self._log.warning(
                "Failed to flush output for %s: %s", agent_name, exc
            )

    async def _update_agent_embed(
        self,
        swarm_id: str,
        agent_name: str,
        status: str,
        *,
        role_name: Optional[str] = None,
        model: Optional[str] = None,
        is_lead: Optional[bool] = None,
    ) -> None:
        """Edit the agent card embed in the forum thread to reflect new status."""
        active = self._sessions.get(swarm_id)
        if active is None:
            return

        thread_id = active.agent_threads.get(agent_name)
        root_message_id = active.agent_root_messages.get(agent_name)
        if not thread_id or not root_message_id:
            return

        # Look up agent metadata from SQLite if not provided
        if role_name is None or model is None or is_lead is None:
            try:
                agents = await self._service._store.list_swarm_agents(swarm_id)
                for a in agents:
                    if a["agent_name"] == agent_name:
                        role_name = role_name or a.get("role_name", agent_name)
                        model = model or a.get("model", "unknown")
                        if is_lead is None:
                            is_lead = bool(a.get("is_lead"))
                        break
            except Exception:
                pass

        from ..rendering import build_swarm_agent_card_embed

        agent_info = {
            "agent_name": agent_name,
            "role_name": role_name or agent_name,
            "model": model or "unknown",
            "status": status,
            "is_lead": is_lead or False,
        }
        embed = build_swarm_agent_card_embed(agent_info)

        try:
            thread = await self._service._bot.fetch_channel(thread_id)
            if thread is not None:
                msg = await thread.fetch_message(root_message_id)
                await msg.edit(embed=embed)
        except Exception as exc:
            self._log.debug(
                "Failed to update agent embed for %s: %s", agent_name, exc
            )

    def get_active_swarm_ids(self) -> list[str]:
        """Return IDs of all active swarms."""
        return list(self._sessions.keys())

    def is_swarm_active(self, swarm_id: str) -> bool:
        """Check if a swarm is currently active."""
        return swarm_id in self._sessions


class _ActiveSwarm:
    """In-memory state for an active swarm session."""

    __slots__ = (
        "swarm_id",
        "team_name",
        "controller",
        "guild_id",
        "workspace_id",
        "workspace_path",
        "forum_channel_id",
        "preset_name",
        "prompt",
        "user_id",
        "agent_threads",
        "agent_root_messages",
        "agent_statuses",
        "agent_finished",
        "output_buffers",
        "last_flush_times",
        "monitor_task",
        "flush_task",
    )

    def __init__(
        self,
        *,
        swarm_id: str,
        team_name: str,
        controller: SwarmController,
        guild_id: int,
        workspace_id: str,
        workspace_path: str,
        forum_channel_id: int,
        preset_name: str,
        prompt: str,
        user_id: int,
    ) -> None:
        self.swarm_id = swarm_id
        self.team_name = team_name
        self.controller = controller
        self.guild_id = guild_id
        self.workspace_id = workspace_id
        self.workspace_path = workspace_path
        self.forum_channel_id = forum_channel_id
        self.preset_name = preset_name
        self.prompt = prompt
        self.user_id = user_id
        self.agent_threads: dict[str, Optional[int]] = {}
        self.agent_root_messages: dict[str, Optional[int]] = {}
        self.agent_statuses: dict[str, str] = {}
        self.agent_finished: set[str] = set()
        self.output_buffers: dict[str, list[str]] = {}
        self.last_flush_times: dict[str, float] = {}
        self.monitor_task: Optional[asyncio.Task[None]] = None
        self.flush_task: Optional[asyncio.Task[None]] = None
