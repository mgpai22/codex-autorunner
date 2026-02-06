from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from ....core.logging_utils import log_event
from ....core.state import now_iso
from ..constants import MAX_COALESCE_BUFFER_MESSAGES, MAX_COALESCE_DELAY_SECONDS
from ..trigger_mode import should_respond, strip_bot_mention

if TYPE_CHECKING:
    from ..service import DiscordBotService

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.messages")


@dataclass
class _CoalescedBuffer:
    """Buffer for coalescing rapid successive messages."""

    topic_key: str
    texts: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    timer_task: Optional[asyncio.Task[None]] = None


async def handle_message(service: "DiscordBotService", message: Any) -> None:
    """Handle an incoming Discord message."""
    if not HAS_DISCORD:
        return
    if not isinstance(message, discord.Message):
        return
    if message.author.bot:
        return

    content = message.content or ""

    # Check trigger mode
    bot_user_id = service._bot.user.id if service._bot.user else None
    if not should_respond(
        message, trigger_mode=service._config.trigger_mode, bot_user_id=bot_user_id
    ):
        return

    # Strip bot mention from content
    content = strip_bot_mention(content, bot_user_id)
    if not content.strip() and not message.attachments:
        return

    guild_id = message.guild.id if message.guild else None
    if guild_id is None:
        return

    # Determine channel/thread context
    if isinstance(message.channel, discord.Thread):
        channel_id = message.channel.parent_id
        thread_id = message.channel.id
    else:
        channel_id = message.channel.id
        thread_id = None

    from ..helpers import build_topic_key

    topic_key = build_topic_key(guild_id, channel_id, thread_id)

    # Check if there's a workspace binding for this channel
    binding = await service._store.get_channel_binding(f"{guild_id}:{channel_id}")
    if binding is None:
        # No binding - ignore unless in a thread that's already tracked
        record = await service._store.get_topic(topic_key)
        if record is None:
            log_event(
                logger,
                logging.DEBUG,
                "discord.message.no_binding",
                guild_id=guild_id,
                channel_id=channel_id,
            )
            return

    # Check for coalescing (rapid successive messages)
    coalesce_key = topic_key
    coalesce_window = service._config.coalesce_window_seconds

    existing_buffer = (
        service._coalesced_buffers.get(coalesce_key)
        if hasattr(service, "_coalesced_buffers")
        else None
    )
    if existing_buffer is not None:
        existing_buffer.texts.append(content)
        elapsed = time.time() - existing_buffer.created_at
        buffer_full = len(existing_buffer.texts) >= MAX_COALESCE_BUFFER_MESSAGES
        delay_exceeded = elapsed >= MAX_COALESCE_DELAY_SECONDS
        if buffer_full or delay_exceeded:
            # Flush immediately – buffer is full or max delay exceeded
            if existing_buffer.timer_task and not existing_buffer.timer_task.done():
                existing_buffer.timer_task.cancel()
            asyncio.create_task(_coalesce_timer(service, coalesce_key, 0))
        else:
            if existing_buffer.timer_task and not existing_buffer.timer_task.done():
                existing_buffer.timer_task.cancel()
            existing_buffer.timer_task = asyncio.create_task(
                _coalesce_timer(service, coalesce_key, coalesce_window)
            )
        return

    # Create new buffer
    if not hasattr(service, "_coalesced_buffers"):
        service._coalesced_buffers = {}
    if not hasattr(service, "_coalesce_locks"):
        service._coalesce_locks = {}

    buffer = _CoalescedBuffer(topic_key=topic_key, texts=[content])
    service._coalesced_buffers[coalesce_key] = buffer
    buffer.timer_task = asyncio.create_task(
        _coalesce_timer(service, coalesce_key, coalesce_window)
    )


async def _coalesce_timer(
    service: "DiscordBotService",
    coalesce_key: str,
    window: float,
) -> None:
    """Wait for coalescing window, then dispatch the combined message."""
    await asyncio.sleep(window)

    buffer = service._coalesced_buffers.pop(coalesce_key, None)
    if buffer is None:
        return

    combined_text = "\n".join(buffer.texts).strip()
    if not combined_text:
        return

    log_event(
        logger,
        logging.INFO,
        "discord.message.dispatch",
        topic_key=buffer.topic_key,
        text_length=len(combined_text),
        message_count=len(buffer.texts),
    )

    await _dispatch_coalesced_turn(service, buffer.topic_key, combined_text)


async def _dispatch_coalesced_turn(
    service: "DiscordBotService",
    topic_key: str,
    combined_text: str,
) -> None:
    """Resolve workspace binding, ensure topic record, and spawn a turn."""
    from ..helpers import split_topic_key
    from ..state import DiscordTopicRecord

    # Parse topic key back to Discord IDs
    try:
        guild_id, channel_id, thread_id = split_topic_key(topic_key)
    except ValueError:
        log_event(
            logger,
            logging.WARNING,
            "discord.message.invalid_topic_key",
            topic_key=topic_key,
        )
        return

    # Check if a turn is already active
    if service._is_turn_active(topic_key):
        log_event(
            logger,
            logging.DEBUG,
            "discord.message.turn_already_active",
            topic_key=topic_key,
        )
        return

    # Resolve workspace binding
    channel_key = f"{guild_id}:{channel_id}"
    binding = await service._store.get_channel_binding(channel_key)

    # Get or create topic record
    record = await service._store.get_topic(topic_key)
    if record is None:
        if binding is None:
            log_event(
                logger,
                logging.DEBUG,
                "discord.message.no_binding_for_dispatch",
                topic_key=topic_key,
            )
            return
        record = DiscordTopicRecord(
            topic_key=topic_key,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=thread_id,
            workspace_path=binding,
            approval_mode=service._config.defaults.approval_mode,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        await service._store.save_topic(topic_key, record)
    elif not record.workspace_path and binding:
        record.workspace_path = binding
        record.updated_at = now_iso()
        await service._store.save_topic(topic_key, record)

    if not record.workspace_path:
        log_event(
            logger,
            logging.DEBUG,
            "discord.message.no_workspace",
            topic_key=topic_key,
        )
        return

    log_event(
        logger,
        logging.INFO,
        "discord.message.turn_pending",
        topic_key=topic_key,
        prompt_preview=combined_text[:80],
    )

    # Spawn the turn as a background task
    service._spawn_task(
        service._execute_turn(
            topic_key,
            combined_text,
            channel_id=channel_id,
            thread_id=thread_id,
            reply_to=None,
            record=record,
        )
    )
