from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from ....core.logging_utils import log_event
from .commands_spec import build_slash_command_specs

if TYPE_CHECKING:
    pass

try:
    import discord
    from discord import app_commands

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

logger = logging.getLogger(
    "codex_autorunner.integrations.discord.handlers.commands_runtime"
)


class DiscordCommandHandlers:
    """Mixin that registers Discord slash commands on the bot's CommandTree."""

    def _register_slash_commands(self) -> None:
        """Register all slash commands on the bot's command tree."""
        if not HAS_DISCORD:
            return

        tree = self._bot.tree
        specs = build_slash_command_specs()

        @tree.command(name="run", description="Run an agent task")
        @app_commands.describe(prompt="The task to run")
        async def cmd_run(interaction: discord.Interaction, prompt: str) -> None:
            await self._handle_slash_run(interaction, prompt)

        @tree.command(name="stop", description="Stop the active task")
        async def cmd_stop(interaction: discord.Interaction) -> None:
            await self._handle_slash_stop(interaction)

        @tree.command(name="bind", description="Bind this channel to a workspace")
        @app_commands.describe(workspace="Path to the workspace directory")
        async def cmd_bind(
            interaction: discord.Interaction, workspace: Optional[str] = None
        ) -> None:
            await self._handle_slash_bind(interaction, workspace)

        @tree.command(name="repos", description="List available repositories")
        async def cmd_repos(interaction: discord.Interaction) -> None:
            await self._handle_slash_repos(interaction)

        @tree.command(name="status", description="Show current configuration")
        async def cmd_status(interaction: discord.Interaction) -> None:
            await self._handle_slash_status(interaction)

        @tree.command(name="new", description="Start a new conversation")
        async def cmd_new(interaction: discord.Interaction) -> None:
            await self._handle_slash_new(interaction)

        @tree.command(name="resume", description="Resume a previous conversation")
        @app_commands.describe(thread_id="Thread ID to resume (optional)")
        async def cmd_resume(
            interaction: discord.Interaction, thread_id: Optional[str] = None
        ) -> None:
            await self._handle_slash_resume(interaction, thread_id)

        @tree.command(name="model", description="Show or change the model")
        @app_commands.describe(name="Model name to switch to (optional)")
        async def cmd_model(
            interaction: discord.Interaction, name: Optional[str] = None
        ) -> None:
            await self._handle_slash_model(interaction, name)

        @tree.command(name="agent", description="Show or change the agent")
        @app_commands.describe(name="Agent name to switch to (optional)")
        async def cmd_agent(
            interaction: discord.Interaction, name: Optional[str] = None
        ) -> None:
            await self._handle_slash_agent(interaction, name)

        @tree.command(name="approvals", description="Set approval and sandbox policy")
        @app_commands.describe(mode="Approval mode: safe or yolo")
        async def cmd_approvals(
            interaction: discord.Interaction, mode: Optional[str] = None
        ) -> None:
            await self._handle_slash_approvals(interaction, mode)

        @tree.command(name="health", description="Run health diagnostics")
        async def cmd_health(interaction: discord.Interaction) -> None:
            await self._handle_slash_health(interaction)

        log_event(
            self._logger,
            logging.INFO,
            "discord.commands.registered",
            command_count=len(specs),
        )

    # ------------------------------------------------------------------
    # Slash command handler stubs (to be implemented in command modules)
    # ------------------------------------------------------------------

    async def _handle_slash_run(self, interaction: Any, prompt: str) -> None:
        await interaction.response.defer()
        await interaction.followup.send(
            "Run command received. Execution will be wired in Phase 4."
        )

    async def _handle_slash_stop(self, interaction: Any) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("Stop command received.", ephemeral=True)

    async def _handle_slash_bind(
        self, interaction: Any, workspace: Optional[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not workspace:
            await interaction.followup.send(
                "Usage: `/bind workspace:/path/to/repo`", ephemeral=True
            )
            return

        from pathlib import Path

        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            await interaction.followup.send(
                f"Path is not a valid directory: `{workspace}`", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        channel = interaction.channel
        if isinstance(channel, discord.Thread):
            channel_id = channel.parent_id
        else:
            channel_id = channel.id

        channel_key = f"{guild_id}:{channel_id}"
        await self._store.set_channel_binding(channel_key, str(workspace_path))

        from ..rendering import build_status_embed

        embed = build_status_embed(workspace=str(workspace_path))
        await interaction.followup.send(
            f"Bound to `{workspace_path}`", embed=embed, ephemeral=True
        )

    async def _handle_slash_repos(self, interaction: Any) -> None:
        await interaction.response.defer(ephemeral=True)
        if self._hub_supervisor is None:
            await interaction.followup.send("No hub configured.", ephemeral=True)
            return
        try:
            repos = self._hub_supervisor.list_repos()
            if not repos:
                await interaction.followup.send(
                    "No repositories found.", ephemeral=True
                )
                return
            from ..rendering import build_repos_embed

            repo_dicts = [
                {
                    "name": r.name if hasattr(r, "name") else str(r),
                    "path": str(r.path) if hasattr(r, "path") else str(r),
                }
                for r in repos
            ]
            embed = build_repos_embed(repo_dicts)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(
                f"Failed to list repos: {exc}", ephemeral=True
            )

    async def _handle_slash_status(self, interaction: Any) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        channel = interaction.channel
        if isinstance(channel, discord.Thread):
            channel_id = channel.parent_id
            thread_id = channel.id
        else:
            channel_id = channel.id
            thread_id = None

        from ..helpers import build_topic_key

        topic_key = build_topic_key(guild_id, channel_id, thread_id)
        record = await self._store.get_topic(topic_key)

        from ..rendering import build_status_embed

        embed = build_status_embed(
            workspace=record.workspace_path if record else None,
            agent=record.agent if record else None,
            model=record.model if record else None,
            approval_mode=record.approval_mode if record else None,
            thread_id=record.codex_thread_id if record else None,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_slash_new(self, interaction: Any) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("New conversation started.", ephemeral=True)

    async def _handle_slash_resume(
        self, interaction: Any, thread_id: Optional[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("Resume not yet implemented.", ephemeral=True)

    async def _handle_slash_model(self, interaction: Any, name: Optional[str]) -> None:
        await interaction.response.defer(ephemeral=True)
        if name:
            await interaction.followup.send(f"Model set to `{name}`.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Current model: (use `/model <name>` to change)", ephemeral=True
            )

    async def _handle_slash_agent(self, interaction: Any, name: Optional[str]) -> None:
        await interaction.response.defer(ephemeral=True)
        if name:
            await interaction.followup.send(f"Agent set to `{name}`.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Current agent: (use `/agent <name>` to change)", ephemeral=True
            )

    async def _handle_slash_approvals(
        self, interaction: Any, mode: Optional[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if mode:
            from ..state import normalize_approval_mode

            normalized = normalize_approval_mode(mode)
            await interaction.followup.send(
                f"Approval mode set to `{normalized}`.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Current approval mode: (use `/approvals <mode>` to change)",
                ephemeral=True,
            )

    async def _handle_slash_health(self, interaction: Any) -> None:
        await interaction.response.defer(ephemeral=True)
        from ..doctor import format_health_results, run_health_check

        results = await run_health_check(
            token=self._config.bot_token,
            state_path=self._config.state_file,
            skip_gateway=True,
        )
        text = format_health_results(results)
        await interaction.followup.send(f"```\n{text}\n```", ephemeral=True)
