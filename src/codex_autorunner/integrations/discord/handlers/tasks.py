from __future__ import annotations

import logging
from typing import Any, Optional

from ....core.logging_utils import log_event
from ....core.state import now_iso
from ..helpers import build_topic_key, sanitize_thread_name
from ..rendering import (
    build_task_card_embed,
    build_tasks_list_embed,
    build_workspaces_embed,
)

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.tasks")

TASK_STATE_TAGS = {
    "queued",
    "running",
    "needs-approval",
    "blocked",
    "done",
    "failed",
    "timeout",
    "stopped",
}


if HAS_DISCORD:

    class TaskCardView(discord.ui.View):
        """Persistent controls for a Task Card."""

        def __init__(self, *, guild_id: int, thread_id: int) -> None:
            super().__init__(timeout=None)

            stop_btn = discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label="Stop",
                custom_id=f"task:stop:{guild_id}:{thread_id}",
            )
            approvals_btn = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Set approvals",
                custom_id=f"task:approvals_menu:{guild_id}:{thread_id}",
            )
            rerun_btn = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Rerun",
                custom_id=f"task:rerun:{guild_id}:{thread_id}",
            )
            escalate_btn = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Escalate (p0)",
                custom_id=f"task:escalate:{guild_id}:{thread_id}",
            )

            self.add_item(stop_btn)
            self.add_item(approvals_btn)
            self.add_item(rerun_btn)
            self.add_item(escalate_btn)

    class TaskApprovalsView(discord.ui.View):
        """Ephemeral approval-mode picker for a Task Card."""

        def __init__(self, *, guild_id: int, thread_id: int) -> None:
            super().__init__(timeout=None)
            approval_select = discord.ui.Select(
                custom_id=f"task:approvals:{guild_id}:{thread_id}",
                placeholder="Choose safe or yolo...",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(label="safe", value="safe"),
                    discord.SelectOption(label="yolo", value="yolo"),
                ],
            )
            self.add_item(approval_select)


class DiscordTasksMixin:
    """Mixin handling Task Card rendering, persistent controls, and triage commands."""

    async def _create_forum_task(
        self,
        guild: Any,
        workspace_id: str,
        forum_channel_id: int,
        prompt: str,
        user: Any,
    ) -> tuple[Any, Any]:
        """Create a forum task thread and store its root Task Card message."""
        if not HAS_DISCORD:
            raise RuntimeError("discord.py is required for forum-backed tasks")

        channel = None
        try:
            channel = guild.get_channel(forum_channel_id) if guild else None
        except Exception:
            channel = None
        if channel is None:
            try:
                channel = self._bot.get_channel(forum_channel_id)
            except Exception:
                channel = None
        if channel is None:
            channel = await self._bot.fetch_channel(forum_channel_id)

        if not isinstance(channel, discord.ForumChannel):
            raise ValueError(f"channel {forum_channel_id} is not a ForumChannel")

        thread_name = sanitize_thread_name(prompt)
        created_at = now_iso()

        workspace_payload: dict[str, Any] = {
            "id": workspace_id,
            "approval_mode": getattr(self._config.defaults, "approval_mode", None),
        }
        try:
            ap, sp = self._config.defaults.policies_for_mode(
                self._config.defaults.approval_mode
            )
            workspace_payload["approval_policy"] = ap
            workspace_payload["sandbox_policy"] = sp
        except Exception:
            pass

        embed = build_task_card_embed(
            workspace_payload,
            prompt,
            user,
            "queued",
            created_at,
            created_at,
        )

        queued_tag = await self._resolve_forum_tag(
            guild_id=guild.id,
            workspace_id=workspace_id,
            forum_channel=channel,
            tag_name="queued",
        )

        applied_tags = [queued_tag] if queued_tag else discord.utils.MISSING

        thread_with_message = await channel.create_thread(
            name=thread_name,
            embed=embed,
            applied_tags=applied_tags,
        )

        thread = thread_with_message.thread
        root_message = thread_with_message.message

        try:
            created_by_id = user.id if user is not None else None
        except Exception:
            created_by_id = None

        try:
            await self._store.save_task(
                guild.id,
                workspace_id,
                forum_channel_id,
                thread.id,
                root_message.id,
                created_by_id,
                prompt,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.task.save_failed",
                guild_id=guild.id,
                thread_id=thread.id,
                exc=exc,
            )

        view = TaskCardView(guild_id=guild.id, thread_id=thread.id)
        await self._edit_message(thread.id, root_message.id, embed=embed, view=view)

        log_event(
            self._logger,
            logging.INFO,
            "discord.task.created",
            guild_id=guild.id,
            workspace_id=workspace_id,
            forum_channel_id=forum_channel_id,
            thread_id=thread.id,
            root_message_id=root_message.id,
        )

        return thread, root_message

    async def _resolve_forum_tag(
        self,
        *,
        guild_id: int,
        workspace_id: str,
        forum_channel: Any,
        tag_name: str,
    ) -> Optional[Any]:
        if not HAS_DISCORD:
            return None
        if not isinstance(tag_name, str) or not tag_name:
            return None

        normalized = tag_name.strip().lower()
        if not normalized:
            return None

        tag_id = None
        try:
            tags = await self._store.get_forum_tags(guild_id, workspace_id)
            tag_id = tags.get(normalized)
            if tag_id is None:
                for key, value in tags.items():
                    if (
                        isinstance(key, str)
                        and key.strip().lower() == normalized
                        and isinstance(value, int)
                    ):
                        tag_id = value
                        break
        except Exception:
            tag_id = None

        if isinstance(tag_id, int):
            try:
                tag = forum_channel.get_tag(tag_id)
                if tag is not None:
                    return tag
            except Exception:
                pass

        try:
            for candidate in getattr(forum_channel, "available_tags", []) or []:
                if (
                    hasattr(candidate, "name")
                    and isinstance(candidate.name, str)
                    and candidate.name.strip().lower() == normalized
                ):
                    return candidate
        except Exception:
            return None

        return None

    async def _update_task_state(
        self,
        guild_id: int,
        thread_id: int,
        new_state: str,
        *,
        agent_response_url: Optional[str] = None,
    ) -> None:
        """Update stored task state, apply forum tag, and refresh the Task Card embed."""
        if not HAS_DISCORD:
            return

        task = await self._store.get_task(guild_id, thread_id)
        if task is None:
            return

        forum_channel_id = task.get("forum_channel_id")
        workspace_id = task.get("workspace_id")

        await self._store.update_task_state(guild_id, thread_id, new_state)

        if (
            isinstance(forum_channel_id, int)
            and isinstance(workspace_id, str)
            and workspace_id
        ):
            await self._apply_task_state_tag(
                guild_id=guild_id,
                workspace_id=workspace_id,
                forum_channel_id=forum_channel_id,
                thread_id=thread_id,
                new_state=new_state,
            )

        try:
            await self._post_task_activity(guild_id, thread_id, new_state)  # type: ignore[attr-defined]
        except Exception:
            pass

        await self._update_task_card(
            guild_id,
            thread_id,
            agent_response_url=agent_response_url,
        )

    async def _apply_task_state_tag(
        self,
        *,
        guild_id: int,
        workspace_id: str,
        forum_channel_id: int,
        thread_id: int,
        new_state: str,
    ) -> None:
        if not HAS_DISCORD:
            return

        thread = self._bot.get_channel(thread_id)
        if thread is None:
            thread = await self._bot.fetch_channel(thread_id)
        if not isinstance(thread, discord.Thread):
            return

        forum_channel = self._bot.get_channel(forum_channel_id)
        if forum_channel is None:
            forum_channel = await self._bot.fetch_channel(forum_channel_id)
        if not isinstance(forum_channel, discord.ForumChannel):
            return

        keep_tags: list[Any] = []
        for tag in getattr(thread, "applied_tags", []) or []:
            name = getattr(tag, "name", None)
            if isinstance(name, str) and name.strip().lower() in TASK_STATE_TAGS:
                continue
            keep_tags.append(tag)

        state_tag = await self._resolve_forum_tag(
            guild_id=guild_id,
            workspace_id=workspace_id,
            forum_channel=forum_channel,
            tag_name=new_state,
        )
        if state_tag is not None:
            keep_tags.append(state_tag)

        if len(keep_tags) > 5:
            keep_tags = keep_tags[-5:]

        try:
            await thread.edit(applied_tags=keep_tags)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.task.tag_update_failed",
                guild_id=guild_id,
                thread_id=thread_id,
                state=new_state,
                exc=exc,
            )

    async def _update_task_card(self, guild_id: int, thread_id: int, **kwargs: Any) -> None:
        """Fetch and edit the root Task Card embed."""
        if not HAS_DISCORD:
            return

        task = await self._store.get_task(guild_id, thread_id)
        if task is None:
            return

        forum_channel_id = task.get("forum_channel_id")
        workspace_id = task.get("workspace_id")
        root_message_id = task.get("root_message_id")
        prompt = task.get("initial_prompt") or ""
        created_at = task.get("created_at") or ""
        status = kwargs.get("status") or task.get("last_state") or "queued"
        updated_at = kwargs.get("updated_at") or task.get("last_state_at") or now_iso()
        created_by_user_id = task.get("created_by_user_id")

        if (
            not isinstance(forum_channel_id, int)
            or not isinstance(workspace_id, str)
            or not workspace_id
            or not isinstance(root_message_id, int)
        ):
            return

        workspace_payload: dict[str, Any] = {"id": workspace_id}

        try:
            topic = await self._store.get_topic(
                build_topic_key(guild_id, forum_channel_id, thread_id)
            )
        except Exception:
            topic = None

        if topic is not None:
            workspace_payload["approval_mode"] = getattr(topic, "approval_mode", None)
            workspace_payload["approval_policy"] = getattr(topic, "approval_policy", None)
            workspace_payload["sandbox_policy"] = getattr(topic, "sandbox_policy", None)

        activity_url = kwargs.get("activity_url")
        if activity_url is None:
            activity_msg_id = task.get("last_activity_message_id")
            if isinstance(activity_msg_id, int):
                activity_channel_id = await self._store.get_scaffolded_channel(
                    guild_id, workspace_id, "activity"
                )
                if isinstance(activity_channel_id, int):
                    activity_url = (
                        f"https://discord.com/channels/{guild_id}/"
                        f"{activity_channel_id}/{activity_msg_id}"
                    )

        embed = build_task_card_embed(
            workspace_payload,
            prompt,
            created_by_user_id if isinstance(created_by_user_id, int) else "",
            str(status),
            str(created_at),
            str(updated_at),
            agent_response_url=kwargs.get("agent_response_url"),
            activity_url=activity_url,
        )

        view = TaskCardView(guild_id=guild_id, thread_id=thread_id)
        await self._edit_message(thread_id, root_message_id, embed=embed, view=view)

    async def _handle_task_button(
        self,
        interaction: Any,
        action: str,
        guild_id: int,
        thread_id: int,
    ) -> None:
        """Router for Task Card button callbacks."""
        if not HAS_DISCORD:
            return

        if interaction.guild_id != guild_id:
            try:
                await interaction.response.send_message(
                    "Guild mismatch.", ephemeral=True
                )
            except Exception:
                pass
            return

        task = await self._store.get_task(guild_id, thread_id)
        if task is None:
            try:
                await interaction.response.send_message(
                    "Task not found.", ephemeral=True
                )
            except Exception:
                pass
            return

        forum_channel_id = task.get("forum_channel_id")
        workspace_id = task.get("workspace_id")

        if not isinstance(forum_channel_id, int) or not isinstance(workspace_id, str):
            try:
                await interaction.response.send_message(
                    "Task state is invalid.", ephemeral=True
                )
            except Exception:
                pass
            return

        if action == "stop":
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass

            topic_key = build_topic_key(guild_id, forum_channel_id, thread_id)
            ok = await self._interrupt_turn(topic_key)
            if ok:
                await self._update_task_state(guild_id, thread_id, "stopped")
                try:
                    await interaction.followup.send("Interrupt requested.", ephemeral=True)
                except Exception:
                    pass
            else:
                try:
                    await interaction.followup.send(
                        "No active task to stop.", ephemeral=True
                    )
                except Exception:
                    pass
            return

        if action == "approvals_menu":
            view = TaskApprovalsView(guild_id=guild_id, thread_id=thread_id)
            try:
                await interaction.response.send_message(
                    "Select approval mode:", view=view, ephemeral=True
                )
            except Exception:
                pass
            return

        if action == "approvals":
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass

            values = []
            if interaction.data:
                values = interaction.data.get("values", [])
            mode = values[0] if values else None
            if not isinstance(mode, str) or mode.strip().lower() not in ("safe", "yolo"):
                try:
                    await interaction.followup.send(
                        "Select `safe` or `yolo`.", ephemeral=True
                    )
                except Exception:
                    pass
                return

            normalized = mode.strip().lower()
            topic_key = build_topic_key(guild_id, forum_channel_id, thread_id)

            def apply(record: Any) -> None:
                record.approval_mode = normalized
                record.approval_policy = None
                record.sandbox_policy = None
                record.updated_at = now_iso()

            await self._store.update_topic_field(topic_key, apply)
            await self._update_task_card(guild_id, thread_id)
            try:
                await interaction.followup.send(
                    f"Approval mode set to `{normalized}`.", ephemeral=True
                )
            except Exception:
                pass
            return

        if action == "rerun":
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass

            prompt = task.get("initial_prompt") or ""
            if not isinstance(prompt, str) or not prompt.strip():
                try:
                    await interaction.followup.send(
                        "No initial prompt stored for this task.", ephemeral=True
                    )
                except Exception:
                    pass
                return

            channel_key = f"{guild_id}:{forum_channel_id}"
            binding = await self._store.get_channel_binding(channel_key)
            if not isinstance(binding, str) or not binding:
                try:
                    await interaction.followup.send(
                        "This tasks forum is not bound to a workspace.", ephemeral=True
                    )
                except Exception:
                    pass
                return

            record = await self._store.get_topic(
                build_topic_key(guild_id, forum_channel_id, thread_id)
            )
            if record is None:
                from ..state import DiscordTopicRecord

                record = DiscordTopicRecord(
                    topic_key=build_topic_key(guild_id, forum_channel_id, thread_id),
                    guild_id=guild_id,
                    channel_id=forum_channel_id,
                    thread_id=thread_id,
                    workspace_path=binding,
                    approval_mode=self._config.defaults.approval_mode,
                    created_at=now_iso(),
                    updated_at=now_iso(),
                )
                await self._store.save_topic(record.topic_key or "", record)
            elif not record.workspace_path:
                record.workspace_path = binding
                record.updated_at = now_iso()
                await self._store.save_topic(record.topic_key or "", record)

            await self._update_task_state(guild_id, thread_id, "queued")
            self._spawn_task(
                self._execute_turn(
                    build_topic_key(guild_id, forum_channel_id, thread_id),
                    prompt.strip(),
                    channel_id=forum_channel_id,
                    thread_id=thread_id,
                    reply_to=None,
                    record=record,
                )
            )

            try:
                await interaction.followup.send("Rerun queued.", ephemeral=True)
            except Exception:
                pass
            return

        if action == "escalate":
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass

            try:
                await self._apply_p0_tag(
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    forum_channel_id=forum_channel_id,
                    thread_id=thread_id,
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.task.escalate_failed",
                    guild_id=guild_id,
                    thread_id=thread_id,
                    exc=exc,
                )

            try:
                await interaction.followup.send("Escalated to p0.", ephemeral=True)
            except Exception:
                pass
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

    async def _apply_p0_tag(
        self,
        *,
        guild_id: int,
        workspace_id: str,
        forum_channel_id: int,
        thread_id: int,
    ) -> None:
        if not HAS_DISCORD:
            return

        thread = self._bot.get_channel(thread_id)
        if thread is None:
            thread = await self._bot.fetch_channel(thread_id)
        if not isinstance(thread, discord.Thread):
            return

        forum_channel = self._bot.get_channel(forum_channel_id)
        if forum_channel is None:
            forum_channel = await self._bot.fetch_channel(forum_channel_id)
        if not isinstance(forum_channel, discord.ForumChannel):
            return

        p0_tag = await self._resolve_forum_tag(
            guild_id=guild_id,
            workspace_id=workspace_id,
            forum_channel=forum_channel,
            tag_name="p0",
        )
        if p0_tag is None:
            return

        tags: list[Any] = list(getattr(thread, "applied_tags", []) or [])
        if any(getattr(tag, "id", None) == getattr(p0_tag, "id", None) for tag in tags):
            return
        tags.append(p0_tag)
        if len(tags) > 5:
            tags = tags[-5:]
        await thread.edit(applied_tags=tags)

        alerts = getattr(self._config, "alerts", None)
        if alerts is None or not getattr(alerts, "enabled", False):
            return
        alert_role_id = getattr(alerts, "alert_role_id", None)
        if not isinstance(alert_role_id, int) or not alert_role_id:
            return

        cooldown = getattr(alerts, "per_task_cooldown_seconds", 0)
        should_send = await self._store.should_alert(
            guild_id, thread_id, "p0", cooldown_seconds=cooldown
        )
        if not should_send:
            return

        try:
            await thread.send(f"<@&{alert_role_id}> Escalated to **p0**.")
        except Exception:
            return
        try:
            await self._store.save_alert(guild_id, thread_id, "p0")
        except Exception:
            pass

    async def _cmd_tasks_list_impl(
        self,
        interaction: Any,
        workspace: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> None:
        guild_id = interaction.guild_id
        tasks = await self._store.list_tasks(guild_id, workspace_id=workspace, state=tag)
        embed = build_tasks_list_embed(tasks, workspace=workspace, tag=tag)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _cmd_tasks_mine_impl(self, interaction: Any) -> None:
        guild_id = interaction.guild_id
        user_id = interaction.user.id if interaction.user else None
        tasks = await self._store.list_tasks(
            guild_id,
            created_by_user_id=user_id,
        )
        embed = build_tasks_list_embed(tasks, workspace=None, tag="mine")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _cmd_workspaces_impl(self, interaction: Any) -> None:
        guild_id = interaction.guild_id
        rows = await self._store.list_scaffolded_channels(guild_id)
        workspaces: dict[str, dict[str, Any]] = {}
        for _gid, workspace_id, channel_type, channel_id, _created_at in rows:
            entry = workspaces.setdefault(
                workspace_id,
                {"workspace_id": workspace_id},
            )
            if channel_type == "tasks":
                entry["tasks_channel_id"] = channel_id
            elif channel_type == "activity":
                entry["activity_channel_id"] = channel_id
            elif channel_type == "approvals":
                entry["approvals_channel_id"] = channel_id

        embed = build_workspaces_embed(list(workspaces.values()))
        await interaction.followup.send(embed=embed, ephemeral=True)
