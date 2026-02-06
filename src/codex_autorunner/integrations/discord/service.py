from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    pass

from ...agents.opencode.supervisor import OpenCodeSupervisor
from ...core.app_server_threads import (
    AppServerThreadRegistry,
    default_app_server_threads_path,
)
from ...core.hub import HubSupervisor
from ...core.locks import process_alive
from ...core.logging_utils import log_event
from ...core.state import now_iso
from ...core.state_roots import resolve_global_state_root
from ...core.utils import build_opencode_supervisor
from ..app_server.supervisor import WorkspaceAppServerSupervisor
from .adapter import DiscordBotClient, allowlist_allows
from .config import (
    DiscordBotConfig,
    DiscordBotLockError,
)
from .constants import (
    TurnKey,
)

# Handler mixins
from .handlers.approvals import DiscordApprovalHandlers
from .handlers.agent_bus import DiscordAgentBusMixin
from .handlers.commands.execution import ExecutionCommands
from .handlers.commands.formatting import FormattingHelpers
from .handlers.commands.scaffold import ScaffoldCommands
from .handlers.commands.shared import SharedHelpers
from .handlers.commands.workspace import WorkspaceCommands
from .handlers.commands_runtime import DiscordCommandHandlers
from .handlers.dashboard import DiscordDashboardMixin
from .handlers.questions import DiscordQuestionHandlers
from .handlers.rbac import DiscordRBACMixin
from .handlers.selections import DiscordSelectionHandlers
from .handlers.tasks import DiscordTasksMixin
from .helpers import (
    ModelOption,
    _discord_lock_path,
    _lock_payload_summary,
    _read_lock_payload,
)
from .notifications import DiscordNotificationHandlers
from .runtime import DiscordRuntimeHelpers
from .state import DiscordStateStore, TopicRouter
from .transport import DiscordMessageTransport
from .types import (
    CompactState,
    ModelPickerState,
    PendingApproval,
    PendingQuestion,
    SelectionState,
    TurnContext,
)

try:
    import discord
except ImportError:
    discord = None  # type: ignore[assignment]


def _build_opencode_supervisor(
    config: DiscordBotConfig,
    *,
    logger: logging.Logger,
) -> Optional[OpenCodeSupervisor]:
    opencode_command = config.opencode_command or None
    opencode_binary = config.agent_binaries.get("opencode")

    supervisor = build_opencode_supervisor(
        opencode_command=opencode_command,
        opencode_binary=opencode_binary,
        workspace_root=config.root,
        logger=logger,
        request_timeout=None,
        max_handles=config.app_server_max_handles,
        idle_ttl_seconds=config.app_server_idle_ttl_seconds,
        base_env=None,
        subagent_models=None,
    )

    if supervisor is None:
        log_event(
            logger,
            logging.INFO,
            "discord.opencode.unavailable",
            reason="command_missing",
        )
        return None

    return supervisor


class DiscordBotService(
    DiscordRuntimeHelpers,
    DiscordMessageTransport,
    DiscordNotificationHandlers,
    DiscordApprovalHandlers,
    DiscordQuestionHandlers,
    DiscordSelectionHandlers,
    DiscordCommandHandlers,
    SharedHelpers,
    ExecutionCommands,
    WorkspaceCommands,
    FormattingHelpers,
    ScaffoldCommands,
    DiscordRBACMixin,
    DiscordAgentBusMixin,
    DiscordDashboardMixin,
    DiscordTasksMixin,
):
    """Discord bot service – gateway-based counterpart of TelegramBotService.

    Uses mixin composition for separation of concerns:
    - DiscordRuntimeHelpers: workspace/client resolution
    - DiscordMessageTransport: message send/edit/delete
    - DiscordNotificationHandlers: lifecycle event delivery
    - DiscordApprovalHandlers: approval request handling
    - DiscordQuestionHandlers: question request handling
    - DiscordSelectionHandlers: paginated selection menus
    - DiscordCommandHandlers: slash command registration
    - SharedHelpers: shared turn execution helpers
    - ExecutionCommands: /run, /stop, /new command implementations
    - WorkspaceCommands: /bind, /status command implementations
    - FormattingHelpers: consistent embed formatting
    - ScaffoldCommands: /setup scaffolding for swarm control surface
    - DiscordRBACMixin: role-based access control helpers
    - DiscordAgentBusMixin: lifecycle/coordination event bus posting
    - DiscordDashboardMixin: pinned dashboard embed maintenance
    - DiscordTasksMixin: forum-backed task cards and triage commands
    """

    def __init__(
        self,
        config: DiscordBotConfig,
        *,
        logger: Optional[logging.Logger] = None,
        hub_root: Optional[Path] = None,
        manifest_path: Optional[Path] = None,
        app_server_auto_restart: Optional[bool] = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._hub_root = hub_root
        self._manifest_path = manifest_path
        self._hub_supervisor: Optional[HubSupervisor] = None
        self._hub_thread_registry: Optional[AppServerThreadRegistry] = None

        if self._hub_root:
            try:
                self._hub_supervisor = HubSupervisor.from_path(self._hub_root)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.pma.hub_supervisor.unavailable",
                    hub_root=str(self._hub_root),
                    exc=exc,
                )
            try:
                self._hub_thread_registry = AppServerThreadRegistry(
                    default_app_server_threads_path(self._hub_root)
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.pma.thread_registry.unavailable",
                    hub_root=str(self._hub_root),
                    exc=exc,
                )

        self._app_server_auto_restart = app_server_auto_restart
        self._allowlist = config.allowlist()
        self._store = DiscordStateStore(
            config.state_file, default_approval_mode=config.defaults.approval_mode
        )
        self._router = TopicRouter(self._store)
        self._app_server_state_root = resolve_global_state_root() / "workspaces"
        self._app_server_supervisor = WorkspaceAppServerSupervisor(
            config.app_server_command,
            state_root=self._app_server_state_root,
            env_builder=self._build_workspace_env,
            approval_handler=self._handle_approval_request,
            notification_handler=self._handle_app_server_notification,
            logger=self._logger,
            auto_restart=self._app_server_auto_restart,
            max_handles=config.app_server_max_handles,
            idle_ttl_seconds=config.app_server_idle_ttl_seconds,
        )
        self._opencode_supervisor = _build_opencode_supervisor(
            config, logger=self._logger
        )

        self._bot = DiscordBotClient(logger=self._logger)

        # Per-turn state maps (same pattern as Telegram)
        self._turn_semaphore: Optional[asyncio.Semaphore] = None
        self._turn_contexts: dict[TurnKey, TurnContext] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._pending_questions: dict[str, PendingQuestion] = {}
        self._model_options: dict[str, ModelPickerState] = {}
        self._model_pending: dict[str, ModelOption] = {}
        self._agent_options: dict[str, SelectionState] = {}
        self._resume_options: dict[str, SelectionState] = {}
        self._bind_options: dict[str, SelectionState] = {}
        self._compact_pending: dict[str, CompactState] = {}
        self._token_usage_by_thread: "collections.OrderedDict[str, dict[str, Any]]" = (
            collections.OrderedDict()
        )
        self._token_usage_by_turn: "collections.OrderedDict[str, dict[str, Any]]" = (
            collections.OrderedDict()
        )
        self._oversize_warnings: set[TurnKey] = set()
        self._outbox_inflight: set[str] = set()
        self._outbox_lock: Optional[asyncio.Lock] = None
        self._spawned_tasks: set[asyncio.Task[Any]] = set()

        # Turn progress tracking (mirrors Telegram pattern)
        from .progress_stream import TurnProgressTracker as _TPT  # noqa: F811

        self._turn_progress_trackers: dict[TurnKey, _TPT] = {}
        self._turn_progress_rendered: dict[TurnKey, str] = {}
        self._turn_progress_updated_at: dict[TurnKey, float] = {}
        self._turn_progress_tasks: dict[TurnKey, asyncio.Task[None]] = {}
        self._turn_progress_heartbeat_tasks: dict[TurnKey, asyncio.Task[None]] = {}
        self._turn_progress_locks: dict[TurnKey, asyncio.Lock] = {}

        # Background task holders
        self._outbox_task: Optional[asyncio.Task[None]] = None
        self._cache_cleanup_task: Optional[asyncio.Task[None]] = None
        self._instance_lock_path: Optional[Path] = None

    # _build_workspace_env is provided by DiscordRuntimeHelpers mixin
    # _handle_approval_request is provided by DiscordApprovalHandlers mixin
    # _handle_app_server_notification is provided by DiscordNotificationHandlers mixin

    # ------------------------------------------------------------------
    # Instance locking
    # ------------------------------------------------------------------

    def _acquire_instance_lock(self) -> None:
        token = self._config.bot_token
        if not token:
            raise DiscordBotLockError("missing discord bot token")
        lock_path = _discord_lock_path(token)
        payload = {
            "pid": os.getpid(),
            "started_at": now_iso(),
            "host": socket.gethostname(),
            "cwd": os.getcwd(),
            "config_root": str(self._config.root),
        }
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing = _read_lock_payload(lock_path)
            pid = existing.get("pid") if isinstance(existing, dict) else None
            if isinstance(pid, int) and process_alive(pid):
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.lock.contended",
                    lock_path=str(lock_path),
                    **_lock_payload_summary(existing),
                )
                raise DiscordBotLockError(
                    "Discord bot already running for this token."
                ) from exc
            try:
                lock_path.unlink()
            except OSError:
                pass
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as exc2:
                existing = _read_lock_payload(lock_path)
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.lock.contended",
                    lock_path=str(lock_path),
                    **_lock_payload_summary(existing),
                )
                raise DiscordBotLockError(
                    "Discord bot already running for this token."
                ) from exc2
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        self._instance_lock_path = lock_path
        log_event(
            self._logger,
            logging.INFO,
            "discord.lock.acquired",
            lock_path=str(lock_path),
            **_lock_payload_summary(payload),
        )

    def _release_instance_lock(self) -> None:
        lock_path = self._instance_lock_path
        if lock_path is None:
            return
        existing = _read_lock_payload(lock_path)
        if isinstance(existing, dict):
            pid = existing.get("pid")
            if isinstance(pid, int) and pid != os.getpid():
                return
        try:
            lock_path.unlink()
        except OSError:
            pass
        self._instance_lock_path = None

    # ------------------------------------------------------------------
    # Turn semaphore
    # ------------------------------------------------------------------

    def _ensure_turn_semaphore(self) -> asyncio.Semaphore:
        if self._turn_semaphore is None:
            self._turn_semaphore = asyncio.Semaphore(
                self._config.concurrency.max_parallel_turns
            )
        return self._turn_semaphore

    # ------------------------------------------------------------------
    # Task spawning
    # ------------------------------------------------------------------

    def _spawn_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._spawned_tasks.add(task)
        task.add_done_callback(self._spawned_tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Outbox lock
    # ------------------------------------------------------------------

    def _ensure_outbox_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._outbox_lock
        lock_loop = getattr(lock, "_loop", None) if lock else None
        if (
            lock is None
            or lock_loop is None
            or lock_loop is not loop
            or lock_loop.is_closed()
        ):
            lock = asyncio.Lock()
            self._outbox_lock = lock
        return lock

    # ------------------------------------------------------------------
    # Presence
    # ------------------------------------------------------------------

    async def _update_presence(self) -> None:
        if discord is None:
            return
        count = len(self._turn_contexts)
        label = f"{count} tasks" if count != 1 else "1 task"
        activity = discord.Activity(type=discord.ActivityType.watching, name=label)
        try:
            await self._bot.bot.change_presence(activity=activity)
        except Exception as exc:
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.presence.change_failed",
                active_turns=count,
                exc=exc,
            )

    # ------------------------------------------------------------------
    # Reconcile / restore (on_ready)
    # ------------------------------------------------------------------

    async def _reconcile_on_ready(self) -> None:
        if discord is None:
            return

        for guild_id in self._config.allowed_guild_ids:
            try:
                await self._reconcile_guild(int(guild_id))
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.reconcile.guild_failed",
                    guild_id=guild_id,
                    exc=exc,
                )

    async def _reconcile_guild(self, guild_id: int) -> None:
        if discord is None:
            return

        await self._reregister_task_card_views(guild_id)

        try:
            rows = await self._store.list_scaffolded_channels(guild_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.reconcile.scaffold_list_failed",
                guild_id=guild_id,
                exc=exc,
            )
            rows = []

        for _gid, workspace_id, channel_type, channel_id, created_at in rows:
            channel = None
            try:
                channel = await self._bot.fetch_channel(channel_id)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.reconcile.channel_recreated",
                    guild_id=guild_id,
                    workspace_id=workspace_id,
                    channel_type=channel_type,
                    channel_id=channel_id,
                    created_at=created_at,
                    exc=exc,
                )
                continue

            if (
                channel_type == "tasks"
                and str(getattr(self._config.scaffold, "tasks_channel_kind", ""))
                .strip()
                .lower()
                == "forum"
                and isinstance(channel, discord.ForumChannel)
            ):
                tags_created: list[dict[str, Any]] = []
                try:
                    await self._ensure_forum_tags(
                        channel,
                        guild_id=guild_id,
                        workspace_id=workspace_id,
                        tags_created=tags_created,
                    )
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.reconcile.tag_ensure_failed",
                        guild_id=guild_id,
                        workspace_id=workspace_id,
                        forum_channel_id=channel_id,
                        exc=exc,
                    )
                else:
                    for entry in tags_created:
                        log_event(
                            self._logger,
                            logging.INFO,
                            "discord.reconcile.tag_recreated",
                            guild_id=guild_id,
                            workspace_id=workspace_id,
                            forum_channel_id=entry.get("forum_channel_id"),
                            tag_name=entry.get("tag_name"),
                            tag_id=entry.get("tag_id"),
                        )

        await self._reconcile_dashboard(guild_id)

    async def _reconcile_dashboard(self, guild_id: int) -> None:
        record = None
        try:
            record = await self._store.get_dashboard(guild_id)
        except Exception:
            record = None

        channel_id: Optional[int] = None
        message_id: Optional[int] = None
        if isinstance(record, tuple) and len(record) >= 2:
            channel_id = record[0] if isinstance(record[0], int) else None
            message_id = record[1] if isinstance(record[1], int) else None
        elif record is not None:
            channel_id = getattr(record, "channel_id", None)
            message_id = getattr(record, "message_id", None)

        if isinstance(channel_id, int) and isinstance(message_id, int):
            try:
                msg = await self._bot.fetch_message(channel_id, message_id)
                if msg is None:
                    raise RuntimeError("dashboard message missing")
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.reconcile.dashboard_recreated",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    exc=exc,
                )

        try:
            await self._ensure_dashboard(guild_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.DEBUG,
                "discord.dashboard.ensure_failed",
                guild_id=guild_id,
                exc=exc,
            )

    async def _reregister_task_card_views(self, guild_id: int) -> None:
        if discord is None:
            return

        try:
            from .handlers import tasks as _tasks  # type: ignore

            view_cls = getattr(_tasks, "TaskCardView", None)
        except Exception:
            view_cls = None

        if view_cls is None:
            return

        try:
            tasks = await self._store.list_tasks(guild_id)
        except Exception:
            tasks = []

        if not hasattr(self, "_task_views_registered"):
            self._task_views_registered: set[tuple[int, int]] = set()

        for task in tasks:
            thread_id = task.get("thread_id") if isinstance(task, dict) else None
            if not isinstance(thread_id, int) or isinstance(thread_id, bool):
                continue
            key = (guild_id, thread_id)
            if key in self._task_views_registered:
                continue
            try:
                self._bot.bot.add_view(view_cls(guild_id=guild_id, thread_id=thread_id))
                self._task_views_registered.add(key)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "discord.task.view_register_failed",
                    guild_id=guild_id,
                    thread_id=thread_id,
                    exc=exc,
                )

    # ------------------------------------------------------------------
    # Gateway lifecycle
    # ------------------------------------------------------------------

    async def run_gateway(self) -> None:
        """Connect to the Discord gateway and begin processing events."""
        self._config.validate()
        self._acquire_instance_lock()
        self._turn_semaphore = asyncio.Semaphore(
            self._config.concurrency.max_parallel_turns
        )

        bot = self._bot.bot

        # Register slash commands on the command tree
        self._register_slash_commands()

        @bot.event
        async def on_ready() -> None:
            self._bot.set_ready()
            log_event(
                self._logger,
                logging.INFO,
                "discord.bot.ready",
                user=str(self._bot.user),
                guild_count=len(self._bot.guilds),
                guilds=[g.name for g in self._bot.guilds],
            )
            # Restore pending approvals from previous session
            await self._restore_pending_approvals()
            # Reconcile control surface (scaffolded channels/tags, dashboard, task views)
            try:
                await self._reconcile_on_ready()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.reconcile.failed",
                    exc=exc,
                )

            # Register lifecycle listener for agent bus (hub lifecycle events)
            try:
                await self._register_lifecycle_listener()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.agent_bus.listener_register_failed",
                    exc=exc,
                )

            # Update bot presence based on active turns
            try:
                await self._update_presence()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "discord.presence.update_failed",
                    exc=exc,
                )
            # Sync slash commands to all allowed guilds
            for guild_id in self._config.allowed_guild_ids:
                try:
                    synced = await self._bot.sync_commands(guild_id)
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.commands.synced",
                        guild_id=guild_id,
                        command_count=len(synced),
                    )
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "discord.commands.sync_failed",
                        guild_id=guild_id,
                        exc=exc,
                    )

        @bot.event
        async def on_message(message: Any) -> None:
            if message.author == bot.user:
                return
            if not allowlist_allows(message, self._allowlist):
                return
            from .handlers.messages import handle_message

            await handle_message(self, message)

        @bot.event
        async def on_interaction(interaction: Any) -> None:
            if not allowlist_allows(interaction, self._allowlist):
                try:
                    await interaction.response.send_message(
                        "You do not have permission to use this bot.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return
            from .dispatch import dispatch_interaction

            await dispatch_interaction(self, interaction)

        try:
            log_event(
                self._logger,
                logging.INFO,
                "discord.bot.starting",
                allowed_guilds=len(self._config.allowed_guild_ids),
                allowed_channels=len(self._config.allowed_channel_ids),
                allowed_roles=len(self._config.allowed_role_ids),
                allowed_users=len(self._config.allowed_user_ids),
                trigger_mode=self._config.trigger_mode,
                max_parallel_turns=self._config.concurrency.max_parallel_turns,
            )
            await self._bot.start(self._config.bot_token or "")
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Clean up resources on shutdown."""
        for task in list(self._spawned_tasks):
            task.cancel()
        if self._outbox_task is not None:
            self._outbox_task.cancel()
        if self._cache_cleanup_task is not None:
            self._cache_cleanup_task.cancel()

        # Wait for tasks to finish
        pending = [t for t in self._spawned_tasks if not t.done()]
        if self._outbox_task and not self._outbox_task.done():
            pending.append(self._outbox_task)
        if self._cache_cleanup_task and not self._cache_cleanup_task.done():
            pending.append(self._cache_cleanup_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            await self._bot.close()
        except Exception:
            pass
        self._release_instance_lock()
        log_event(
            self._logger,
            logging.INFO,
            "discord.bot.stopped",
        )
