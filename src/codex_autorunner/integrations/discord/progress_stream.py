from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .constants import (
    COMPACT_MAX_ACTIONS,
    COMPACT_MAX_TEXT_LENGTH,
    EMBED_COLOR_ERROR,
    EMBED_COLOR_PROGRESS,
    EMBED_COLOR_SUCCESS,
    STATUS_ICONS,
)

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


def format_elapsed(seconds: float) -> str:
    total = max(int(seconds), 0)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


@dataclass
class ProgressAction:
    label: str
    text: str
    status: str
    item_id: Optional[str] = None
    subagent_label: Optional[str] = None


@dataclass
class TurnProgressTracker:
    """Tracks agent turn progress and renders as Discord embeds."""

    started_at: float
    agent: str
    model: str
    label: str
    max_actions: int = COMPACT_MAX_ACTIONS
    max_output_chars: int = COMPACT_MAX_TEXT_LENGTH
    actions: list[ProgressAction] = field(default_factory=list)
    step: int = 0
    last_output_index: Optional[int] = None
    last_thinking_index: Optional[int] = None
    context_usage_percent: Optional[int] = None
    finalized: bool = False

    def set_label(self, label: str) -> None:
        if label:
            self.label = label

    def set_context_usage_percent(self, percent: Optional[int]) -> None:
        if percent is None:
            self.context_usage_percent = None
            return
        self.context_usage_percent = min(max(int(percent), 0), 100)

    def add_action(
        self,
        label: str,
        text: str,
        status: str,
        *,
        item_id: Optional[str] = None,
        track_output: bool = False,
        track_thinking: bool = False,
        subagent_label: Optional[str] = None,
    ) -> None:
        normalized = _normalize_text(text)
        if not normalized:
            return
        self.actions.append(
            ProgressAction(
                label=label,
                text=normalized,
                status=status,
                item_id=item_id,
                subagent_label=subagent_label,
            )
        )
        idx = len(self.actions) - 1
        if track_output:
            self.last_output_index = idx
        if track_thinking:
            self.last_thinking_index = idx
        self.step += 1

    def update_last_output(self, text: str) -> None:
        if self.last_output_index is None:
            return
        if self.last_output_index >= len(self.actions):
            return
        normalized = _normalize_text(text)
        if normalized:
            self.actions[self.last_output_index].text = normalized

    def update_last_thinking(self, text: str) -> None:
        if self.last_thinking_index is None:
            return
        if self.last_thinking_index >= len(self.actions):
            return
        normalized = _normalize_text(text)
        if normalized:
            self.actions[self.last_thinking_index].text = normalized

    def finalize(self, status: str = "done") -> None:
        self.finalized = True
        for action in self.actions:
            if action.status == "running":
                action.status = status


def render_progress_text(tracker: TurnProgressTracker) -> str:
    """Render progress as plain text (for contexts where embeds aren't appropriate)."""
    elapsed = format_elapsed(time.time() - tracker.started_at)
    lines: list[str] = []
    visible = tracker.actions[-tracker.max_actions :]
    for action in visible:
        icon = STATUS_ICONS.get(action.status, STATUS_ICONS.get("running", ">"))
        label = action.label
        if action.subagent_label:
            label = f"[{action.subagent_label}] {label}"
        text = _truncate(action.text, tracker.max_output_chars)
        lines.append(f"{icon} {label}: {text}")

    footer_parts = [tracker.agent, tracker.model, elapsed]
    if tracker.step > 0:
        footer_parts.append(f"step {tracker.step}")
    if tracker.context_usage_percent is not None:
        footer_parts.append(f"ctx {tracker.context_usage_percent}%")
    lines.append(" | ".join(footer_parts))
    return "\n".join(lines)


def render_progress_embed(tracker: TurnProgressTracker) -> Any:
    """Render progress as a Discord embed with color-coded status."""
    if not HAS_DISCORD:
        return None

    elapsed = format_elapsed(time.time() - tracker.started_at)
    color = EMBED_COLOR_SUCCESS if tracker.finalized else EMBED_COLOR_PROGRESS

    # Check for errors
    if tracker.finalized:
        has_fail = any(a.status == "fail" for a in tracker.actions)
        if has_fail:
            color = EMBED_COLOR_ERROR

    lines: list[str] = []
    visible = tracker.actions[-tracker.max_actions :]
    for action in visible:
        icon = STATUS_ICONS.get(action.status, STATUS_ICONS.get("running", ">"))
        label = action.label
        if action.subagent_label:
            label = f"[{action.subagent_label}] {label}"
        text = _truncate(action.text, tracker.max_output_chars)
        lines.append(f"{icon} **{label}**: {text}")

    description = "\n".join(lines) if lines else "Starting..."
    embed = discord.Embed(description=description[:4096], color=color)

    footer_parts = [tracker.agent, tracker.model, elapsed]
    if tracker.step > 0:
        footer_parts.append(f"step {tracker.step}")
    if tracker.context_usage_percent is not None:
        footer_parts.append(f"ctx {tracker.context_usage_percent}%")
    embed.set_footer(text=" | ".join(footer_parts))

    return embed
