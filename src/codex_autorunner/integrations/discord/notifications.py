from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ...core.logging_utils import log_event
from ...core.state import now_iso
from .constants import (
    PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
    TOKEN_USAGE_CACHE_LIMIT,
    TOKEN_USAGE_TURN_CACHE_LIMIT,
)

logger = logging.getLogger("codex_autorunner.integrations.discord.notifications")

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


# ---------------------------------------------------------------------------
# Lightweight helpers (avoid pulling in telegram helpers)
# ---------------------------------------------------------------------------


def _coerce_id(value: Any) -> Optional[str]:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _extract_turn_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("turn"), payload.get("item")):
        if not isinstance(candidate, dict):
            continue
        for key in ("threadId", "thread_id"):
            thread_id = _coerce_id(candidate.get(key))
            if thread_id:
                return thread_id
        thread = candidate.get("thread")
        if isinstance(thread, dict):
            thread_id = _coerce_id(
                thread.get("id") or thread.get("threadId") or thread.get("thread_id")
            )
            if thread_id:
                return thread_id
    return None


def _extract_context_usage_percent(
    token_usage: Optional[dict[str, Any]],
) -> Optional[int]:
    if not isinstance(token_usage, dict):
        return None
    usage = None
    last = token_usage.get("last")
    total = token_usage.get("total")
    if isinstance(last, dict):
        usage = last
    elif isinstance(total, dict):
        usage = total
    if usage is None:
        return None
    total_tokens = usage.get("totalTokens")
    context_window = token_usage.get("modelContextWindow")
    if not isinstance(total_tokens, int) or not isinstance(context_window, int):
        return None
    if context_window <= 0:
        return None
    percent_remaining = round((context_window - total_tokens) / context_window * 100)
    return max(0, min(100, 100 - percent_remaining))


def _is_interrupt_status(status: Optional[str]) -> bool:
    if not status:
        return False
    return status.strip().lower() in {
        "interrupted",
        "cancelled",
        "canceled",
        "aborted",
    }


def _extract_command_text(
    item: Optional[dict[str, Any]], params: dict[str, Any]
) -> str:
    command = None
    if isinstance(item, dict):
        command = item.get("command")
    if command is None:
        command = params.get("command")
    if isinstance(command, list):
        return " ".join(str(part) for part in command).strip()
    if isinstance(command, str):
        return command.strip()
    return ""


def _extract_files(params: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in ("files", "fileChanges", "paths"):
        payload = params.get(key)
        if isinstance(payload, list):
            for entry in payload:
                if isinstance(entry, str) and entry:
                    files.append(entry)
                elif isinstance(entry, dict):
                    path = entry.get("path") or entry.get("file") or entry.get("name")
                    if isinstance(path, str) and path:
                        files.append(path)
    return files


def _extract_error_message(params: dict[str, Any]) -> str:
    err = params.get("error")
    if isinstance(err, dict):
        message = err.get("message") if isinstance(err.get("message"), str) else ""
        details = ""
        if isinstance(err.get("additionalDetails"), str):
            details = err["additionalDetails"]
        return (message + " " + details).strip() if (message or details) else ""
    if isinstance(err, str):
        return err
    message = params.get("message")
    if isinstance(message, str):
        return message
    return ""


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

        # ---- Token usage ----
        if method == "thread/tokenUsage/updated":
            thread_id = params.get("threadId")
            turn_id = _coerce_id(params.get("turnId"))
            token_usage = params.get("tokenUsage")
            if not isinstance(thread_id, str) or not isinstance(token_usage, dict):
                return
            self._cache_token_usage(token_usage, turn_id=turn_id, thread_id=thread_id)
            if self._config.progress_stream.enabled:
                await self._note_progress_context_usage(
                    token_usage, turn_id=turn_id, thread_id=thread_id
                )
            return

        # ---- Reasoning delta ----
        if method == "item/reasoning/summaryTextDelta":
            turn_id = _coerce_id(params.get("turnId"))
            thread_id = _extract_turn_thread_id(params)
            delta = params.get("delta")
            if not turn_id or not isinstance(delta, str):
                return
            if self._config.progress_stream.enabled:
                await self._note_progress_thinking(turn_id, delta, thread_id=thread_id)
            return

        if method == "item/reasoning/summaryPartAdded":
            return

        # ---- Item completed ----
        if method == "item/completed":
            item = params.get("item") if isinstance(params, dict) else None
            if isinstance(item, dict) and item.get("type") == "reasoning":
                return
            if self._config.progress_stream.enabled:
                await self._note_progress_item_completed(params)
            return

        # ---- Progress-stream events ----
        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            await self._note_task_needs_approval(params)
            if self._config.progress_stream.enabled:
                await self._note_progress_approval(method, params)
            return

        if self._config.progress_stream.enabled:
            if method == "turn/completed":
                await self._note_progress_turn_completed(params)
                return
            if method == "error":
                await self._note_progress_error(params)
                return
            if isinstance(method, str) and "outputDelta" in method:
                await self._note_progress_output_delta(params)
                return

        # ---- Lifecycle flows ----
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
            notification_channel = getattr(
                self._config, "default_notification_channel_id", None
            )
            if notification_channel:
                await self._send_lifecycle_notification(
                    notification_channel,
                    title="Flow Paused",
                    description=f"Flow **{flow_name or flow_id}** has been paused.",
                )
            await self._post_lifecycle_to_agent_bus(
                "flow_paused",
                {"flow_id": flow_id, "flow_name": flow_name},
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
            await self._post_lifecycle_to_agent_bus(
                "flow_completed",
                {"flow_id": flow_id, "flow_name": flow_name},
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
            await self._post_lifecycle_to_agent_bus(
                "flow_failed",
                {"flow_id": flow_id, "flow_name": flow_name, "error": error},
            )
            return

        log_event(
            self._logger,
            logging.DEBUG,
            "discord.notification.unhandled",
            method=method,
        )

    async def _note_task_needs_approval(self, params: dict[str, Any]) -> None:
        """When an approval is requested, update the forum task tag to needs-approval."""
        try:
            turn_id = _coerce_id(params.get("turnId"))
            thread_id = _extract_turn_thread_id(params)
            if not hasattr(self, "_resolve_turn_context"):
                return
            ctx = self._resolve_turn_context(turn_id, thread_id=thread_id)  # type: ignore[attr-defined]
            if ctx is None:
                return
            guild_id = getattr(ctx, "guild_id", None)
            disc_thread_id = getattr(ctx, "thread_id", None)
            if (
                not isinstance(guild_id, int)
                or isinstance(guild_id, bool)
                or not isinstance(disc_thread_id, int)
                or isinstance(disc_thread_id, bool)
            ):
                return
            await self._update_task_state(guild_id, disc_thread_id, "needs-approval")
        except Exception:
            return

    async def _post_lifecycle_to_agent_bus(self, event_type: str, details: Any) -> None:
        """Post a lifecycle event into the agent bus channel (best-effort)."""
        if not hasattr(self, "_post_to_agent_bus"):
            return
        try:
            from .rendering import build_agent_bus_embed

            embed = build_agent_bus_embed("Autorunner", event_type, details)
            for guild_id in getattr(self._config, "allowed_guild_ids", set()):
                await self._post_to_agent_bus(int(guild_id), "Autorunner", embed)  # type: ignore[attr-defined]
        except Exception:
            return

    async def _post_task_activity(self, guild_id: int, thread_id: int, state: str) -> None:
        """Post a task state update to the workspace activity-feed channel."""
        if not HAS_DISCORD:
            return
        try:
            task = await self._store.get_task(guild_id, thread_id)
        except Exception:
            task = None
        if not isinstance(task, dict):
            return

        workspace_id = task.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            return

        activity_channel_id = None
        try:
            activity_channel_id = await self._store.get_scaffolded_channel(
                guild_id, workspace_id, "activity"
            )
        except Exception:
            activity_channel_id = None
        if not isinstance(activity_channel_id, int) or isinstance(activity_channel_id, bool):
            return

        prompt = task.get("initial_prompt") or ""
        preview = prompt.strip().replace("\n", " ")
        if preview:
            preview = preview[:200]
        else:
            preview = "(no prompt)"

        created_by_user_id = task.get("created_by_user_id")
        initiator = f"<@{created_by_user_id}>" if isinstance(created_by_user_id, int) else ""
        thread_url = f"https://discord.com/channels/{guild_id}/{thread_id}"

        color = None
        state_key = state.strip().lower() if isinstance(state, str) else ""
        if state_key in {"failed", "timeout"}:
            from .constants import EMBED_COLOR_ERROR as _C

            color = _C
        elif state_key in {"needs-approval", "blocked", "stopped"}:
            from .constants import EMBED_COLOR_WARNING as _C

            color = _C
        elif state_key in {"running"}:
            from .constants import EMBED_COLOR_PROGRESS as _C

            color = _C
        elif state_key in {"done"}:
            from .constants import EMBED_COLOR_SUCCESS as _C

            color = _C
        else:
            from .constants import EMBED_COLOR_INFO as _C

            color = _C

        from .rendering import build_response_embed

        title = f"Task {state_key or state}"
        lines = [f"[Task thread]({thread_url}) \u2022 <#{thread_id}>"]
        if initiator:
            lines.append(f"Initiator: {initiator}")
        lines.append(f"State: **{state_key or state}**")
        lines.append(preview)
        embed = build_response_embed("\n".join(lines), color=color, title=title)

        msg_id = await self._send_message(activity_channel_id, embed=embed)
        if msg_id is None:
            return
        try:
            await self._store.update_task_activity(guild_id, thread_id, msg_id)
        except Exception:
            pass

    async def _maybe_send_task_alert(self, guild_id: int, thread_id: int, alert_type: str) -> None:
        """Trigger a role ping for urgent task events (deduped via discord_alerts)."""
        if not HAS_DISCORD:
            return

        alerts = getattr(self._config, "alerts", None)
        if alerts is None or not getattr(alerts, "enabled", False):
            return
        alert_role_id = getattr(alerts, "alert_role_id", None)
        if not isinstance(alert_role_id, int) or not alert_role_id:
            return

        cooldown = int(getattr(alerts, "per_task_cooldown_seconds", 0) or 0)
        should_send = False
        try:
            should_send = await self._store.should_alert(
                guild_id, thread_id, alert_type, cooldown_seconds=cooldown
            )
        except Exception:
            should_send = True
        if not should_send:
            return

        thread = self._bot.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self._bot.fetch_channel(thread_id)
            except Exception:
                thread = None
        if thread is None or not hasattr(thread, "send"):
            return

        kind = (alert_type or "").strip().lower()
        if kind == "timeout":
            text = f"<@&{alert_role_id}> Task **timed out**."
        elif kind == "failed":
            text = f"<@&{alert_role_id}> Task **failed**."
        else:
            text = f"<@&{alert_role_id}> Task alert: **{kind or alert_type}**."

        try:
            await thread.send(text)
        except Exception:
            return

        try:
            await self._store.save_alert(guild_id, thread_id, alert_type)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Progress tracking (mirrors Telegram notification handlers)
    # ------------------------------------------------------------------

    async def _start_turn_progress(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Any,
        agent: str,
        model: Optional[str],
        label: str = "working",
    ) -> None:
        if not self._config.progress_stream.enabled:
            return
        from .progress_stream import TurnProgressTracker

        tracker = TurnProgressTracker(
            started_at=time.monotonic(),
            agent=agent,
            model=model or "default",
            label=label,
            max_actions=self._config.progress_stream.max_actions,
            max_output_chars=self._config.progress_stream.max_output_chars,
        )
        self._turn_progress_trackers[turn_key] = tracker
        self._turn_progress_rendered.pop(turn_key, None)
        self._turn_progress_updated_at.pop(turn_key, None)
        log_event(
            self._logger,
            logging.INFO,
            "discord.progress.started",
            topic_key=ctx.topic_key if ctx else None,
            first_progress_at=now_iso(),
        )
        await self._emit_progress_edit(turn_key, ctx=ctx, force=True)
        heartbeat_task = self._turn_progress_heartbeat_tasks.get(turn_key)
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
        self._turn_progress_heartbeat_tasks[turn_key] = self._spawn_task(
            self._turn_progress_heartbeat(turn_key)
        )

    def _clear_turn_progress(self, turn_key: tuple[str, str]) -> None:
        self._turn_progress_trackers.pop(turn_key, None)
        self._turn_progress_rendered.pop(turn_key, None)
        self._turn_progress_updated_at.pop(turn_key, None)
        self._turn_progress_locks.pop(turn_key, None)
        task = self._turn_progress_tasks.pop(turn_key, None)
        if task and not task.done():
            task.cancel()
        heartbeat_task = self._turn_progress_heartbeat_tasks.pop(turn_key, None)
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()

    async def _note_progress_thinking(
        self, turn_id: str, preview: str, *, thread_id: Optional[str] = None
    ) -> None:
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        # Use add_action for first thinking, update_last_thinking for subsequent
        if tracker.last_thinking_index is None:
            tracker.add_action("thinking", preview, "update", track_thinking=True)
        else:
            tracker.update_last_thinking(preview)
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_context_usage(
        self,
        token_usage: dict[str, Any],
        *,
        turn_id: Optional[str],
        thread_id: Optional[str],
    ) -> None:
        percent = _extract_context_usage_percent(token_usage)
        if percent is None:
            return
        turn_key = None
        if turn_id:
            turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None and len(self._turn_contexts) == 1:
            turn_key = next(iter(self._turn_contexts.keys()))
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        tracker.set_context_usage_percent(percent)
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        turn_id = _coerce_id(params.get("turnId") or item.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        item_type = item.get("type")
        if item_type == "reasoning":
            return
        if item_type == "commandExecution":
            command = _extract_command_text(item, params)
            if command:
                tracker.add_action("command", command, "done")
                tracker.last_output_index = None
        elif item_type == "fileChange":
            files = _extract_files(item)
            summary = ", ".join(files) if files else "Updated files"
            tracker.add_action("files", summary, "done")
        elif item_type == "tool":
            tool = item.get("name") or item.get("tool") or item.get("id") or "Tool call"
            tracker.add_action("tool", str(tool), "done")
            tracker.last_output_index = None
        elif item_type == "agentMessage":
            text = item.get("text") or "Agent message"
            tracker.add_action("agent", str(text), "done")
        else:
            text = item.get("text") or item.get("message") or "Item completed"
            tracker.add_action("item", str(text), "done")
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_approval(
        self, method: str, params: dict[str, Any]
    ) -> None:
        turn_id = _coerce_id(params.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        if method == "item/commandExecution/requestApproval":
            summary = (
                _extract_command_text(None, params) or "Command approval requested"
            )
        elif method == "item/fileChange/requestApproval":
            files = _extract_files(params)
            summary = ", ".join(files) if files else "File approval requested"
        else:
            summary = "Approval requested"
        tracker.add_action("approval", summary, "warn")
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_output_delta(self, params: dict[str, Any]) -> None:
        turn_id = _coerce_id(params.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        delta = params.get("delta") or params.get("text")
        if not isinstance(delta, str):
            return
        if tracker.last_output_index is None:
            tracker.add_action("output", delta, "update", track_output=True)
        else:
            tracker.update_last_output(delta)
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_error(self, params: dict[str, Any]) -> None:
        turn_id = _coerce_id(params.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        message = _extract_error_message(params)
        tracker.add_action("error", message or "App-server error", "fail")
        await self._schedule_progress_edit(turn_key)

    async def _note_progress_turn_completed(self, params: dict[str, Any]) -> None:
        turn_id = _coerce_id(params.get("turnId"))
        thread_id = _extract_turn_thread_id(params)
        turn_key = self._resolve_turn_key(turn_id, thread_id=thread_id)
        if turn_key is None:
            return
        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        status = params.get("status")
        if isinstance(status, str) and _is_interrupt_status(status):
            tracker.set_label("cancelled")
        elif isinstance(status, str) and status and status != "completed":
            tracker.set_label("failed")
        else:
            tracker.set_label("done")
        tracker.finalized = True
        await self._emit_progress_edit(turn_key, force=True)
        self._clear_turn_progress(turn_key)

    async def _schedule_progress_edit(self, turn_key: tuple[str, str]) -> None:
        lock = self._turn_progress_locks.setdefault(turn_key, asyncio.Lock())
        async with lock:
            tracker = self._turn_progress_trackers.get(turn_key)
            ctx = self._turn_contexts.get(turn_key)
            if tracker is None or ctx is None or ctx.placeholder_message_id is None:
                return
            if tracker.finalized:
                return
            min_interval = self._config.progress_stream.min_edit_interval_seconds
            now = time.monotonic()
            last_updated = self._turn_progress_updated_at.get(turn_key, 0.0)
            if (now - last_updated) >= min_interval:
                await self._emit_progress_edit(turn_key, ctx=ctx, now=now)
                return
            if turn_key in self._turn_progress_tasks:
                return
            delay = max(min_interval - (now - last_updated), 0.0)
            task = self._spawn_task(self._delayed_progress_edit(turn_key, delay))
            self._turn_progress_tasks[turn_key] = task

    async def _delayed_progress_edit(
        self, turn_key: tuple[str, str], delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            await self._emit_progress_edit(turn_key)
        finally:
            self._turn_progress_tasks.pop(turn_key, None)

    async def _turn_progress_heartbeat(self, turn_key: tuple[str, str]) -> None:
        try:
            while True:
                await asyncio.sleep(PROGRESS_HEARTBEAT_INTERVAL_SECONDS)
                tracker = self._turn_progress_trackers.get(turn_key)
                if tracker is None or tracker.finalized:
                    return
                ctx = self._turn_contexts.get(turn_key)
                if ctx is None or ctx.placeholder_message_id is None:
                    continue
                now = time.monotonic()
                last_updated = self._turn_progress_updated_at.get(turn_key, 0.0)
                if (now - last_updated) >= PROGRESS_HEARTBEAT_INTERVAL_SECONDS:
                    await self._emit_progress_edit(turn_key, ctx=ctx, now=now)
        finally:
            self._turn_progress_heartbeat_tasks.pop(turn_key, None)

    async def _emit_progress_edit(
        self,
        turn_key: tuple[str, str],
        *,
        ctx: Optional[Any] = None,
        now: Optional[float] = None,
        force: bool = False,
    ) -> None:
        from .progress_stream import render_progress_embed

        tracker = self._turn_progress_trackers.get(turn_key)
        if tracker is None:
            return
        if ctx is None:
            ctx = self._turn_contexts.get(turn_key)
        if ctx is None or ctx.placeholder_message_id is None:
            return
        if now is None:
            now = time.monotonic()
        embed = render_progress_embed(tracker)
        if embed is None:
            return
        # Use embed description as dedup key
        rendered = embed.description or ""
        if not force and rendered == self._turn_progress_rendered.get(turn_key):
            return
        target_id = ctx.thread_id or ctx.channel_id
        ok = await self._edit_message(
            target_id,
            ctx.placeholder_message_id,
            embed=embed,
        )
        if ok:
            self._turn_progress_rendered[turn_key] = rendered
            self._turn_progress_updated_at[turn_key] = now

    # ------------------------------------------------------------------
    # Lifecycle notifications
    # ------------------------------------------------------------------

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
