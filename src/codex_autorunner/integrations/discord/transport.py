from __future__ import annotations

import io
import logging
from typing import Any, Optional

from ...core.logging_utils import log_event
from .constants import DISCORD_MAX_MESSAGE_LENGTH
from .overflow import (
    choose_overflow_strategy,
    split_message,
)

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

logger = logging.getLogger("codex_autorunner.integrations.discord.transport")


class DiscordMessageTransport:
    """Mixin providing message send/edit/delete operations for the Discord bot service."""

    async def _send_message(
        self,
        channel_id: int,
        text: Optional[str] = None,
        *,
        embed: Optional[Any] = None,
        view: Optional[Any] = None,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        file: Optional[Any] = None,
    ) -> Optional[int]:
        """Send a message to a channel or thread. Returns message_id on success."""
        try:
            target_id = thread_id or channel_id
            channel = self._bot.get_channel(target_id)
            if channel is None:
                channel = await self._bot.fetch_channel(target_id)
            if channel is None:
                log_event(
                    logger,
                    logging.WARNING,
                    "discord.transport.channel_not_found",
                    channel_id=target_id,
                )
                return None

            kwargs: dict[str, Any] = {}
            if text:
                kwargs["content"] = text[:DISCORD_MAX_MESSAGE_LENGTH]
            if embed:
                kwargs["embed"] = embed
            if view:
                kwargs["view"] = view
            if file:
                kwargs["file"] = file
            if reply_to and hasattr(channel, "fetch_message"):
                try:
                    ref_msg = await channel.fetch_message(reply_to)
                    kwargs["reference"] = ref_msg
                except Exception:
                    pass

            msg = await channel.send(**kwargs)
            return msg.id
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "discord.transport.send_failed",
                channel_id=channel_id,
                thread_id=thread_id,
                exc=exc,
            )
            return None

    async def _edit_message(
        self,
        channel_id: int,
        message_id: int,
        text: Optional[str] = None,
        *,
        embed: Optional[Any] = None,
        view: Optional[Any] = None,
    ) -> bool:
        """Edit an existing message. Returns True on success."""
        try:
            target_channel = self._bot.get_channel(channel_id)
            if target_channel is None:
                target_channel = await self._bot.fetch_channel(channel_id)
            if target_channel is None or not hasattr(target_channel, "fetch_message"):
                return False
            msg = await target_channel.fetch_message(message_id)
            kwargs: dict[str, Any] = {}
            if text is not None:
                kwargs["content"] = text[:DISCORD_MAX_MESSAGE_LENGTH]
            if embed is not None:
                kwargs["embed"] = embed
            if view is not None:
                kwargs["view"] = view
            await msg.edit(**kwargs)
            return True
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "discord.transport.edit_failed",
                channel_id=channel_id,
                message_id=message_id,
                exc=exc,
            )
            return False

    async def _delete_message(
        self,
        channel_id: int,
        message_id: int,
    ) -> bool:
        """Delete a message. Returns True on success."""
        try:
            target_channel = self._bot.get_channel(channel_id)
            if target_channel is None:
                target_channel = await self._bot.fetch_channel(channel_id)
            if target_channel is None or not hasattr(target_channel, "fetch_message"):
                return False
            msg = await target_channel.fetch_message(message_id)
            await msg.delete()
            return True
        except Exception as exc:
            log_event(
                logger,
                logging.WARNING,
                "discord.transport.delete_failed",
                channel_id=channel_id,
                message_id=message_id,
                exc=exc,
            )
            return False

    async def _send_placeholder(
        self,
        channel_id: int,
        text: str = "Working...",
        *,
        embed: Optional[Any] = None,
        thread_id: Optional[int] = None,
    ) -> Optional[int]:
        """Send a placeholder message. Returns message_id."""
        return await self._send_message(
            channel_id, text, embed=embed, thread_id=thread_id
        )

    async def _send_file(
        self,
        channel_id: int,
        data: bytes,
        filename: str,
        *,
        thread_id: Optional[int] = None,
    ) -> Optional[int]:
        """Send a file attachment."""
        try:
            file = discord.File(io.BytesIO(data), filename=filename)
            return await self._send_message(channel_id, file=file, thread_id=thread_id)
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "discord.transport.send_file_failed",
                channel_id=channel_id,
                filename=filename,
                exc=exc,
            )
            return None

    async def _deliver_turn_response(
        self,
        channel_id: int,
        *,
        thread_id: Optional[int] = None,
        placeholder_id: Optional[int] = None,
        response: str,
        metrics: Optional[dict[str, Any]] = None,
    ) -> Optional[int]:
        """Deliver the final response of an agent turn."""
        from .rendering import build_response_embed

        target_id = thread_id or channel_id
        strategy = choose_overflow_strategy(response, self._config.message_overflow)

        if strategy == "plain" and placeholder_id:
            ok = await self._edit_message(target_id, placeholder_id, response)
            return placeholder_id if ok else None

        # Delete placeholder if we'll send a new message
        if placeholder_id:
            await self._delete_message(target_id, placeholder_id)

        if strategy == "embed":
            embed = build_response_embed(response, metrics=metrics)
            return await self._send_message(target_id, embed=embed)

        if strategy == "file":
            file_data = response.encode("utf-8")
            file_msg_id = await self._send_file(target_id, file_data, "response.md")
            if metrics:
                embed = build_response_embed("(see attached file)", metrics=metrics)
                await self._send_message(target_id, embed=embed)
            return file_msg_id

        # Default: split
        chunks = split_message(response)
        last_id = None
        for chunk in chunks:
            last_id = await self._send_message(target_id, chunk)
        return last_id
