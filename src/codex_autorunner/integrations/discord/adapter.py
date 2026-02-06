from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .config import DiscordAllowlist
from .constants import (
    DISCORD_SELECT_OPTIONS_LIMIT,
)

try:
    import discord
    from discord import app_commands
    from discord.ext import commands

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

logger = logging.getLogger("codex_autorunner.integrations.discord.adapter")


class DiscordDependencyError(Exception):
    """Raised when discord.py is not installed."""


def _require_discord() -> None:
    if not HAS_DISCORD:
        raise DiscordDependencyError(
            "discord.py is required for Discord bot support. "
            "Install with: pip install 'codex-autorunner[discord]'"
        )


def _build_intents() -> "discord.Intents":
    """Build the required intents for the bot."""
    _require_discord()
    intents = discord.Intents.default()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    intents.members = True
    return intents


class DiscordBotClient:
    """Wraps discord.py Bot with lifecycle and command tree management."""

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        _require_discord()
        self._logger = logger or logging.getLogger(__name__)
        self._bot = commands.Bot(
            command_prefix=commands.when_mentioned,
            intents=_build_intents(),
            help_command=None,
        )
        self._ready_event = asyncio.Event()

    @property
    def bot(self) -> "commands.Bot":
        return self._bot

    @property
    def tree(self) -> "app_commands.CommandTree":
        return self._bot.tree

    @property
    def user(self) -> Optional[Any]:
        return self._bot.user

    @property
    def guilds(self) -> list[Any]:
        return list(self._bot.guilds)

    @property
    def is_ready(self) -> bool:
        return self._ready_event.is_set()

    async def wait_until_ready(self) -> None:
        await self._ready_event.wait()

    def set_ready(self) -> None:
        self._ready_event.set()

    async def start(self, token: str) -> None:
        """Connect to the Discord gateway."""
        await self._bot.start(token)

    async def close(self) -> None:
        """Gracefully disconnect from the gateway."""
        if not self._bot.is_closed():
            await self._bot.close()

    async def sync_commands(self, guild_id: Optional[int] = None) -> list[Any]:
        """Sync the command tree. If guild_id is given, sync to that guild only."""
        if guild_id is not None:
            guild = discord.Object(id=guild_id)
            self._bot.tree.copy_global_to(guild=guild)
            return await self._bot.tree.sync(guild=guild)
        return await self._bot.tree.sync()

    def get_channel(self, channel_id: int) -> Optional[Any]:
        return self._bot.get_channel(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        return await self._bot.fetch_channel(channel_id)

    async def fetch_message(self, channel_id: int, message_id: int) -> Optional[Any]:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if hasattr(channel, "fetch_message"):
            return await channel.fetch_message(message_id)
        return None


def allowlist_allows(
    interaction_or_message: Any,
    allowlist: DiscordAllowlist,
) -> bool:
    """Check whether an interaction or message passes the allowlist filters.

    If a filter set is empty, that dimension is unrestricted.
    For threads, the parent channel is checked against allowed_channel_ids.
    """
    _require_discord()

    guild_id: Optional[int] = None
    channel_id: Optional[int] = None
    user_id: Optional[int] = None
    member_role_ids: set[int] = set()

    if isinstance(interaction_or_message, discord.Interaction):
        interaction = interaction_or_message
        guild_id = interaction.guild_id
        channel = interaction.channel
        if channel is not None:
            if isinstance(channel, discord.Thread):
                channel_id = channel.parent_id
            else:
                channel_id = channel.id
        if interaction.user:
            user_id = interaction.user.id
            if hasattr(interaction.user, "roles"):
                member_role_ids = {r.id for r in interaction.user.roles}
    elif isinstance(interaction_or_message, discord.Message):
        message = interaction_or_message
        if message.guild:
            guild_id = message.guild.id
        channel = message.channel
        if isinstance(channel, discord.Thread):
            channel_id = channel.parent_id
        else:
            channel_id = getattr(channel, "id", None)
        if message.author:
            user_id = message.author.id
            if hasattr(message.author, "roles"):
                member_role_ids = {r.id for r in message.author.roles}
    else:
        return False

    if allowlist.allowed_guild_ids and guild_id not in allowlist.allowed_guild_ids:
        return False

    if (
        allowlist.allowed_channel_ids
        and channel_id not in allowlist.allowed_channel_ids
    ):
        return False

    if allowlist.allowed_user_ids and user_id not in allowlist.allowed_user_ids:
        return False

    if allowlist.allowed_role_ids and not member_role_ids.intersection(
        allowlist.allowed_role_ids
    ):
        return False

    return True


# ---------------------------------------------------------------------------
# Component builders
# ---------------------------------------------------------------------------


def build_approval_view(request_id: str) -> "discord.ui.View":
    """Build a persistent view with approval buttons.

    Buttons encode the decision in their custom_id:
        approval:<request_id>:<decision>

    Decisions: accept, accept_session, decline, cancel
    """
    _require_discord()

    view = discord.ui.View(timeout=None)

    accept_btn = discord.ui.Button(
        style=discord.ButtonStyle.success,
        label="Accept",
        custom_id=f"approval:{request_id}:accept",
    )
    accept_session_btn = discord.ui.Button(
        style=discord.ButtonStyle.primary,
        label="Accept Session",
        custom_id=f"approval:{request_id}:accept_session",
    )
    decline_btn = discord.ui.Button(
        style=discord.ButtonStyle.danger,
        label="Decline",
        custom_id=f"approval:{request_id}:decline",
    )
    cancel_btn = discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="Cancel Turn",
        custom_id=f"approval:{request_id}:cancel",
    )

    view.add_item(accept_btn)
    view.add_item(accept_session_btn)
    view.add_item(decline_btn)
    view.add_item(cancel_btn)
    return view


def build_question_view(
    request_id: str,
    options: list[str],
    *,
    multiple: bool = False,
) -> "discord.ui.View":
    """Build a view with a Select menu for question options, plus an Other button."""
    _require_discord()

    view = discord.ui.View(timeout=None)

    if options:
        select_options = [
            discord.SelectOption(label=opt[:100], value=str(i))
            for i, opt in enumerate(options[:DISCORD_SELECT_OPTIONS_LIMIT])
        ]
        select = discord.ui.Select(
            custom_id=f"question:{request_id}:select",
            placeholder="Choose an option..." if not multiple else "Choose options...",
            options=select_options,
            min_values=1,
            max_values=len(select_options) if multiple else 1,
        )
        view.add_item(select)

    other_btn = discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="Other",
        custom_id=f"question:{request_id}:other",
    )
    done_btn = discord.ui.Button(
        style=discord.ButtonStyle.success,
        label="Done",
        custom_id=f"question:{request_id}:done",
    )
    cancel_btn = discord.ui.Button(
        style=discord.ButtonStyle.danger,
        label="Cancel",
        custom_id=f"question:{request_id}:cancel",
    )

    view.add_item(other_btn)
    if multiple:
        view.add_item(done_btn)
    view.add_item(cancel_btn)
    return view


def build_selection_view(
    items: list[tuple[str, str]],
    page: int = 0,
    page_size: int = 10,
    *,
    kind: str = "selection",
    button_labels: Optional[dict[str, str]] = None,
) -> tuple["discord.ui.View", list[tuple[str, str]]]:
    """Build a paginated selection view.

    Returns (view, page_items) where page_items are the items on the current page.
    """
    _require_discord()

    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    end = min(start + page_size, len(items))
    page_items = items[start:end]

    view = discord.ui.View(timeout=None)

    if page_items:
        select_options = []
        for item_id, label in page_items:
            display = (
                label[:100]
                if button_labels is None
                else (button_labels.get(item_id, label)[:100])
            )
            select_options.append(discord.SelectOption(label=display, value=item_id))
        select = discord.ui.Select(
            custom_id=f"{kind}:select:{page}",
            placeholder="Choose an option...",
            options=select_options,
        )
        view.add_item(select)

    if total_pages > 1:
        prev_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Previous",
            custom_id=f"{kind}:page:{max(0, page - 1)}",
            disabled=(page == 0),
        )
        page_indicator = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=f"{page + 1}/{total_pages}",
            custom_id=f"{kind}:page_indicator",
            disabled=True,
        )
        next_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Next",
            custom_id=f"{kind}:page:{min(total_pages - 1, page + 1)}",
            disabled=(page >= total_pages - 1),
        )
        view.add_item(prev_btn)
        view.add_item(page_indicator)
        view.add_item(next_btn)

    cancel_btn = discord.ui.Button(
        style=discord.ButtonStyle.danger,
        label="Cancel",
        custom_id=f"{kind}:cancel",
    )
    view.add_item(cancel_btn)

    return view, page_items


def build_custom_input_modal(
    request_id: str,
    *,
    title: str = "Custom Input",
    label: str = "Your response",
    placeholder: str = "Type your response here...",
) -> "discord.ui.Modal":
    """Build a modal for free-text input (used by question 'Other' button)."""
    _require_discord()

    class CustomInputModal(discord.ui.Modal, title=title):
        response = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
            custom_id=f"question:{request_id}:modal_input",
        )

    return CustomInputModal(custom_id=f"question:{request_id}:modal")
