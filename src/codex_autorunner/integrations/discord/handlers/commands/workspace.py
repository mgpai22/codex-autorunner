from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(
    "codex_autorunner.integrations.discord.handlers.commands.workspace"
)


class WorkspaceCommands:
    """Mixin providing workspace-related slash command implementations.

    These are wired to the slash command stubs in commands_runtime.py.
    Full implementations will be connected when the command tree matures.
    """

    async def _cmd_bind_impl(self, interaction: Any, workspace: Optional[str]) -> None:
        """Implementation for /bind slash command."""
        if not workspace:
            # Show current binding
            guild_id = interaction.guild_id
            channel = interaction.channel
            import discord as _discord

            if isinstance(channel, _discord.Thread):
                channel_id = channel.parent_id
            else:
                channel_id = channel.id
            channel_key = f"{guild_id}:{channel_id}"
            binding = await self._store.get_channel_binding(channel_key)
            if binding:
                await interaction.followup.send(f"Currently bound to `{binding}`")
            else:
                await interaction.followup.send(
                    "No workspace bound. Use `/bind <path>`"
                )
            return

        from pathlib import Path

        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            await interaction.followup.send(
                f"Path is not a valid directory: `{workspace_path}`"
            )
            return

        guild_id = interaction.guild_id
        channel = interaction.channel
        import discord as _discord

        if isinstance(channel, _discord.Thread):
            channel_id = channel.parent_id
        else:
            channel_id = channel.id
        channel_key = f"{guild_id}:{channel_id}"
        await self._store.set_channel_binding(channel_key, str(workspace_path))

        from ...rendering import build_status_embed

        embed = build_status_embed(workspace=str(workspace_path))
        await interaction.followup.send(f"Bound to `{workspace_path}`", embed=embed)

    async def _cmd_status_impl(self, interaction: Any) -> None:
        """Implementation for /status slash command."""
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

        from ...rendering import build_status_embed

        embed = build_status_embed(
            workspace=record.workspace_path if record else None,
            agent=record.agent if record else None,
            model=record.model if record else None,
            approval_mode=record.approval_mode if record else None,
            thread_id=record.codex_thread_id if record else None,
            guild_name=interaction.guild.name if interaction.guild else None,
        )
        is_active = self._is_turn_active(topic_key)
        if is_active:
            embed.add_field(name="Turn", value="Active", inline=True)
        await interaction.followup.send(embed=embed)
