from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .....core.logging_utils import log_event
from .....core.state import now_iso
from ...constants import (
    DEFAULT_AGENT,
    DEFAULT_AGENT_MODELS,
    DEFAULT_AGENT_TURN_TIMEOUT_SECONDS,
    TurnKey,
)
from ...types import TurnContext

logger = logging.getLogger(
    "codex_autorunner.integrations.discord.handlers.commands.execution"
)


def _build_turn_id(codex_thread_id: str) -> str:
    """Generate a unique turn id from the codex thread id + timestamp."""
    return f"{codex_thread_id}:{int(time.time() * 1000)}"


class ExecutionCommands:
    """Mixin providing execution-related slash command implementations."""

    async def _execute_turn(
        self,
        topic_key: str,
        prompt: str,
        *,
        channel_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        record: Any,
    ) -> None:
        """Orchestrate a full agent turn lifecycle.

        Shared by both ``_cmd_run_impl`` (slash command) and
        ``_coalesce_timer`` (mention-triggered messages).
        """
        target_id = thread_id or channel_id
        turn_key: Optional[TurnKey] = None
        placeholder_id: Optional[int] = None
        semaphore = self._ensure_turn_semaphore()

        try:
            # ---- 1. Acquire semaphore ----
            await semaphore.acquire()

            # ---- 2. Get app-server client ----
            client = await self._client_for_workspace(record.workspace_path)
            if client is None:
                await self._send_message(
                    target_id, "App-server unavailable for this workspace."
                )
                return

            # ---- 3. Get or create codex thread ----
            codex_thread_id = record.codex_thread_id
            if not codex_thread_id:
                thread_result = await client.thread_start(
                    cwd=record.workspace_path or "."
                )
                codex_thread_id = thread_result.get("id")
                if not codex_thread_id:
                    await self._send_message(target_id, "Failed to start a new thread.")
                    return
                record.codex_thread_id = codex_thread_id
                record.updated_at = now_iso()
                await self._store.save_topic(topic_key, record)
            else:
                await self._ensure_thread_alive(client, codex_thread_id, topic_key)

            # ---- 4. Send placeholder ----
            placeholder_id = await self._send_placeholder(target_id, "Working...")

            # ---- 5. Build turn id and create TurnContext ----
            turn_id = _build_turn_id(codex_thread_id)
            turn_key = (topic_key, turn_id)

            ctx = TurnContext(
                topic_key=topic_key,
                guild_id=record.guild_id,
                channel_id=channel_id,
                thread_id=thread_id,
                codex_thread_id=codex_thread_id,
                reply_to_message_id=reply_to,
                placeholder_message_id=placeholder_id,
            )
            self._turn_contexts[turn_key] = ctx

            try:
                await self._update_presence()
            except Exception:
                pass

            # ---- 6. Start progress tracking ----
            agent = record.agent or DEFAULT_AGENT
            model = record.model or DEFAULT_AGENT_MODELS.get(agent, "default")
            await self._start_turn_progress(
                turn_key,
                ctx=ctx,
                agent=agent,
                model=model,
                effort=getattr(record, "reasoning_effort", None),
                label="working",
            )

            log_event(
                self._logger,
                logging.INFO,
                "discord.turn.started",
                topic_key=topic_key,
                turn_id=turn_id,
                codex_thread_id=codex_thread_id,
                prompt_preview=prompt[:80],
            )

            # ---- 7. Resolve policies ----
            approval_policy, sandbox_policy = self._config.defaults.policies_for_mode(
                record.approval_mode
            )
            if record.approval_policy:
                approval_policy = record.approval_policy
            if record.sandbox_policy:
                sandbox_policy = record.sandbox_policy

            if (
                isinstance(ctx.guild_id, int)
                and not isinstance(ctx.guild_id, bool)
                and isinstance(thread_id, int)
                and not isinstance(thread_id, bool)
            ):
                try:
                    await self._update_task_state(ctx.guild_id, thread_id, "running")
                except Exception:
                    pass

            # ---- 8. Start the turn ----
            turn_kwargs: dict[str, Any] = {}
            if record.agent:
                turn_kwargs["agent"] = record.agent
            if record.model:
                turn_kwargs["model"] = record.model
            if getattr(record, "reasoning_effort", None):
                turn_kwargs["effort"] = record.reasoning_effort

            try:
                turn_handle = await client.turn_start(
                    codex_thread_id,
                    prompt,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                    **turn_kwargs,
                )
            except Exception as turn_exc:
                # If the thread was stale (e.g. app-server restarted), try to
                # resume the session from disk before falling back to a fresh thread.
                if "thread not found" in str(turn_exc).lower():
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.turn.stale_thread",
                        topic_key=topic_key,
                        stale_thread_id=codex_thread_id,
                    )
                    # Attempt resume from persisted session files
                    resumed = False
                    try:
                        await client.thread_resume(codex_thread_id)
                        turn_handle = await client.turn_start(
                            codex_thread_id,
                            prompt,
                            approval_policy=approval_policy,
                            sandbox_policy=sandbox_policy,
                            **turn_kwargs,
                        )
                        resumed = True
                        log_event(
                            self._logger,
                            logging.INFO,
                            "discord.turn.resumed",
                            topic_key=topic_key,
                            codex_thread_id=codex_thread_id,
                        )
                    except Exception as resume_exc:
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "discord.turn.resume_failed",
                            topic_key=topic_key,
                            codex_thread_id=codex_thread_id,
                            exc=resume_exc,
                        )
                    if not resumed:
                        # Fall back to fresh thread
                        thread_result = await client.thread_start(
                            cwd=record.workspace_path or "."
                        )
                        codex_thread_id = thread_result.get("id")
                        if not codex_thread_id:
                            raise RuntimeError("Failed to start fresh thread after stale thread") from turn_exc
                        record.codex_thread_id = codex_thread_id
                        record.updated_at = now_iso()
                        await self._store.save_topic(topic_key, record)
                        turn_handle = await client.turn_start(
                            codex_thread_id,
                            prompt,
                            approval_policy=approval_policy,
                            sandbox_policy=sandbox_policy,
                            **turn_kwargs,
                        )
                else:
                    raise

            # ---- 9. Wait for completion ----
            agent_timeout = self._config.agent_turn_timeout_seconds.get(
                agent,
                DEFAULT_AGENT_TURN_TIMEOUT_SECONDS.get(agent, 28800.0),
            )
            result = await turn_handle.wait(timeout=agent_timeout)

            # ---- 10. Compose response ----
            if result.agent_messages:
                response_text = "\n\n".join(result.agent_messages)
            elif result.errors:
                response_text = "Error: " + "; ".join(result.errors)
            else:
                response_text = "(No agent response.)"

            # ---- 11. Deliver response ----
            response_message_id = await self._deliver_turn_response(
                channel_id,
                thread_id=thread_id,
                placeholder_id=placeholder_id,
                response=response_text,
            )
            # Placeholder was consumed by _deliver_turn_response
            placeholder_id = None

            agent_response_url: Optional[str] = None
            if (
                isinstance(ctx.guild_id, int)
                and not isinstance(ctx.guild_id, bool)
                and isinstance(thread_id, int)
                and not isinstance(thread_id, bool)
                and isinstance(response_message_id, int)
                and response_message_id
            ):
                agent_response_url = (
                    f"https://discord.com/channels/{ctx.guild_id}/"
                    f"{target_id}/{response_message_id}"
                )

            # ---- 12. Update record ----
            record.turn_count = (record.turn_count or 0) + 1
            record.last_turn_at = now_iso()
            record.updated_at = now_iso()
            await self._store.save_topic(topic_key, record)

            end_state = "done"
            status = result.status
            if isinstance(status, str):
                status_key = status.strip().lower()
                if status_key and status_key != "completed":
                    if status_key in {"interrupted", "cancelled", "canceled", "aborted"}:
                        end_state = "stopped"
                    elif status_key == "timeout":
                        end_state = "timeout"
                    else:
                        end_state = "failed"

            if (
                isinstance(ctx.guild_id, int)
                and not isinstance(ctx.guild_id, bool)
                and isinstance(thread_id, int)
                and not isinstance(thread_id, bool)
            ):
                try:
                    await self._update_task_state(
                        ctx.guild_id,
                        thread_id,
                        end_state,
                        agent_response_url=agent_response_url,
                    )
                except Exception:
                    pass
                if end_state in {"failed", "timeout"}:
                    try:
                        await self._maybe_send_task_alert(
                            ctx.guild_id, thread_id, end_state
                        )
                    except Exception:
                        pass

            log_event(
                self._logger,
                logging.INFO,
                "discord.turn.completed",
                topic_key=topic_key,
                turn_id=turn_id,
                status=result.status,
                response_length=len(response_text),
            )

        except asyncio.TimeoutError:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.turn.timeout",
                topic_key=topic_key,
            )
            if (
                isinstance(getattr(record, "guild_id", None), int)
                and not isinstance(getattr(record, "guild_id", None), bool)
                and isinstance(thread_id, int)
                and not isinstance(thread_id, bool)
            ):
                try:
                    await self._update_task_state(record.guild_id, thread_id, "timeout")
                except Exception:
                    pass
                try:
                    await self._maybe_send_task_alert(record.guild_id, thread_id, "timeout")
                except Exception:
                    pass
            if placeholder_id:
                await self._edit_message(target_id, placeholder_id, "Turn timed out.")
                placeholder_id = None

        except asyncio.CancelledError:
            log_event(
                self._logger,
                logging.INFO,
                "discord.turn.cancelled",
                topic_key=topic_key,
            )
            if (
                isinstance(getattr(record, "guild_id", None), int)
                and not isinstance(getattr(record, "guild_id", None), bool)
                and isinstance(thread_id, int)
                and not isinstance(thread_id, bool)
            ):
                try:
                    await self._update_task_state(record.guild_id, thread_id, "stopped")
                except Exception:
                    pass
            if placeholder_id:
                await self._edit_message(target_id, placeholder_id, "Turn cancelled.")
                placeholder_id = None

        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.turn.error",
                topic_key=topic_key,
                exc=exc,
            )
            if (
                isinstance(getattr(record, "guild_id", None), int)
                and not isinstance(getattr(record, "guild_id", None), bool)
                and isinstance(thread_id, int)
                and not isinstance(thread_id, bool)
            ):
                try:
                    await self._update_task_state(record.guild_id, thread_id, "failed")
                except Exception:
                    pass
                try:
                    await self._maybe_send_task_alert(record.guild_id, thread_id, "failed")
                except Exception:
                    pass
            error_text = f"Turn failed: {exc}"
            if len(error_text) > 200:
                error_text = error_text[:200] + "..."
            if placeholder_id:
                await self._edit_message(target_id, placeholder_id, error_text)
                placeholder_id = None
            else:
                await self._send_message(target_id, error_text)

        finally:
            # ---- 13. Cleanup ----
            if turn_key is not None:
                self._turn_contexts.pop(turn_key, None)
                self._clear_turn_progress(turn_key)
            try:
                await self._update_presence()
            except Exception:
                pass
            semaphore.release()

    async def _ensure_thread_alive(self, client: Any, codex_thread_id: str, topic_key: str) -> None:
        """Proactively resume a codex thread so the app-server has it loaded.

        Non-fatal — errors are logged and swallowed so the turn can still proceed.
        """
        try:
            await client.thread_resume(codex_thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.thread.resume_failed",
                topic_key=topic_key,
                codex_thread_id=codex_thread_id,
                exc=exc,
            )

    async def _cmd_run_impl(
        self, interaction: Any, prompt: str,
        *, model: Optional[str] = None, effort: Optional[str] = None,
    ) -> None:
        """Implementation for /run slash command."""
        import discord as _discord

        guild_id = interaction.guild_id
        channel = interaction.channel
        in_thread = isinstance(channel, _discord.Thread)
        if in_thread:
            channel_id = channel.parent_id
            thread_id = channel.id
        else:
            channel_id = channel.id
            thread_id = None

        # Check workspace binding
        channel_key = f"{guild_id}:{channel_id}"
        binding = await self._store.get_channel_binding(channel_key)
        if binding is None:
            await interaction.followup.send(
                "No workspace bound. Use `/bind <path>` first."
            )
            return

        # Apply defaults for model and effort
        effective_model = model or "gpt-5.3-codex"
        effective_effort = effort or "medium"

        from ...helpers import build_topic_key

        workspace_id: Optional[str] = None
        tasks_forum_id: Optional[int] = None

        # Fast-path: if invoked inside a scaffolded tasks channel, we know the workspace id.
        try:
            rows = await self._store.list_scaffolded_channels(guild_id)
        except Exception:
            rows = []

        for _gid, ws_id, ch_type, ch_id, _created_at in rows:
            if ch_type == "tasks" and ch_id == channel_id:
                workspace_id = ws_id
                tasks_forum_id = ch_id
                break
            if ch_type == "run" and ch_id == channel_id:
                workspace_id = ws_id
                break

        # Otherwise: map the bound workspace path back to a hub repo id.
        if workspace_id is None and self._hub_supervisor is not None:
            try:
                from pathlib import Path

                binding_path = Path(binding).expanduser().resolve()
                repos = self._hub_supervisor.list_repos()
                for repo in repos:
                    repo_id = getattr(repo, "id", None)
                    repo_path = getattr(repo, "path", None)
                    if not isinstance(repo_id, str) or not repo_id:
                        continue
                    if repo_path is None:
                        continue
                    try:
                        candidate = Path(str(repo_path)).expanduser().resolve()
                    except Exception:
                        continue
                    if candidate == binding_path:
                        workspace_id = repo_id
                        break
            except Exception:
                workspace_id = None

        if (
            thread_id is None
            and isinstance(workspace_id, str)
            and workspace_id
            and tasks_forum_id is None
        ):
            tasks_forum_id = await self._store.get_scaffolded_channel(
                guild_id, workspace_id, "tasks"
            )

        # Forum-backed tasks: /run in a bound workspace creates a forum thread when scaffolded.
        channel_kind = str(self._config.scaffold.tasks_channel_kind or "").strip().lower()
        if (
            not in_thread
            and thread_id is None
            and channel_kind == "forum"
            and isinstance(workspace_id, str)
            and workspace_id
            and isinstance(tasks_forum_id, int)
            and not isinstance(tasks_forum_id, bool)
        ):
            guild = interaction.guild
            if guild is None:
                await interaction.followup.send("`/run` must be run inside a guild.")
                return

            try:
                thread, _root = await self._create_forum_task(
                    guild, workspace_id, tasks_forum_id, prompt, interaction.user
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.task.create_failed",
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    forum_channel_id=tasks_forum_id,
                    exc=exc,
                )
                await interaction.followup.send(f"Failed to create task: {exc}")
                return

            channel_id = tasks_forum_id
            thread_id = thread.id
            topic_key = build_topic_key(guild_id, channel_id, thread_id)

            # Ensure the tasks forum itself is bound for reruns and state resolution.
            try:
                forum_key = f"{guild_id}:{tasks_forum_id}"
                existing = await self._store.get_channel_binding(forum_key)
                if existing is None:
                    await self._store.set_channel_binding(forum_key, binding)
            except Exception:
                pass

            record = await self._store.get_topic(topic_key)
            if record is None:
                from ...state import DiscordTopicRecord

                approval_mode = self._config.defaults.approval_mode
                try:
                    mode_override = self._resolve_approval_mode_for_user(interaction)
                    if isinstance(mode_override, str) and mode_override:
                        approval_mode = mode_override
                except Exception:
                    pass

                record = DiscordTopicRecord(
                    topic_key=topic_key,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    workspace_path=binding,
                    approval_mode=approval_mode,
                    model=effective_model,
                    reasoning_effort=effective_effort,
                    created_at=now_iso(),
                    updated_at=now_iso(),
                )
                await self._store.save_topic(topic_key, record)
            elif not record.workspace_path:
                record.workspace_path = binding
                record.model = effective_model
                record.reasoning_effort = effective_effort
                record.updated_at = now_iso()
                await self._store.save_topic(topic_key, record)
            else:
                record.model = effective_model
                record.reasoning_effort = effective_effort
                record.updated_at = now_iso()
                await self._store.save_topic(topic_key, record)

            log_event(
                self._logger,
                logging.INFO,
                "discord.run.started",
                topic_key=topic_key,
                prompt_preview=prompt[:80],
            )

            try:
                await interaction.followup.send(
                    f"Task created: <#{thread.id}>", ephemeral=True
                )
            except Exception:
                pass

            self._spawn_task(
                self._execute_turn(
                    topic_key,
                    prompt,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    reply_to=None,
                    record=record,
                )
            )
            return

        topic_key = build_topic_key(guild_id, channel_id, thread_id)

        if self._is_turn_active(topic_key):
            await interaction.followup.send(
                "A task is already running in this context. Use `/stop` first."
            )
            return

        record = await self._store.get_topic(topic_key)
        if record is None:
            from ...state import DiscordTopicRecord

            approval_mode = self._config.defaults.approval_mode
            try:
                mode_override = self._resolve_approval_mode_for_user(interaction)
                if isinstance(mode_override, str) and mode_override:
                    approval_mode = mode_override
            except Exception:
                pass

            record = DiscordTopicRecord(
                topic_key=topic_key,
                guild_id=guild_id,
                channel_id=channel_id,
                thread_id=thread_id,
                workspace_path=binding,
                approval_mode=approval_mode,
                model=effective_model,
                reasoning_effort=effective_effort,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            await self._store.save_topic(topic_key, record)
        elif not record.workspace_path:
            record.workspace_path = binding
            record.model = effective_model
            record.reasoning_effort = effective_effort
            record.updated_at = now_iso()
            await self._store.save_topic(topic_key, record)
        else:
            record.model = effective_model
            record.reasoning_effort = effective_effort
            record.updated_at = now_iso()
            await self._store.save_topic(topic_key, record)

        log_event(
            self._logger,
            logging.INFO,
            "discord.run.started",
            topic_key=topic_key,
            prompt_preview=prompt[:80],
        )

        await interaction.followup.send(f"Task started: {prompt[:200]}")

        # Spawn the turn as a background task
        self._spawn_task(
            self._execute_turn(
                topic_key,
                prompt,
                channel_id=channel_id,
                thread_id=thread_id,
                reply_to=None,
                record=record,
            )
        )

    async def _cmd_stop_impl(self, interaction: Any) -> None:
        """Implementation for /stop slash command."""
        import discord as _discord

        guild_id = interaction.guild_id
        channel = interaction.channel
        if isinstance(channel, _discord.Thread):
            channel_id = channel.parent_id
            thread_id = channel.id
        else:
            channel_id = channel.id
            thread_id = None

        from ...helpers import build_topic_key

        topic_key = build_topic_key(guild_id, channel_id, thread_id)

        if not self._is_turn_active(topic_key):
            await interaction.followup.send("No active task to stop.", ephemeral=True)
            return

        ok = await self._interrupt_turn(topic_key)
        if ok:
            if thread_id is not None:
                try:
                    await self._update_task_state(guild_id, thread_id, "stopped")
                except Exception:
                    pass
            await interaction.followup.send("Task interrupted.", ephemeral=True)
        else:
            await interaction.followup.send("Failed to interrupt task.", ephemeral=True)

    async def _cmd_new_impl(self, interaction: Any) -> None:
        """Implementation for /new slash command."""
        import discord as _discord

        guild_id = interaction.guild_id
        channel = interaction.channel
        if isinstance(channel, _discord.Thread):
            channel_id = channel.parent_id
            thread_id = channel.id
        else:
            channel_id = channel.id
            thread_id = None

        from ...helpers import build_topic_key

        topic_key = build_topic_key(guild_id, channel_id, thread_id)

        record = await self._store.get_topic(topic_key)
        if record is not None:
            record.codex_thread_id = None
            record.updated_at = now_iso()
            await self._store.save_topic(topic_key, record)

        await interaction.followup.send(
            "New conversation started. Previous context cleared."
        )
