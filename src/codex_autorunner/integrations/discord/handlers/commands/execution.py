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

            # ---- 6. Start progress tracking ----
            agent = record.agent or DEFAULT_AGENT
            model = record.model or DEFAULT_AGENT_MODELS.get(agent, "default")
            await self._start_turn_progress(
                turn_key, ctx=ctx, agent=agent, model=model, label="working"
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

            # ---- 8. Start the turn ----
            turn_handle = await client.turn_start(
                codex_thread_id,
                prompt,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
            )

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
            await self._deliver_turn_response(
                channel_id,
                thread_id=thread_id,
                placeholder_id=placeholder_id,
                response=response_text,
            )
            # Placeholder was consumed by _deliver_turn_response
            placeholder_id = None

            # ---- 12. Update record ----
            record.turn_count = (record.turn_count or 0) + 1
            record.last_turn_at = now_iso()
            record.updated_at = now_iso()
            await self._store.save_topic(topic_key, record)

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
            semaphore.release()

    async def _cmd_run_impl(self, interaction: Any, prompt: str) -> None:
        """Implementation for /run slash command."""
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

        if self._is_turn_active(topic_key):
            await interaction.followup.send(
                "A task is already running in this context. Use `/stop` first."
            )
            return

        # Check workspace binding
        channel_key = f"{guild_id}:{channel_id}"
        binding = await self._store.get_channel_binding(channel_key)
        if binding is None:
            await interaction.followup.send(
                "No workspace bound. Use `/bind <path>` first."
            )
            return

        # Create or get topic
        record = await self._store.get_topic(topic_key)
        if record is None:
            from ...state import DiscordTopicRecord

            record = DiscordTopicRecord(
                topic_key=topic_key,
                guild_id=guild_id,
                channel_id=channel_id,
                thread_id=thread_id,
                workspace_path=binding,
                approval_mode=self._config.defaults.approval_mode,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
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
