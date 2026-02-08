from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

from .....core.logging_utils import log_event

logger = logging.getLogger(
    "codex_autorunner.integrations.discord.handlers.commands.workspace_create"
)


def normalize_git_url(raw: str) -> str:
    """Normalize a user-entered URL for ``git clone``.

    * SSH (``git@host:path``) — returned as-is.
    * Has scheme (``https://www.github.com/...``) — ``www.`` stripped after
      the scheme.
    * Bare domain (``github.com/user/repo``, ``www.github.com/user/repo``)
      — ``www.`` stripped, ``https://`` prepended.
    * ``.git`` suffix is preserved (only stripped elsewhere for name
      inference).
    """
    url = raw.strip()

    # SSH: git@host:path — leave as-is
    if re.match(r"^[\w.-]+@[\w.-]+:", url):
        return url

    # Has scheme (https://, http://, git://, etc.)
    if re.match(r"^[a-zA-Z][\w+.-]*://", url):
        # Strip www. right after the scheme
        url = re.sub(r"^([a-zA-Z][\w+.-]*://)www\.", r"\1", url)
        return url

    # Bare domain — strip www. and prepend https://
    if url.startswith("www."):
        url = url[4:]
    return f"https://{url}"


def _validate_git_url(url: str) -> Optional[str]:
    """Return an error message if *url* is obviously invalid, else ``None``."""
    if not url:
        return "Git URL cannot be empty."

    # SSH: must have a path after the colon
    if re.match(r"^[\w.-]+@[\w.-]+:", url):
        after_colon = url.split(":", 1)[1]
        if not after_colon or after_colon == "/":
            return "SSH URL is missing a repository path."
        return None

    # Scheme-based: must have a hostname and a path
    m = re.match(r"^[a-zA-Z][\w+.-]*://([^/]*)(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        if not host:
            return "URL is missing a hostname."
        if not path or path == "/":
            return "URL is missing a repository path."
        return None

    # Bare domain form (already normalized to https:// by normalize_git_url,
    # but handle defensively)
    return "URL does not look like a valid git URL."


class WorkspaceCreateCommands:
    """Mixin providing the /workspace create interactive flow."""

    # ------------------------------------------------------------------
    # Entry point (called from commands_runtime.py)
    # ------------------------------------------------------------------

    async def _cmd_workspace_create_impl(self, interaction: Any) -> None:
        import discord as _discord

        if self._hub_supervisor is None:
            await interaction.followup.send("No hub configured.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "`/workspace create` must be run inside a guild.", ephemeral=True
            )
            return

        select = _discord.ui.Select(
            custom_id="wscreate:type_select",
            placeholder="Choose workspace type...",
            options=[
                _discord.SelectOption(
                    label="New (empty repo)",
                    value="new",
                    description="Create a new empty workspace",
                ),
                _discord.SelectOption(
                    label="Clone (from git URL)",
                    value="clone",
                    description="Clone an existing git repository",
                ),
                _discord.SelectOption(
                    label="Worktree (branch from existing)",
                    value="worktree",
                    description="Create a git worktree from a base repo",
                ),
            ],
        )
        view = _discord.ui.View(timeout=300)
        view.add_item(select)
        await interaction.followup.send(
            "**Create Workspace** — select a type:", view=view, ephemeral=True
        )

    # ------------------------------------------------------------------
    # /workspace clone — direct slash command (no modal)
    # ------------------------------------------------------------------

    async def _cmd_workspace_clone_impl(
        self, interaction: Any, url: str, name: Optional[str] = None
    ) -> None:
        if self._hub_supervisor is None:
            await interaction.followup.send("No hub configured.", ephemeral=True)
            return

        if interaction.guild is None:
            await interaction.followup.send(
                "`/workspace clone` must be run inside a guild.", ephemeral=True
            )
            return

        normalized = normalize_git_url(url)
        error = _validate_git_url(normalized)
        if error:
            await interaction.followup.send(f"Invalid URL: {error}", ephemeral=True)
            return

        from .....core.state import now_iso
        from ...types import WorkspaceCreateSession

        session_id = uuid.uuid4().hex[:12]
        session = WorkspaceCreateSession(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            ws_type="clone",
            git_url=normalized,
            repo_id=name or None,
            created_at=now_iso(),
        )
        self._ws_create_sessions[session_id] = session
        await self._ws_create_execute(interaction, session_id)

    # ------------------------------------------------------------------
    # Component interaction dispatcher
    # ------------------------------------------------------------------

    async def _handle_workspace_create_interaction(
        self, interaction: Any, custom_id: str
    ) -> None:
        parts = custom_id.split(":")
        if len(parts) < 2:
            return

        action = parts[1]

        if action == "type_select":
            await self._ws_create_handle_type_select(interaction)
            return

        if action == "git_init" and len(parts) >= 4:
            session_id = parts[2]
            choice = parts[3]
            await self._ws_create_handle_git_init(interaction, session_id, choice)
            return

        if action == "base_select" and len(parts) >= 3:
            session_id = parts[2]
            await self._ws_create_handle_worktree_base_select(interaction, session_id)
            return

    # ------------------------------------------------------------------
    # Modal submit dispatcher
    # ------------------------------------------------------------------

    async def _handle_workspace_create_modal(
        self, interaction: Any, custom_id: str
    ) -> None:
        parts = custom_id.split(":")
        if len(parts) < 3:
            return

        modal_type = parts[1]
        session_id = parts[2]

        if modal_type == "modal_new":
            await self._ws_create_handle_new_modal(interaction, session_id)
        elif modal_type == "modal_clone":
            await self._ws_create_handle_clone_modal(interaction, session_id)
        elif modal_type == "modal_worktree":
            await self._ws_create_handle_worktree_modal(interaction, session_id)

    # ------------------------------------------------------------------
    # Type selection -> route to modal
    # ------------------------------------------------------------------

    async def _ws_create_handle_type_select(self, interaction: Any) -> None:
        import discord as _discord

        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            await interaction.response.defer(ephemeral=True)
            return

        ws_type = values[0]
        session_id = uuid.uuid4().hex[:12]

        from .....core.state import now_iso
        from ...types import WorkspaceCreateSession

        session = WorkspaceCreateSession(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            ws_type=ws_type,
            created_at=now_iso(),
        )
        self._ws_create_sessions[session_id] = session

        if ws_type == "new":
            modal = _discord.ui.Modal(
                title="New Workspace",
                custom_id=f"wscreate:modal_new:{session_id}",
            )
            modal.add_item(
                _discord.ui.TextInput(
                    label="Workspace ID",
                    placeholder="my-project",
                    custom_id="repo_id",
                    required=True,
                    max_length=100,
                )
            )
            modal.add_item(
                _discord.ui.TextInput(
                    label="Custom Path (optional)",
                    placeholder="Leave blank for default",
                    custom_id="repo_path",
                    required=False,
                    max_length=200,
                )
            )
            await interaction.response.send_modal(modal)

        elif ws_type == "clone":
            modal = _discord.ui.Modal(
                title="Clone Repository",
                custom_id=f"wscreate:modal_clone:{session_id}",
            )
            modal.add_item(
                _discord.ui.TextInput(
                    label="Git URL",
                    placeholder="https://github.com/user/repo.git",
                    custom_id="git_url",
                    required=True,
                    max_length=500,
                )
            )
            modal.add_item(
                _discord.ui.TextInput(
                    label="Workspace ID (optional)",
                    placeholder="Leave blank to infer from URL",
                    custom_id="repo_id",
                    required=False,
                    max_length=100,
                )
            )
            await interaction.response.send_modal(modal)

        elif ws_type == "worktree":
            await self._ws_create_show_base_picker(interaction, session_id)

    # ------------------------------------------------------------------
    # New repo modal -> show git init buttons
    # ------------------------------------------------------------------

    async def _ws_create_handle_new_modal(
        self, interaction: Any, session_id: str
    ) -> None:
        import discord as _discord

        session = self._ws_create_sessions.get(session_id)
        if session is None:
            await interaction.response.send_message(
                "Session expired.", ephemeral=True
            )
            return

        components = interaction.data.get("components") or []
        for row in components:
            for comp in row.get("components") or []:
                cid = comp.get("custom_id", "")
                val = (comp.get("value") or "").strip()
                if cid == "repo_id":
                    session.repo_id = val
                elif cid == "repo_path":
                    session.repo_path = val or None

        if not session.repo_id:
            await interaction.response.send_message(
                "Workspace ID is required.", ephemeral=True
            )
            self._ws_create_sessions.pop(session_id, None)
            return

        view = _discord.ui.View(timeout=120)
        view.add_item(
            _discord.ui.Button(
                label="With Git Init",
                style=_discord.ButtonStyle.primary,
                custom_id=f"wscreate:git_init:{session_id}:yes",
            )
        )
        view.add_item(
            _discord.ui.Button(
                label="Without Git Init",
                style=_discord.ButtonStyle.secondary,
                custom_id=f"wscreate:git_init:{session_id}:no",
            )
        )
        await interaction.response.send_message(
            f"Creating workspace **{session.repo_id}** — initialize git?",
            view=view,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Git init button -> execute new repo creation
    # ------------------------------------------------------------------

    async def _ws_create_handle_git_init(
        self, interaction: Any, session_id: str, choice: str
    ) -> None:
        session = self._ws_create_sessions.get(session_id)
        if session is None:
            await interaction.response.send_message(
                "Session expired.", ephemeral=True
            )
            return

        session.git_init = choice == "yes"
        await interaction.response.defer(ephemeral=True)
        await self._ws_create_execute(interaction, session_id)

    # ------------------------------------------------------------------
    # Clone modal -> execute clone
    # ------------------------------------------------------------------

    async def _ws_create_handle_clone_modal(
        self, interaction: Any, session_id: str
    ) -> None:
        session = self._ws_create_sessions.get(session_id)
        if session is None:
            await interaction.response.send_message(
                "Session expired.", ephemeral=True
            )
            return

        components = interaction.data.get("components") or []
        for row in components:
            for comp in row.get("components") or []:
                cid = comp.get("custom_id", "")
                val = (comp.get("value") or "").strip()
                if cid == "git_url":
                    session.git_url = val
                elif cid == "repo_id":
                    session.repo_id = val or None

        if not session.git_url:
            await interaction.response.send_message(
                "Git URL is required.", ephemeral=True
            )
            self._ws_create_sessions.pop(session_id, None)
            return

        await interaction.response.defer(ephemeral=True)
        await self._ws_create_execute(interaction, session_id)

    # ------------------------------------------------------------------
    # Worktree: show base repo picker
    # ------------------------------------------------------------------

    async def _ws_create_show_base_picker(
        self, interaction: Any, session_id: str
    ) -> None:
        import discord as _discord

        try:
            workspaces = (
                self._hub_supervisor.list_workspaces()
                if hasattr(self._hub_supervisor, "list_workspaces")
                else self._hub_supervisor.list_repos()
            )
        except Exception as exc:
            await interaction.response.send_message(
                f"Failed to list workspaces: {exc}", ephemeral=True
            )
            self._ws_create_sessions.pop(session_id, None)
            return

        base_repos = [
            ws
            for ws in workspaces
            if getattr(ws, "kind", "") == "base"
        ]
        if not base_repos:
            await interaction.response.send_message(
                "No base repositories available for worktree creation.",
                ephemeral=True,
            )
            self._ws_create_sessions.pop(session_id, None)
            return

        options = []
        for ws in base_repos[:25]:
            ws_id = getattr(ws, "id", "")
            ws_name = getattr(ws, "display_name", None) or ws_id
            options.append(
                _discord.SelectOption(
                    label=ws_name[:100],
                    value=ws_id,
                    description=str(getattr(ws, "path", ""))[:100],
                )
            )

        select = _discord.ui.Select(
            custom_id=f"wscreate:base_select:{session_id}",
            placeholder="Select base repository...",
            options=options,
        )
        view = _discord.ui.View(timeout=300)
        view.add_item(select)
        await interaction.response.send_message(
            "Select the base repository for the worktree:",
            view=view,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Worktree: base repo selected -> show branch modal
    # ------------------------------------------------------------------

    async def _ws_create_handle_worktree_base_select(
        self, interaction: Any, session_id: str
    ) -> None:
        import discord as _discord

        session = self._ws_create_sessions.get(session_id)
        if session is None:
            await interaction.response.send_message(
                "Session expired.", ephemeral=True
            )
            return

        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            await interaction.response.defer(ephemeral=True)
            return

        session.base_repo_id = values[0]

        modal = _discord.ui.Modal(
            title="Create Worktree",
            custom_id=f"wscreate:modal_worktree:{session_id}",
        )
        modal.add_item(
            _discord.ui.TextInput(
                label="Branch Name",
                placeholder="feature/my-branch",
                custom_id="branch",
                required=True,
                max_length=200,
            )
        )
        modal.add_item(
            _discord.ui.TextInput(
                label="Start Point (optional)",
                placeholder="e.g. main, HEAD, a commit SHA",
                custom_id="start_point",
                required=False,
                max_length=200,
            )
        )
        await interaction.response.send_modal(modal)

    # ------------------------------------------------------------------
    # Worktree: branch modal submitted -> execute
    # ------------------------------------------------------------------

    async def _ws_create_handle_worktree_modal(
        self, interaction: Any, session_id: str
    ) -> None:
        session = self._ws_create_sessions.get(session_id)
        if session is None:
            await interaction.response.send_message(
                "Session expired.", ephemeral=True
            )
            return

        components = interaction.data.get("components") or []
        for row in components:
            for comp in row.get("components") or []:
                cid = comp.get("custom_id", "")
                val = (comp.get("value") or "").strip()
                if cid == "branch":
                    session.branch = val
                elif cid == "start_point":
                    session.start_point = val or None

        if not session.branch:
            await interaction.response.send_message(
                "Branch name is required.", ephemeral=True
            )
            self._ws_create_sessions.pop(session_id, None)
            return

        await interaction.response.defer(ephemeral=True)
        await self._ws_create_execute(interaction, session_id)

    # ------------------------------------------------------------------
    # Execute workspace creation + scaffold channels
    # ------------------------------------------------------------------

    async def _ws_create_execute(self, interaction: Any, session_id: str) -> None:
        import discord as _discord

        session = self._ws_create_sessions.pop(session_id, None)
        if session is None:
            await interaction.followup.send("Session expired.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Guild context lost.", ephemeral=True
            )
            return
        guild_id = interaction.guild_id

        # --- Create the workspace via HubSupervisor ---
        try:
            if session.ws_type == "new":
                from pathlib import Path

                repo_path: Optional[Any] = None
                if session.repo_path:
                    repo_path = Path(session.repo_path)
                snapshot = self._hub_supervisor.create_repo(
                    session.repo_id or "",
                    repo_path=repo_path,
                    git_init=session.git_init,
                )
            elif session.ws_type == "clone":
                snapshot = self._hub_supervisor.clone_repo(
                    git_url=session.git_url or "",
                    repo_id=session.repo_id,
                )
            elif session.ws_type == "worktree":
                snapshot = self._hub_supervisor.create_worktree(
                    base_repo_id=session.base_repo_id or "",
                    branch=session.branch or "",
                    start_point=session.start_point,
                )
            else:
                await interaction.followup.send(
                    f"Unknown workspace type: {session.ws_type}", ephemeral=True
                )
                return
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.workspace_create.failed",
                ws_type=session.ws_type,
                exc=exc,
            )
            await interaction.followup.send(
                f"Failed to create workspace: {exc}", ephemeral=True
            )
            return

        workspace_id = snapshot.id
        workspace_name = snapshot.display_name or workspace_id
        workspace_path_str = str(snapshot.path) if snapshot.path else ""

        log_event(
            self._logger,
            logging.INFO,
            "discord.workspace_create.created",
            workspace_id=workspace_id,
            ws_type=session.ws_type,
            guild_id=guild_id,
        )

        # --- Scaffold Discord channels for the new workspace ---
        scaffold = self._config.scaffold
        channel_kind = str(scaffold.tasks_channel_kind or "forum").strip().lower()
        category_name = f"{scaffold.category_prefix}{workspace_name}"

        bot_member = self._resolve_bot_member(guild)
        readonly_overwrites: Optional[dict[Any, _discord.PermissionOverwrite]] = None
        if bot_member is not None:
            readonly_overwrites = {
                guild.default_role: _discord.PermissionOverwrite(send_messages=False),
                bot_member: _discord.PermissionOverwrite(send_messages=True),
            }
        else:
            readonly_overwrites = {
                guild.default_role: _discord.PermissionOverwrite(send_messages=False),
            }

        channels_created: list[dict[str, Any]] = []
        tags_created: list[dict[str, Any]] = []

        try:
            category = await self._ensure_category(
                guild,
                guild_id=guild_id,
                workspace_id=workspace_id,
                category_name=category_name,
                channels_created=channels_created,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.workspace_create.category_failed",
                guild_id=guild_id,
                workspace_id=workspace_id,
                exc=exc,
            )
            await interaction.followup.send(
                f"Workspace **{workspace_id}** created but channel scaffolding failed: {exc}",
                ephemeral=True,
            )
            return

        tasks_channel: Optional[Any] = None
        try:
            tasks_channel = await self._ensure_tasks_channel(
                category,
                guild_id=guild_id,
                workspace_id=workspace_id,
                channel_kind=channel_kind,
                tags_created=tags_created,
                channels_created=channels_created,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.workspace_create.tasks_channel_failed",
                guild_id=guild_id,
                workspace_id=workspace_id,
                exc=exc,
            )

        if tasks_channel is not None:
            await self._move_into_category(tasks_channel, category)

        try:
            activity = await self._ensure_text_channel(
                category,
                guild_id=guild_id,
                workspace_id=workspace_id,
                channel_type="activity",
                channel_name=scaffold.activity_channel_name,
                overwrites=readonly_overwrites,
                channels_created=channels_created,
            )
            if activity is not None:
                await self._move_into_category(activity, category)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.workspace_create.activity_channel_failed",
                guild_id=guild_id,
                workspace_id=workspace_id,
                exc=exc,
            )

        try:
            approvals = await self._ensure_text_channel(
                category,
                guild_id=guild_id,
                workspace_id=workspace_id,
                channel_type="approvals",
                channel_name=scaffold.approval_channel_name,
                overwrites=None,
                channels_created=channels_created,
            )
            if approvals is not None:
                await self._move_into_category(approvals, category)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.workspace_create.approval_channel_failed",
                guild_id=guild_id,
                workspace_id=workspace_id,
                exc=exc,
            )

        # --- Run channel (dedicated text channel for /run) ---
        run_channel: Optional[Any] = None
        try:
            run_channel = await self._ensure_text_channel(
                category,
                guild_id=guild_id,
                workspace_id=workspace_id,
                channel_type="run",
                channel_name=scaffold.run_channel_name,
                overwrites=None,
                channels_created=channels_created,
            )
            if run_channel is not None:
                await self._move_into_category(run_channel, category)
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.workspace_create.run_channel_failed",
                guild_id=guild_id,
                workspace_id=workspace_id,
                exc=exc,
            )

        # Auto-bind tasks channel to workspace path
        if (
            bool(scaffold.auto_bind)
            and tasks_channel is not None
            and workspace_path_str
        ):
            try:
                channel_key = f"{guild_id}:{tasks_channel.id}"
                await self._store.set_channel_binding(
                    channel_key, workspace_path_str
                )
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.workspace_create.auto_bound",
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    channel_id=tasks_channel.id,
                    workspace_path=workspace_path_str,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.workspace_create.auto_bind_failed",
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    exc=exc,
                )

        # Auto-bind run channel to workspace path
        if (
            bool(scaffold.auto_bind)
            and run_channel is not None
            and workspace_path_str
        ):
            try:
                run_key = f"{guild_id}:{run_channel.id}"
                await self._store.set_channel_binding(run_key, workspace_path_str)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.workspace_create.run_auto_bind_failed",
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    exc=exc,
                )

        log_event(
            self._logger,
            logging.INFO,
            "discord.workspace_create.scaffolded",
            guild_id=guild_id,
            workspace_id=workspace_id,
            channels_created=len(channels_created),
            tags_created=len(tags_created),
        )

        # --- Build confirmation embed ---
        embed = _discord.Embed(
            title="Workspace Created",
            color=_discord.Color.green(),
        )
        embed.add_field(name="ID", value=f"`{workspace_id}`", inline=True)
        embed.add_field(name="Type", value=session.ws_type, inline=True)
        if workspace_path_str:
            embed.add_field(
                name="Path", value=f"`{workspace_path_str}`", inline=False
            )
        if session.ws_type == "clone" and session.git_url:
            embed.add_field(
                name="Git URL", value=session.git_url, inline=False
            )
        if session.ws_type == "worktree":
            if session.base_repo_id:
                embed.add_field(
                    name="Base Repo",
                    value=f"`{session.base_repo_id}`",
                    inline=True,
                )
            if session.branch:
                embed.add_field(
                    name="Branch",
                    value=f"`{session.branch}`",
                    inline=True,
                )
        embed.add_field(
            name="Channels",
            value=f"{len(channels_created)} created",
            inline=True,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
