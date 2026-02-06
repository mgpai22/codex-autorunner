from __future__ import annotations

from typing import Any, Optional, Sequence

from ...constants import (
    DISCORD_EMBED_DESC_LIMIT,
    DISCORD_EMBED_FIELD_VALUE_LIMIT,
    EMBED_COLOR_INFO,
)

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


class FormattingHelpers:
    """Mixin providing consistent formatting helpers for Discord embeds."""

    @staticmethod
    def _truncate_field(text: str, limit: int = DISCORD_EMBED_FIELD_VALUE_LIMIT) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    def _build_info_embed(
        title: str,
        description: str,
        *,
        fields: Optional[list[tuple[str, str, bool]]] = None,
    ) -> Any:
        if not HAS_DISCORD:
            return None
        embed = discord.Embed(
            title=title[:256],
            description=description[:DISCORD_EMBED_DESC_LIMIT],
            color=EMBED_COLOR_INFO,
        )
        if fields:
            for name, value, inline in fields[:25]:
                embed.add_field(
                    name=name[:256],
                    value=FormattingHelpers._truncate_field(value),
                    inline=inline,
                )
        return embed

    @staticmethod
    def _format_code_block(text: str, language: str = "") -> str:
        return f"```{language}\n{text}\n```"

    @staticmethod
    def _format_key_value(items: Sequence[tuple[str, str]]) -> str:
        if not items:
            return "—"
        lines = [f"**{key}**: {value}" for key, value in items]
        return "\n".join(lines)
