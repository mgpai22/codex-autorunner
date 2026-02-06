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
