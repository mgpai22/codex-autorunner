from __future__ import annotations

from typing import Any, Optional

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


def should_respond(
    message: Any,
    *,
    trigger_mode: str,
    bot_user_id: Optional[int],
) -> bool:
    """Determine whether the bot should respond to a message based on trigger mode.

    trigger_mode:
        "all"      – respond to every message in allowed channels
        "mentions" – only respond when the bot is @mentioned
    """
    if trigger_mode == "all":
        return True

    if trigger_mode == "mentions":
        if not HAS_DISCORD or not isinstance(message, discord.Message):
            return False
        if bot_user_id is None:
            return False
        # Check if the bot is mentioned
        for mention in message.mentions:
            if mention.id == bot_user_id:
                return True
        # Also check if the message is a reply to the bot
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if hasattr(ref, "author") and ref.author.id == bot_user_id:
                return True
        return False

    return False


def strip_bot_mention(
    content: str,
    bot_user_id: Optional[int],
) -> str:
    """Remove the bot mention from the beginning of message content."""
    if bot_user_id is None:
        return content
    mention_patterns = [f"<@{bot_user_id}>", f"<@!{bot_user_id}>"]
    stripped = content.strip()
    for pattern in mention_patterns:
        if stripped.startswith(pattern):
            stripped = stripped[len(pattern) :].strip()
            break
    return stripped
