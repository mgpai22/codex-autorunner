from __future__ import annotations

import logging
from typing import Any

from .....core.logging_utils import log_event
from .....core.state import now_iso

logger = logging.getLogger(
    "codex_autorunner.integrations.discord.handlers.commands.execution"
)


class ExecutionCommands:
    """Mixin providing execution-related slash command implementations."""

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
            await self._store.save_topic(record)

        # Send placeholder
        target_id = thread_id or channel_id
        await self._send_placeholder(target_id, "Working...")

        log_event(
            self._logger,
            logging.INFO,
            "discord.run.started",
            topic_key=topic_key,
            prompt_preview=prompt[:80],
        )

        # The actual agent turn execution will be fully wired when
        # the app-server streaming integration is connected.
        await interaction.followup.send(f"Task started: {prompt[:200]}")

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
            await self._store.save_topic(record)

        await interaction.followup.send(
            "New conversation started. Previous context cleared."
        )
