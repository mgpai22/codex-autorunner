from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from ....core.logging_utils import log_event
from ....core.state import now_iso
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

        @tree.command(
            name="setup",
            description="Scaffold swarm control surface channels",
        )
        @app_commands.default_permissions(administrator=True)
        async def cmd_setup(interaction: discord.Interaction) -> None:
            if hasattr(self, "_check_rbac"):
                allowed = await self._check_rbac(interaction, "can_setup")  # type: ignore[attr-defined]
                if not allowed:
                    try:
                        await interaction.response.send_message(
                            "You do not have permission to run `/setup`.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return
            await interaction.response.defer()
            await self._cmd_setup_impl(interaction)

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

        @cmd_model.autocomplete("name")
        async def model_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            return await self._autocomplete_model(interaction, current)

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

        @tree.command(name="workspaces", description="List scaffolded workspaces")
        async def cmd_workspaces(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            await self._cmd_workspaces_impl(interaction)

        tasks_group = app_commands.Group(
            name="tasks", description="Task triage and navigation"
        )

        @tasks_group.command(name="list", description="List tasks")
        @app_commands.describe(workspace="Workspace id (optional)", tag="Tag/state (optional)")
        async def cmd_tasks_list(
            interaction: discord.Interaction,
            workspace: Optional[str] = None,
            tag: Optional[str] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            await self._cmd_tasks_list_impl(interaction, workspace, tag)

        @tasks_group.command(name="mine", description="List tasks created by you")
        async def cmd_tasks_mine(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            await self._cmd_tasks_mine_impl(interaction)

        tree.add_command(tasks_group)

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
        if hasattr(self, "_check_rbac"):
            allowed = await self._check_rbac(interaction, "can_run")  # type: ignore[attr-defined]
            if not allowed:
                try:
                    await interaction.response.send_message(
                        "You do not have permission to run tasks.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return
        await interaction.response.defer()
        await self._cmd_run_impl(interaction, prompt)

    async def _handle_slash_stop(self, interaction: Any) -> None:
        if hasattr(self, "_check_rbac"):
            allowed = await self._check_rbac(interaction, "can_stop")  # type: ignore[attr-defined]
            if not allowed:
                try:
                    await interaction.response.send_message(
                        "You do not have permission to stop tasks.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return
        await interaction.response.defer(ephemeral=True)
        await self._cmd_stop_impl(interaction)

    async def _handle_slash_bind(
        self, interaction: Any, workspace: Optional[str]
    ) -> None:
        if hasattr(self, "_check_rbac"):
            allowed = await self._check_rbac(interaction, "can_bind")  # type: ignore[attr-defined]
            if not allowed:
                try:
                    await interaction.response.send_message(
                        "You do not have permission to bind workspaces.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return
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
        await self._cmd_new_impl(interaction)

    async def _handle_slash_resume(
        self, interaction: Any, thread_id: Optional[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        channel = interaction.channel
        if isinstance(channel, discord.Thread):
            channel_id = channel.parent_id
            disc_thread_id = channel.id
        else:
            channel_id = channel.id
            disc_thread_id = None

        from ..helpers import build_topic_key

        topic_key = build_topic_key(guild_id, channel_id, disc_thread_id)
        record = await self._store.get_topic(topic_key)

        if record is None:
            await interaction.followup.send(
                "No topic found. Use `/bind` first.", ephemeral=True
            )
            return

        if not record.workspace_path:
            await interaction.followup.send(
                "No workspace bound. Use `/bind` first.", ephemeral=True
            )
            return

        if thread_id:
            # Direct resume with explicit thread ID
            record.codex_thread_id = thread_id
            record.updated_at = now_iso()
            await self._store.save_topic(topic_key, record)
            await interaction.followup.send(
                f"Resumed thread `{thread_id}`.", ephemeral=True
            )
        else:
            current = record.codex_thread_id
            if current:
                await interaction.followup.send(
                    f"Current thread: `{current}`.\n"
                    "Use `/resume <thread_id>` to switch, "
                    "or `/new` to start fresh.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "No active thread. Use `/run` to start one, "
                    "or `/resume <thread_id>` to resume a specific thread.",
                    ephemeral=True,
                )

    async def _autocomplete_model(
        self, interaction: Any, current: str
    ) -> list[Any]:
        """Return model choices for the /model autocomplete."""
        from discord import app_commands

        choices: list[app_commands.Choice[str]] = []

        # Try to fetch live model list from the app-server.
        try:
            guild_id = interaction.guild_id
            channel = interaction.channel
            if isinstance(channel, discord.Thread):
                channel_id = channel.parent_id
            else:
                channel_id = channel.id
            channel_key = f"{guild_id}:{channel_id}"
            binding = await self._store.get_channel_binding(channel_key)
            if binding:
                client = await self._client_for_workspace(binding)
                if client is not None:
                    from ..constants import DEFAULT_AGENT, DEFAULT_MODEL_LIST_LIMIT

                    record = None
                    try:
                        from ..helpers import build_topic_key

                        thread_id = (
                            channel.id
                            if isinstance(channel, discord.Thread)
                            else None
                        )
                        topic_key = build_topic_key(guild_id, channel_id, thread_id)
                        record = await self._store.get_topic(topic_key)
                    except Exception:
                        pass
                    agent = (
                        (record.agent if record else None) or DEFAULT_AGENT
                    )
                    result = await client.model_list(
                        agent=agent, limit=DEFAULT_MODEL_LIST_LIMIT
                    )
                    entries: list[dict[str, Any]] = []
                    if isinstance(result, list):
                        entries = [e for e in result if isinstance(e, dict)]
                    elif isinstance(result, dict):
                        for key in ("data", "models", "items", "results"):
                            value = result.get(key)
                            if isinstance(value, list):
                                entries = [
                                    e for e in value if isinstance(e, dict)
                                ]
                                break
                    for entry in entries[:25]:
                        model_id = entry.get("model") or entry.get("id")
                        if not isinstance(model_id, str) or not model_id:
                            continue
                        display = entry.get("displayName")
                        label = (
                            f"{display} ({model_id})"
                            if isinstance(display, str) and display and display != model_id
                            else model_id
                        )
                        # Discord caps choice name at 100 chars
                        if len(label) > 100:
                            label = label[:97] + "..."
                        choices.append(
                            app_commands.Choice(name=label, value=model_id)
                        )
        except Exception:
            pass

        # Fall back to well-known models if the live list is empty.
        if not choices:
            from ..constants import DEFAULT_AGENT_MODELS

            fallback = [
                ("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5"),
                ("claude-opus-4-6", "Claude Opus 4.6"),
                ("o4-mini", "o4-mini"),
                ("gpt-5.3-codex", "GPT-5.3 Codex"),
            ]
            for model_id, display in fallback:
                choices.append(
                    app_commands.Choice(
                        name=f"{display} ({model_id})", value=model_id
                    )
                )

        # Filter by what the user has typed so far.
        if current:
            lower = current.lower()
            choices = [c for c in choices if lower in c.name.lower() or lower in c.value.lower()]

        return choices[:25]

    async def _handle_slash_model(self, interaction: Any, name: Optional[str]) -> None:
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

        if name:
            from ..constants import DEFAULT_AGENT, DEFAULT_AGENT_MODELS

            def apply(record: Any) -> None:
                record.model = name
                record.updated_at = now_iso()

            await self._store.update_topic_field(topic_key, apply)
            await interaction.followup.send(
                f"Model set to `{name}`. Will apply on the next turn.", ephemeral=True
            )
        else:
            record = await self._store.get_topic(topic_key)
            current_model = record.model if record else None
            if not current_model:
                from ..constants import DEFAULT_AGENT, DEFAULT_AGENT_MODELS

                agent = (record.agent if record else None) or DEFAULT_AGENT
                current_model = DEFAULT_AGENT_MODELS.get(agent, "default")
            await interaction.followup.send(
                f"Current model: `{current_model}`. Use `/model <name>` to change.",
                ephemeral=True,
            )

    async def _handle_slash_agent(self, interaction: Any, name: Optional[str]) -> None:
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

        if name:
            from ..state import normalize_agent

            normalized = normalize_agent(name)
            if normalized is None:
                from ..constants import VALID_AGENT_VALUES

                await interaction.followup.send(
                    f"Unknown agent `{name}`. Valid: {', '.join(sorted(VALID_AGENT_VALUES))}",
                    ephemeral=True,
                )
                return

            from ..constants import DEFAULT_AGENT_MODELS

            def apply(record: Any) -> None:
                record.agent = normalized
                record.model = DEFAULT_AGENT_MODELS.get(normalized)
                record.codex_thread_id = None
                record.reasoning_effort = None
                record.updated_at = now_iso()

            await self._store.update_topic_field(topic_key, apply)
            await interaction.followup.send(
                f"Agent set to `{normalized}`. Thread reset.", ephemeral=True
            )
        else:
            record = await self._store.get_topic(topic_key)
            from ..constants import DEFAULT_AGENT

            current = (record.agent if record else None) or DEFAULT_AGENT
            await interaction.followup.send(
                f"Current agent: `{current}`. Use `/agent <name>` to change.",
                ephemeral=True,
            )

    async def _handle_slash_approvals(
        self, interaction: Any, mode: Optional[str]
    ) -> None:
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

        if mode:
            from ..constants import APPROVAL_POLICY_VALUES, APPROVAL_PRESETS

            mode_lower = mode.strip().lower()

            # Check if it's a preset
            if mode_lower in APPROVAL_PRESETS:
                ap, sp = APPROVAL_PRESETS[mode_lower]

                def apply_preset(record: Any) -> None:
                    record.approval_policy = ap
                    record.sandbox_policy = sp
                    record.updated_at = now_iso()

                await self._store.update_topic_field(topic_key, apply_preset)
                await interaction.followup.send(
                    f"Approval preset `{mode_lower}` applied "
                    f"(approval={ap}, sandbox={sp}).",
                    ephemeral=True,
                )
                return

            # Check if it's a mode (safe/yolo)
            from ..state import APPROVAL_MODES

            if mode_lower in APPROVAL_MODES:
                normalized = mode_lower

                def apply_mode(record: Any) -> None:
                    record.approval_mode = normalized
                    record.approval_policy = None
                    record.sandbox_policy = None
                    record.updated_at = now_iso()

                await self._store.update_topic_field(topic_key, apply_mode)
                await interaction.followup.send(
                    f"Approval mode set to `{normalized}`.", ephemeral=True
                )
                return

            # Check if it's a direct policy value
            if mode_lower in APPROVAL_POLICY_VALUES:

                def apply_policy(record: Any) -> None:
                    record.approval_policy = mode_lower
                    record.updated_at = now_iso()

                await self._store.update_topic_field(topic_key, apply_policy)
                await interaction.followup.send(
                    f"Approval policy set to `{mode_lower}`.", ephemeral=True
                )
                return

            await interaction.followup.send(
                f"Unknown mode `{mode}`. Use: safe, yolo, "
                f"{', '.join(sorted(APPROVAL_PRESETS))}",
                ephemeral=True,
            )
        else:
            record = await self._store.get_topic(topic_key)
            current_mode = record.approval_mode if record else "yolo"
            ap = record.approval_policy if record else None
            sp = record.sandbox_policy if record else None
            parts = [f"Mode: `{current_mode}`"]
            if ap:
                parts.append(f"approval_policy: `{ap}`")
            if sp:
                parts.append(f"sandbox_policy: `{sp}`")
            await interaction.followup.send(
                " | ".join(parts) + "\nUse `/approvals <mode>` to change.",
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
