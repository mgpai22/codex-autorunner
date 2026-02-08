from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .....core.logging_utils import log_event

logger = logging.getLogger(
    "codex_autorunner.integrations.discord.handlers.commands.scaffold"
)

_CONTROL_PLANE_WORKSPACE_ID = "__control_plane__"
_CONTROL_PLANE_CATEGORY_NAME = "Control Plane"
_SCAFFOLD_SLEEP_SECONDS = 0.5


class ScaffoldCommands:
    """Mixin providing the /setup command implementation."""

    async def _cmd_setup_impl(self, interaction: Any) -> None:
        import discord as _discord

        if self._hub_supervisor is None:
            await interaction.followup.send("No hub configured.")
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("`/setup` must be run inside a guild.")
            return

        guild_id = interaction.guild_id
        if not isinstance(guild_id, int):
            await interaction.followup.send("Missing guild id.")
            return

        log_event(
            self._logger,
            logging.INFO,
            "discord.setup.started",
            guild_id=guild_id,
        )

        try:
            workspaces = (
                self._hub_supervisor.list_workspaces()
                if hasattr(self._hub_supervisor, "list_workspaces")
                else self._hub_supervisor.list_repos()
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.setup.list_workspaces_failed",
                guild_id=guild_id,
                exc=exc,
            )
            await interaction.followup.send(f"Failed to list workspaces: {exc}")
            return

        scaffold = self._config.scaffold
        channel_kind = str(scaffold.tasks_channel_kind or "forum").strip().lower()

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

        for ws in workspaces:
            workspace_id = getattr(ws, "id", None)
            if not isinstance(workspace_id, str) or not workspace_id:
                continue
            workspace_name = getattr(ws, "display_name", None)
            if not isinstance(workspace_name, str) or not workspace_name:
                workspace_name = workspace_id
            workspace_path = getattr(ws, "path", None)
            workspace_path_str = str(workspace_path) if workspace_path is not None else ""

            category_name = f"{scaffold.category_prefix}{workspace_name}"

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
                    "discord.setup.workspace_category_failed",
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    exc=exc,
                )
                continue

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
                    "discord.setup.tasks_channel_failed",
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
                    "discord.setup.activity_channel_failed",
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
                    "discord.setup.approval_channel_failed",
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
                    "discord.setup.run_channel_failed",
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    exc=exc,
                )

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
                        "discord.setup.auto_bound",
                        guild_id=guild_id,
                        workspace_id=workspace_id,
                        channel_id=tasks_channel.id,
                        workspace_path=workspace_path_str,
                    )
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.setup.auto_bind_failed",
                        guild_id=guild_id,
                        workspace_id=workspace_id,
                        channel_id=getattr(tasks_channel, "id", None),
                        workspace_path=workspace_path_str,
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
                    await self._store.set_channel_binding(
                        run_key, workspace_path_str
                    )
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.setup.run_auto_bind_failed",
                        guild_id=guild_id,
                        workspace_id=workspace_id,
                        exc=exc,
                    )

        try:
            control_plane_category = await self._ensure_category(
                guild,
                guild_id=guild_id,
                workspace_id=_CONTROL_PLANE_WORKSPACE_ID,
                category_name=_CONTROL_PLANE_CATEGORY_NAME,
                channels_created=channels_created,
            )

            dashboard = await self._ensure_text_channel(
                control_plane_category,
                guild_id=guild_id,
                workspace_id=_CONTROL_PLANE_WORKSPACE_ID,
                channel_type="dashboard",
                channel_name=scaffold.dashboard_channel_name,
                overwrites=readonly_overwrites,
                channels_created=channels_created,
            )
            if dashboard is not None:
                await self._move_into_category(dashboard, control_plane_category)

            agent_bus = await self._ensure_text_channel(
                control_plane_category,
                guild_id=guild_id,
                workspace_id=_CONTROL_PLANE_WORKSPACE_ID,
                channel_type="agent_bus",
                channel_name=scaffold.agent_bus_channel_name,
                overwrites=readonly_overwrites,
                channels_created=channels_created,
            )
            if agent_bus is not None:
                await self._move_into_category(agent_bus, control_plane_category)

            notifications = await self._ensure_text_channel(
                control_plane_category,
                guild_id=guild_id,
                workspace_id=_CONTROL_PLANE_WORKSPACE_ID,
                channel_type="notifications",
                channel_name=scaffold.notifications_channel_name,
                overwrites=None,
                channels_created=channels_created,
            )
            if notifications is not None:
                await self._move_into_category(notifications, control_plane_category)

        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.setup.control_plane_failed",
                guild_id=guild_id,
                exc=exc,
            )

        from ...rendering import build_setup_summary_embed

        embed = build_setup_summary_embed(workspaces, channels_created, tags_created)
        await interaction.followup.send(embed=embed)

        log_event(
            self._logger,
            logging.INFO,
            "discord.setup.completed",
            guild_id=guild_id,
            workspace_count=len(workspaces),
            channels_created=len(channels_created),
            tags_created=len(tags_created),
        )

    def _resolve_bot_member(self, guild: Any) -> Optional[Any]:
        bot_user = self._bot.user
        member = getattr(guild, "me", None)
        if member is not None:
            return member
        if bot_user is None:
            return None
        if hasattr(guild, "get_member"):
            return guild.get_member(bot_user.id)
        return None

    async def _ensure_category(
        self,
        guild: Any,
        *,
        guild_id: int,
        workspace_id: str,
        category_name: str,
        channels_created: list[dict[str, Any]],
    ) -> Any:
        import discord as _discord

        existing_id = await self._store.get_scaffolded_channel(
            guild_id, workspace_id, "category"
        )
        if existing_id is not None:
            try:
                channel = await self._bot.fetch_channel(existing_id)
                if isinstance(channel, _discord.CategoryChannel):
                    return channel
            except Exception:
                pass

        category = await guild.create_category(category_name)
        await asyncio.sleep(_SCAFFOLD_SLEEP_SECONDS)
        await self._store.save_scaffolded_channel(
            guild_id, workspace_id, "category", category.id
        )
        channels_created.append(
            {
                "workspace_id": workspace_id,
                "channel_type": "category",
                "channel_id": category.id,
            }
        )

        log_event(
            self._logger,
            logging.INFO,
            "discord.setup.category.created",
            guild_id=guild_id,
            workspace_id=workspace_id,
            channel_id=category.id,
            name=category_name,
        )
        return category

    async def _ensure_tasks_channel(
        self,
        category: Any,
        *,
        guild_id: int,
        workspace_id: str,
        channel_kind: str,
        tags_created: list[dict[str, Any]],
        channels_created: list[dict[str, Any]],
    ) -> Optional[Any]:
        import discord as _discord

        channel_name = self._config.scaffold.tasks_channel_name
        existing_id = await self._store.get_scaffolded_channel(
            guild_id, workspace_id, "tasks"
        )
        if existing_id is not None:
            try:
                channel = await self._bot.fetch_channel(existing_id)
                if channel_kind == "forum" and isinstance(channel, _discord.ForumChannel):
                    await self._ensure_forum_tags(
                        channel,
                        guild_id=guild_id,
                        workspace_id=workspace_id,
                        tags_created=tags_created,
                    )
                    return channel
                if channel_kind == "text" and isinstance(channel, _discord.TextChannel):
                    return channel
            except Exception:
                pass

        if channel_kind == "text":
            tasks_channel = await category.create_text_channel(channel_name)
        else:
            forum_tags = [
                _discord.ForumTag(name=name)
                for name in self._config.scaffold.task_forum_tags
            ]
            tasks_channel = await category.create_forum(
                channel_name, available_tags=forum_tags
            )

        await asyncio.sleep(_SCAFFOLD_SLEEP_SECONDS)
        await self._store.save_scaffolded_channel(
            guild_id, workspace_id, "tasks", tasks_channel.id
        )
        channels_created.append(
            {
                "workspace_id": workspace_id,
                "channel_type": "tasks",
                "channel_id": tasks_channel.id,
            }
        )

        log_event(
            self._logger,
            logging.INFO,
            "discord.setup.channel.created",
            guild_id=guild_id,
            workspace_id=workspace_id,
            channel_type="tasks",
            channel_id=tasks_channel.id,
            name=channel_name,
            kind=channel_kind,
        )

        if channel_kind == "forum" and isinstance(tasks_channel, _discord.ForumChannel):
            created_names = set(self._config.scaffold.task_forum_tags)
            for tag in tasks_channel.available_tags:
                if tag.name in created_names and tag.id is not None:
                    tags_created.append(
                        {
                            "workspace_id": workspace_id,
                            "forum_channel_id": tasks_channel.id,
                            "tag_name": tag.name,
                            "tag_id": tag.id,
                        }
                    )
            await self._store_forum_tags(
                tasks_channel,
                guild_id=guild_id,
                workspace_id=workspace_id,
            )

        return tasks_channel

    async def _ensure_text_channel(
        self,
        category: Any,
        *,
        guild_id: int,
        workspace_id: str,
        channel_type: str,
        channel_name: str,
        overwrites: Optional[dict[Any, Any]],
        channels_created: list[dict[str, Any]],
    ) -> Optional[Any]:
        import discord as _discord

        existing_id = await self._store.get_scaffolded_channel(
            guild_id, workspace_id, channel_type
        )
        if existing_id is not None:
            try:
                channel = await self._bot.fetch_channel(existing_id)
                if isinstance(channel, _discord.TextChannel):
                    return channel
            except Exception:
                pass

        kwargs: dict[str, Any] = {}
        if overwrites is not None:
            kwargs["overwrites"] = overwrites
        channel = await category.create_text_channel(channel_name, **kwargs)
        await asyncio.sleep(_SCAFFOLD_SLEEP_SECONDS)
        await self._store.save_scaffolded_channel(
            guild_id, workspace_id, channel_type, channel.id
        )
        channels_created.append(
            {
                "workspace_id": workspace_id,
                "channel_type": channel_type,
                "channel_id": channel.id,
            }
        )

        log_event(
            self._logger,
            logging.INFO,
            "discord.setup.channel.created",
            guild_id=guild_id,
            workspace_id=workspace_id,
            channel_type=channel_type,
            channel_id=channel.id,
            name=channel_name,
        )
        return channel

    async def _ensure_forum_tags(
        self,
        channel: Any,
        *,
        guild_id: int,
        workspace_id: str,
        tags_created: list[dict[str, Any]],
    ) -> None:
        import discord as _discord

        required = list(self._config.scaffold.task_forum_tags)
        if not required:
            return

        existing = {tag.name: tag for tag in getattr(channel, "available_tags", [])}
        missing = [name for name in required if name not in existing]
        if missing:
            try:
                updated = list(getattr(channel, "available_tags", []))
                updated.extend(_discord.ForumTag(name=name) for name in missing)
                await channel.edit(available_tags=updated)
                channel = await self._bot.fetch_channel(channel.id)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.setup.forum_tags.edit_failed",
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    channel_id=getattr(channel, "id", None),
                    exc=exc,
                )

        await self._store_forum_tags(
            channel,
            guild_id=guild_id,
            workspace_id=workspace_id,
        )

        for tag in getattr(channel, "available_tags", []):
            if tag.name in missing and tag.id is not None:
                tags_created.append(
                    {
                        "workspace_id": workspace_id,
                        "forum_channel_id": channel.id,
                        "tag_name": tag.name,
                        "tag_id": tag.id,
                    }
                )

    async def _store_forum_tags(
        self,
        channel: Any,
        *,
        guild_id: int,
        workspace_id: str,
    ) -> None:
        for tag in getattr(channel, "available_tags", []):
            tag_name = getattr(tag, "name", None)
            tag_id = getattr(tag, "id", None)
            if not isinstance(tag_name, str) or not tag_name:
                continue
            if not isinstance(tag_id, int) or isinstance(tag_id, bool):
                continue
            await self._store.save_forum_tag(
                guild_id, workspace_id, channel.id, tag_name, tag_id
            )

    async def _move_into_category(self, channel: Any, category: Any) -> None:
        channel_category_id = getattr(channel, "category_id", None)
        if channel_category_id == getattr(category, "id", None):
            return
        if hasattr(channel, "edit"):
            try:
                await channel.edit(category=category)
            except Exception:
                pass
