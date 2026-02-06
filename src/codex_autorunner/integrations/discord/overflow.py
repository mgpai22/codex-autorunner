from __future__ import annotations

import io
from typing import Optional

from .constants import DISCORD_EMBED_DESC_LIMIT, DISCORD_MAX_MESSAGE_LENGTH

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


def split_message(text: str, max_len: int = DISCORD_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split *text* into chunks of at most *max_len* characters.

    Splitting prefers paragraph breaks (``\\n\\n``), then line breaks, then
    spaces.  Code fences are tracked so that a chunk ending inside a fenced
    block is closed with `` ``` `` and the next chunk reopened with the
    original fence info line.
    """
    if not text:
        return []
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    open_fence: Optional[str] = None

    while remaining:
        # If we are inside an open fence, prepend the reopening marker.
        prefix = f"```{open_fence}\n" if open_fence is not None else ""
        budget = max_len - len(prefix)
        if budget <= 0:
            budget = 1

        cut = _slice_to_boundary(remaining, budget)
        chunk_text = remaining[:cut] if cut > 0 else remaining[:1]

        # Track fence state through this chunk.
        end_fence = _scan_fence_state(chunk_text, open_fence=open_fence)

        # If the chunk ends with an open fence, close it.
        suffix = ""
        if end_fence is not None:
            suffix = "\n```" if not chunk_text.endswith("\n") else "```"

        assembled = f"{prefix}{chunk_text}{suffix}"
        chunks.append(assembled)

        remaining = remaining[len(chunk_text) :]
        open_fence = end_fence

    return chunks


def needs_overflow(text: str) -> bool:
    """Return ``True`` if *text* exceeds the Discord plain-message limit."""
    return len(text) > DISCORD_MAX_MESSAGE_LENGTH


def overflow_as_embed(text: str) -> Optional["discord.Embed"]:
    """Return an :class:`discord.Embed` if *text* fits in the embed description.

    Returns ``None`` when *text* exceeds the embed description limit.
    """
    if not HAS_DISCORD:
        return None
    if len(text) > DISCORD_EMBED_DESC_LIMIT:
        return None
    return discord.Embed(description=text)


def overflow_as_file(text: str, filename: str = "response.md") -> "discord.File":
    """Wrap *text* in a :class:`discord.File` attachment."""
    if not HAS_DISCORD:
        raise RuntimeError("discord.py is not installed")
    buf = io.BytesIO(text.encode("utf-8"))
    return discord.File(buf, filename=filename)


def choose_overflow_strategy(text: str, strategy: str = "split") -> str:
    """Decide how to deliver *text* given its length and the configured *strategy*.

    Returns one of ``"plain"``, ``"split"``, ``"embed"``, or ``"file"``.
    """
    length = len(text)
    if length <= DISCORD_MAX_MESSAGE_LENGTH:
        return "plain"
    if strategy == "split":
        return "split"
    if length <= DISCORD_EMBED_DESC_LIMIT:
        return "embed"
    return "file"


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _slice_to_boundary(text: str, limit: int) -> int:
    """Return the number of characters to take from *text* respecting *limit*.

    Prefers paragraph, line, then word boundaries.
    """
    if len(text) <= limit:
        return len(text)
    # Paragraph break.
    para = text.rfind("\n\n", 0, limit + 1)
    if para != -1 and para + 2 <= limit:
        return para + 2
    # Line break.
    nl = text.rfind("\n", 0, limit + 1)
    if nl > 0:
        return nl
    # Word break.
    sp = text.rfind(" ", 0, limit + 1)
    if sp > 0:
        return sp
    return limit


def _scan_fence_state(text: str, *, open_fence: Optional[str]) -> Optional[str]:
    """Walk *text* line-by-line tracking code-fence open/close state."""
    state = open_fence
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("```"):
            continue
        info = stripped[3:].strip()
        if state is None:
            state = info  # opening fence
        else:
            state = None  # closing fence
    return state
