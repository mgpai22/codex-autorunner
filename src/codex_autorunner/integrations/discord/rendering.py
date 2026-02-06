from __future__ import annotations

import re
from typing import Any, Optional, Sequence

from .constants import (
    DISCORD_EMBED_DESC_LIMIT,
    DISCORD_EMBED_FIELD_NAME_LIMIT,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    DISCORD_EMBED_FIELDS_LIMIT,
    DISCORD_EMBED_TITLE_LIMIT,
    EMBED_COLOR_ERROR,
    EMBED_COLOR_INFO,
    EMBED_COLOR_PROGRESS,
    EMBED_COLOR_SUCCESS,
    EMBED_COLOR_WARNING,
    STATUS_ICONS,
)

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_status_embed(
    *,
    workspace: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    approval_mode: Optional[str] = None,
    thread_id: Optional[str] = None,
    guild_name: Optional[str] = None,
) -> "discord.Embed":
    """Build a status embed showing current workspace/agent/model configuration."""
    embed = discord.Embed(title="Status", color=EMBED_COLOR_INFO)
    if workspace:
        embed.add_field(
            name="Workspace",
            value=_truncate(workspace, DISCORD_EMBED_FIELD_VALUE_LIMIT),
            inline=True,
        )
    if agent:
        embed.add_field(name="Agent", value=agent, inline=True)
    if model:
        embed.add_field(name="Model", value=model, inline=True)
    if approval_mode:
        embed.add_field(name="Approval Mode", value=approval_mode, inline=True)
    if thread_id:
        embed.add_field(name="Thread", value=_truncate(thread_id, 50), inline=True)
    return embed


def build_response_embed(
    response_text: str,
    *,
    metrics: Optional[dict[str, Any]] = None,
    color: int = EMBED_COLOR_SUCCESS,
    title: Optional[str] = None,
) -> "discord.Embed":
    """Build an embed for an agent response."""
    description = _truncate(response_text, DISCORD_EMBED_DESC_LIMIT)
    embed = discord.Embed(description=description, color=color)
    if title:
        embed.title = _truncate(title, DISCORD_EMBED_TITLE_LIMIT)
    if metrics:
        footer_parts: list[str] = []
        if "tokens_in" in metrics:
            footer_parts.append(f"In: {metrics['tokens_in']}")
        if "tokens_out" in metrics:
            footer_parts.append(f"Out: {metrics['tokens_out']}")
        if "elapsed" in metrics:
            footer_parts.append(f"Time: {metrics['elapsed']:.1f}s")
        if footer_parts:
            embed.set_footer(text=" | ".join(footer_parts))
    return embed


def build_error_embed(
    error_message: str,
    *,
    title: str = "Error",
) -> "discord.Embed":
    """Build an error embed."""
    return discord.Embed(
        title=_truncate(title, DISCORD_EMBED_TITLE_LIMIT),
        description=_truncate(error_message, DISCORD_EMBED_DESC_LIMIT),
        color=EMBED_COLOR_ERROR,
    )


def build_warning_embed(
    message: str,
    *,
    title: str = "Warning",
) -> "discord.Embed":
    """Build a warning embed."""
    return discord.Embed(
        title=_truncate(title, DISCORD_EMBED_TITLE_LIMIT),
        description=_truncate(message, DISCORD_EMBED_DESC_LIMIT),
        color=EMBED_COLOR_WARNING,
    )


def build_progress_embed(
    actions: list[dict[str, Any]],
    *,
    elapsed: float = 0.0,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    step_count: int = 0,
    context_pct: Optional[float] = None,
) -> "discord.Embed":
    """Build a progress embed for live turn updates."""
    lines: list[str] = []
    for action in actions[-5:]:  # Show last 5 actions
        icon = STATUS_ICONS.get(
            action.get("status", "running"), STATUS_ICONS["running"]
        )
        label = action.get("label", "")
        output = action.get("output", "")
        line = f"{icon} {label}"
        if output:
            line += f"\n```\n{_truncate(output, 120)}\n```"
        lines.append(line)

    description = "\n".join(lines) if lines else "Starting..."
    embed = discord.Embed(
        description=_truncate(description, DISCORD_EMBED_DESC_LIMIT),
        color=EMBED_COLOR_PROGRESS,
    )

    footer_parts: list[str] = []
    if agent:
        footer_parts.append(agent)
    if model:
        footer_parts.append(model)
    if elapsed > 0:
        mins = int(elapsed // 60)
        secs = elapsed % 60
        footer_parts.append(f"{mins}m {secs:.0f}s" if mins else f"{secs:.1f}s")
    if step_count > 0:
        footer_parts.append(f"Step {step_count}")
    if context_pct is not None:
        footer_parts.append(f"Context: {context_pct:.0f}%")
    if footer_parts:
        embed.set_footer(text=" | ".join(footer_parts))

    return embed


def build_repos_embed(
    repos: Sequence[dict[str, Any]],
) -> "discord.Embed":
    """Build an embed listing available repositories."""
    embed = discord.Embed(title="Repositories", color=EMBED_COLOR_INFO)
    for i, repo in enumerate(repos[:DISCORD_EMBED_FIELDS_LIMIT]):
        name = repo.get("name", repo.get("path", f"repo-{i}"))
        path = repo.get("path", "")
        embed.add_field(
            name=_truncate(name, DISCORD_EMBED_FIELD_NAME_LIMIT),
            value=_truncate(path, DISCORD_EMBED_FIELD_VALUE_LIMIT) or "\u2014",
            inline=False,
        )
    if len(repos) > DISCORD_EMBED_FIELDS_LIMIT:
        embed.set_footer(
            text=f"Showing {DISCORD_EMBED_FIELDS_LIMIT} of {len(repos)} repos"
        )
    return embed


def build_setup_summary_embed(
    workspaces: Sequence[Any],
    channels_created: Sequence[Any],
    tags_created: Sequence[Any],
) -> "discord.Embed":
    """Build a summary embed for /setup scaffolding."""
    embed = discord.Embed(title="Setup Complete", color=EMBED_COLOR_SUCCESS)

    workspace_names: list[str] = []
    for ws in workspaces:
        name: Optional[str] = None
        if isinstance(ws, dict):
            raw = ws.get("display_name") or ws.get("name") or ws.get("id")
            name = raw if isinstance(raw, str) else None
        else:
            raw = getattr(ws, "display_name", None) or getattr(ws, "name", None)
            if not isinstance(raw, str) or not raw:
                raw = getattr(ws, "id", None)
            name = raw if isinstance(raw, str) else None
        if name:
            workspace_names.append(name)

    created_by_type: dict[str, int] = {}
    for item in channels_created:
        if isinstance(item, dict):
            raw_type = item.get("channel_type")
            if isinstance(raw_type, str) and raw_type:
                created_by_type[raw_type] = created_by_type.get(raw_type, 0) + 1

    description_lines = [
        f"Workspaces: {len(workspaces)}",
        f"Channels created: {len(channels_created)}",
        f"Forum tags created: {len(tags_created)}",
    ]
    embed.description = _truncate("\n".join(description_lines), DISCORD_EMBED_DESC_LIMIT)

    if workspace_names:
        embed.add_field(
            name="Workspaces",
            value=_truncate(
                "\n".join(workspace_names[:DISCORD_EMBED_FIELDS_LIMIT]),
                DISCORD_EMBED_FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )

    if created_by_type:
        parts = [f"{k}: {v}" for k, v in sorted(created_by_type.items())]
        embed.add_field(
            name="Created Channels",
            value=_truncate(
                ", ".join(parts),
                DISCORD_EMBED_FIELD_VALUE_LIMIT,
            ),
            inline=False,
        )

    if len(workspace_names) > DISCORD_EMBED_FIELDS_LIMIT:
        embed.set_footer(
            text=f"Showing {DISCORD_EMBED_FIELDS_LIMIT} of {len(workspace_names)} workspaces"
        )

    return embed


def build_task_card_embed(
    workspace: Any,
    prompt: str,
    user: Any,
    status: str,
    created_at: str,
    updated_at: str,
    *,
    agent_response_url: Optional[str] = None,
    activity_url: Optional[str] = None,
) -> "discord.Embed":
    """Build the Task Card embed shown as the forum thread starter message."""
    workspace_label = ""
    if isinstance(workspace, str):
        workspace_label = workspace
    elif isinstance(workspace, dict):
        for key in ("name", "path", "id", "workspace_id", "workspaceId"):
            value = workspace.get(key)
            if isinstance(value, str) and value.strip():
                workspace_label = value.strip()
                break
    elif workspace is not None:
        workspace_label = str(workspace)

    initiator = ""
    if hasattr(user, "mention"):
        initiator = str(user.mention)
    elif hasattr(user, "display_name"):
        initiator = str(user.display_name)
    elif isinstance(user, int) and not isinstance(user, bool):
        initiator = f"<@{user}>"
    elif isinstance(user, str):
        initiator = user
    elif user is not None:
        initiator = str(user)

    status_key = status.strip().lower() if isinstance(status, str) else ""
    if status_key in ("failed", "timeout"):
        color = EMBED_COLOR_ERROR
    elif status_key in ("needs-approval", "blocked", "stopped"):
        color = EMBED_COLOR_WARNING
    elif status_key in ("running",):
        color = EMBED_COLOR_PROGRESS
    elif status_key in ("done",):
        color = EMBED_COLOR_SUCCESS
    else:
        color = EMBED_COLOR_INFO

    title = "Task"
    if workspace_label:
        title = f"Task \u2022 {workspace_label}"

    description = prompt.strip() if isinstance(prompt, str) else ""
    if not description:
        description = "(no prompt)"

    embed = discord.Embed(
        title=_truncate(title, DISCORD_EMBED_TITLE_LIMIT),
        description=_truncate(description, DISCORD_EMBED_DESC_LIMIT),
        color=color,
    )

    if workspace_label:
        embed.add_field(
            name="Workspace",
            value=_truncate(workspace_label, DISCORD_EMBED_FIELD_VALUE_LIMIT),
            inline=True,
        )

    if initiator:
        embed.add_field(
            name="Initiator",
            value=_truncate(initiator, DISCORD_EMBED_FIELD_VALUE_LIMIT),
            inline=True,
        )

    embed.add_field(
        name="Status",
        value=_truncate(status or "unknown", DISCORD_EMBED_FIELD_VALUE_LIMIT),
        inline=True,
    )

    if isinstance(workspace, dict):
        approval_mode = workspace.get("approval_mode") or workspace.get("approvalMode")
        approval_policy = workspace.get("approval_policy") or workspace.get(
            "approvalPolicy"
        )
        sandbox_policy = workspace.get("sandbox_policy") or workspace.get(
            "sandboxPolicy"
        )
        parts: list[str] = []
        if isinstance(approval_mode, str) and approval_mode:
            parts.append(f"mode: `{approval_mode}`")
        if isinstance(approval_policy, str) and approval_policy:
            parts.append(f"approval: `{approval_policy}`")
        if isinstance(sandbox_policy, str) and sandbox_policy:
            parts.append(f"sandbox: `{sandbox_policy}`")
        if parts:
            embed.add_field(
                name="Approvals",
                value=_truncate(
                    " | ".join(parts), DISCORD_EMBED_FIELD_VALUE_LIMIT
                ),
                inline=False,
            )

    embed.add_field(
        name="Created",
        value=_truncate(created_at or "\u2014", DISCORD_EMBED_FIELD_VALUE_LIMIT),
        inline=True,
    )
    embed.add_field(
        name="Updated",
        value=_truncate(updated_at or "\u2014", DISCORD_EMBED_FIELD_VALUE_LIMIT),
        inline=True,
    )

    links: list[str] = []
    if isinstance(agent_response_url, str) and agent_response_url:
        links.append(f"[Agent response]({agent_response_url})")
    if isinstance(activity_url, str) and activity_url:
        links.append(f"[Activity]({activity_url})")
    if links:
        embed.add_field(
            name="Links",
            value=_truncate(
                " | ".join(links), DISCORD_EMBED_FIELD_VALUE_LIMIT
            ),
            inline=False,
        )

    return embed


def build_tasks_list_embed(
    tasks: Sequence[Any],
    *,
    workspace: Optional[str] = None,
    tag: Optional[str] = None,
) -> "discord.Embed":
    """Build an embed listing forum-backed tasks from SQLite state."""
    title = "Tasks"
    if workspace:
        title += f" \u2022 {workspace}"
    if tag:
        title += f" \u2022 {tag}"

    lines: list[str] = []
    for task in tasks[:200]:
        if isinstance(task, dict):
            thread_id = task.get("thread_id")
            state = task.get("last_state") or task.get("state") or "unknown"
            prompt = task.get("initial_prompt") or ""
        else:
            thread_id = getattr(task, "thread_id", None)
            state = getattr(task, "last_state", None) or getattr(task, "state", None)
            state = state or "unknown"
            prompt = getattr(task, "initial_prompt", "") or ""

        thread_ref = f"<#{thread_id}>" if isinstance(thread_id, int) else "(missing)"
        preview = prompt.strip().replace("\n", " ")
        preview = _truncate(preview, 90) if preview else "(no prompt)"
        lines.append(f"\u2022 {thread_ref} \u2014 **{state}** \u2014 {preview}")

        if len("\n".join(lines)) > DISCORD_EMBED_DESC_LIMIT - 200:
            break

    embed = discord.Embed(title=_truncate(title, DISCORD_EMBED_TITLE_LIMIT))
    if lines:
        embed.description = _truncate("\n".join(lines), DISCORD_EMBED_DESC_LIMIT)
    else:
        embed.description = "No tasks found."
    embed.color = EMBED_COLOR_INFO
    embed.set_footer(text=f"Showing {min(len(tasks), len(lines))} of {len(tasks)}")
    return embed


def build_workspaces_embed(workspaces_with_channels: Sequence[Any]) -> "discord.Embed":
    """Build an embed listing workspaces and their scaffolded channels."""
    embed = discord.Embed(title="Workspaces", color=EMBED_COLOR_INFO)

    for entry in workspaces_with_channels[:DISCORD_EMBED_FIELDS_LIMIT]:
        if isinstance(entry, dict):
            workspace_id = entry.get("workspace_id") or entry.get("workspaceId") or ""
            tasks_id = entry.get("tasks_channel_id") or entry.get("tasks")
            activity_id = entry.get("activity_channel_id") or entry.get("activity")
            approvals_id = entry.get("approvals_channel_id") or entry.get("approvals")
        else:
            workspace_id = getattr(entry, "workspace_id", "") or ""
            tasks_id = getattr(entry, "tasks_channel_id", None)
            activity_id = getattr(entry, "activity_channel_id", None)
            approvals_id = getattr(entry, "approvals_channel_id", None)

        def _ch(val: Any) -> str:
            return f"<#{val}>" if isinstance(val, int) else "\u2014"

        value = "\n".join(
            [
                f"Tasks: {_ch(tasks_id)}",
                f"Activity: {_ch(activity_id)}",
                f"Approvals: {_ch(approvals_id)}",
            ]
        )

        embed.add_field(
            name=_truncate(str(workspace_id) or "(unknown)", DISCORD_EMBED_FIELD_NAME_LIMIT),
            value=_truncate(value, DISCORD_EMBED_FIELD_VALUE_LIMIT),
            inline=False,
        )

    if len(workspaces_with_channels) > DISCORD_EMBED_FIELDS_LIMIT:
        embed.set_footer(
            text=(
                f"Showing {DISCORD_EMBED_FIELDS_LIMIT} of "
                f"{len(workspaces_with_channels)} workspaces"
            )
        )

    return embed


def _dashboard_status_icon(status: str, active_tasks: int) -> str:
    key = (status or "").strip().lower()
    if active_tasks > 0 or key in {"running"}:
        return STATUS_ICONS.get("running", "\u25b8")
    if key in {"error", "failed", "init_error"}:
        return STATUS_ICONS.get("fail", "\u2717")
    if key in {"locked", "paused", "stopped"}:
        return STATUS_ICONS.get("warn", "\u26a0")
    return STATUS_ICONS.get("done", "\u2713")


def build_dashboard_embed(workspaces_status: list[dict[str, Any]]) -> "discord.Embed":
    """Build a pinned dashboard embed showing per-workspace status."""
    embed = discord.Embed(title="Dashboard", color=EMBED_COLOR_INFO)

    if not workspaces_status:
        embed.description = "No workspaces found."
        return embed

    for workspace in workspaces_status[:DISCORD_EMBED_FIELDS_LIMIT]:
        raw_name = (
            workspace.get("name")
            or workspace.get("display_name")
            or workspace.get("workspace")
            or workspace.get("id")
            or "workspace"
        )
        name = str(raw_name)

        raw_active = workspace.get("active_tasks", workspace.get("activeTasks", 0))
        try:
            active_tasks = int(raw_active)
        except (TypeError, ValueError):
            active_tasks = 0

        raw_last = workspace.get("last_activity", workspace.get("lastActivity", ""))
        last_activity = raw_last if isinstance(raw_last, str) else str(raw_last or "")

        raw_status = workspace.get("status", "")
        status = raw_status if isinstance(raw_status, str) else str(raw_status or "")

        icon = _dashboard_status_icon(status, active_tasks)
        field_name = _truncate(
            f"{icon} {name}", DISCORD_EMBED_FIELD_NAME_LIMIT
        )

        lines = [f"Active: `{max(active_tasks, 0)}`"]
        if status:
            lines.append(f"Status: `{_truncate(status, 48)}`")
        if last_activity:
            lines.append(f"Last: `{_truncate(last_activity, 80)}`")

        value = _truncate("\n".join(lines), DISCORD_EMBED_FIELD_VALUE_LIMIT) or "\u2014"
        embed.add_field(name=field_name, value=value, inline=False)

    if len(workspaces_status) > DISCORD_EMBED_FIELDS_LIMIT:
        embed.set_footer(
            text=f"Showing {DISCORD_EMBED_FIELDS_LIMIT} of {len(workspaces_status)} workspaces"
        )

    return embed


def build_agent_bus_embed(
    agent_name: str,
    event_type: str,
    details: Any,
) -> "discord.Embed":
    """Build a structured coordination embed for the agent bus."""
    raw_event = event_type or "event"
    title = raw_event.replace("_", " ").strip().title()

    color = EMBED_COLOR_INFO
    lower = raw_event.strip().lower()
    if any(tok in lower for tok in ("failed", "error")):
        color = EMBED_COLOR_ERROR
    elif any(tok in lower for tok in ("paused", "stopped")):
        color = EMBED_COLOR_WARNING
    elif "completed" in lower:
        color = EMBED_COLOR_SUCCESS

    embed = discord.Embed(
        title=_truncate(title, DISCORD_EMBED_TITLE_LIMIT),
        color=color,
    )
    if agent_name:
        embed.set_author(name=_truncate(agent_name, DISCORD_EMBED_FIELD_NAME_LIMIT))

    if isinstance(details, str):
        embed.description = _truncate(details, DISCORD_EMBED_DESC_LIMIT)
        return embed

    if isinstance(details, dict):
        lines: list[str] = []
        for key, value in list(details.items())[:12]:
            key_str = _truncate(str(key), 64)
            val_str = _truncate(str(value), 240)
            lines.append(f"**{key_str}**: {val_str}")
        embed.description = _truncate("\n".join(lines), DISCORD_EMBED_DESC_LIMIT)
        return embed

    embed.description = _truncate(str(details), DISCORD_EMBED_DESC_LIMIT)
    return embed


def build_handoff_embed(
    source_workspace: str,
    target_workspace: str,
    reason: str,
) -> "discord.Embed":
    """Build a cross-workspace handoff embed."""
    header = f"**{source_workspace}** \u2192 **{target_workspace}**"
    description = header + (f"\n{reason}" if reason else "")
    return discord.Embed(
        title="Handoff",
        description=_truncate(description, DISCORD_EMBED_DESC_LIMIT),
        color=EMBED_COLOR_INFO,
    )


def markdown_to_discord(text: str) -> str:
    """Convert text that may contain Telegram HTML to Discord-compatible markdown.

    Discord natively supports markdown, so this is mostly about stripping HTML tags.
    """
    if not text:
        return ""
    # Strip common Telegram HTML tags
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\n\1\n```", text, flags=re.DOTALL)
    text = re.sub(r'<a href="(.*?)">(.*?)</a>', r"[\2](\1)", text)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)  # strip remaining tags
    return text
