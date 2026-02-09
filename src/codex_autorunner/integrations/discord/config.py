from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .constants import (
    CACHE_CLEANUP_INTERVAL_SECONDS,
    COALESCE_BUFFER_TTL_SECONDS,
    DEFAULT_AGENT_TURN_TIMEOUT_SECONDS,
    DEFAULT_APP_SERVER_COMMAND,
    DEFAULT_APP_SERVER_IDLE_TTL_SECONDS,
    DEFAULT_APP_SERVER_MAX_HANDLES,
    DEFAULT_APP_SERVER_START_TIMEOUT_SECONDS,
    DEFAULT_APP_SERVER_TURN_TIMEOUT_SECONDS,
    DEFAULT_COALESCE_WINDOW_SECONDS,
    DEFAULT_MESSAGE_OVERFLOW,
    DEFAULT_METRICS_MODE,
    DEFAULT_PROGRESS_STREAM_ENABLED,
    DEFAULT_PROGRESS_STREAM_MAX_ACTIONS,
    DEFAULT_PROGRESS_STREAM_MAX_OUTPUT_CHARS,
    DEFAULT_PROGRESS_STREAM_MIN_EDIT_INTERVAL_SECONDS,
    DEFAULT_STATE_FILE,
    DEFAULT_TRIGGER_MODE,
    MEDIA_BATCH_BUFFER_TTL_SECONDS,
    MESSAGE_OVERFLOW_OPTIONS,
    METRICS_MODE_OPTIONS,
    MODEL_PENDING_TTL_SECONDS,
    OVERSIZE_WARNING_TTL_SECONDS,
    PENDING_APPROVAL_TTL_SECONDS,
    PENDING_QUESTION_TTL_SECONDS,
    PROGRESS_STREAM_TTL_SECONDS,
    REASONING_BUFFER_TTL_SECONDS,
    SELECTION_STATE_TTL_SECONDS,
    TRIGGER_MODE_OPTIONS,
    TURN_PREVIEW_TTL_SECONDS,
    UPDATE_ID_PERSIST_INTERVAL_SECONDS,
)
from .state import APPROVAL_MODE_SAFE, APPROVAL_MODE_YOLO, normalize_approval_mode

DEFAULT_SAFE_APPROVAL_POLICY = "on-request"
DEFAULT_YOLO_APPROVAL_POLICY = "never"
DEFAULT_YOLO_SANDBOX_POLICY = "dangerFullAccess"
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300.0
DEFAULT_MEDIA_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MEDIA_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_MEDIA_IMAGE_PROMPT = (
    "The user sent an image with no caption. Use it to continue the "
    "conversation; if no clear task, describe the image and ask what they want."
)
DEFAULT_MEDIA_BATCH_UPLOADS = True
DEFAULT_MEDIA_BATCH_WINDOW_SECONDS = 1.0
DEFAULT_SHELL_TIMEOUT_MS = 120_000
DEFAULT_SHELL_MAX_OUTPUT_CHARS = 3800
DEFAULT_PAUSE_DISPATCH_MAX_FILE_BYTES = 50 * 1024 * 1024


class DiscordBotConfigError(Exception):
    """Raised when discord bot config is invalid."""


class DiscordBotLockError(Exception):
    """Raised when another discord bot instance already holds the lock."""


class AppServerUnavailableError(Exception):
    """Raised when the app-server is unavailable after timeout."""


@dataclass(frozen=True)
class DiscordBotDefaults:
    approval_mode: str
    approval_policy: Optional[str]
    sandbox_policy: Optional[str]
    yolo_approval_policy: str
    yolo_sandbox_policy: str

    def policies_for_mode(self, mode: str) -> tuple[Optional[str], Optional[str]]:
        normalized = normalize_approval_mode(mode, default=APPROVAL_MODE_YOLO)
        if normalized == APPROVAL_MODE_YOLO:
            return self.yolo_approval_policy, self.yolo_sandbox_policy
        return self.approval_policy, self.sandbox_policy


@dataclass(frozen=True)
class DiscordBotConcurrency:
    max_parallel_turns: int
    per_topic_queue: bool


@dataclass(frozen=True)
class DiscordBotMediaConfig:
    enabled: bool
    images: bool
    files: bool
    max_image_bytes: int
    max_file_bytes: int
    image_prompt: str
    batch_uploads: bool
    batch_window_seconds: float


@dataclass(frozen=True)
class DiscordBotShellConfig:
    enabled: bool
    timeout_ms: int
    max_output_chars: int


@dataclass(frozen=True)
class DiscordBotCacheConfig:
    cleanup_interval_seconds: float
    coalesce_buffer_ttl_seconds: float
    media_batch_buffer_ttl_seconds: float
    model_pending_ttl_seconds: float
    pending_approval_ttl_seconds: float
    pending_question_ttl_seconds: float
    reasoning_buffer_ttl_seconds: float
    selection_state_ttl_seconds: float
    turn_preview_ttl_seconds: float
    progress_stream_ttl_seconds: float
    oversize_warning_ttl_seconds: float
    update_id_persist_interval_seconds: float


@dataclass(frozen=True)
class DiscordScaffoldConfig:
    enabled: bool = False
    auto_bind: bool = True
    category_prefix: str = ""
    tasks_channel_kind: str = "forum"
    tasks_channel_name: str = "tasks"
    task_forum_tags: tuple[str, ...] = (
        "queued",
        "running",
        "needs-approval",
        "blocked",
        "done",
        "failed",
        "timeout",
        "stopped",
        "swarm",
        "p0",
        "p1",
        "p2",
    )
    activity_channel_name: str = "activity-feed"
    approval_channel_name: str = "approvals"
    run_channel_name: str = "run"
    dashboard_channel_name: str = "dashboard"
    agent_bus_channel_name: str = "agent-bus"
    notifications_channel_name: str = "notifications"


@dataclass(frozen=True)
class DiscordRoleTier:
    name: str
    role_ids: tuple[int, ...]
    approval_mode: str = "safe"
    can_setup: bool = False
    can_run: bool = False
    can_stop: bool = False
    can_bind: bool = False
    read_only: bool = False


@dataclass(frozen=True)
class DiscordRBACConfig:
    enabled: bool = False
    use_default_member_permissions: bool = True
    tiers: tuple[DiscordRoleTier, ...] = ()
    default_tier: str = "viewer"


@dataclass(frozen=True)
class SwarmConfig:
    enabled: bool = True
    max_agents: int = 6
    default_lead_model: str = "claude-opus-4-6"
    default_worker_model: str = "claude-sonnet-4-5-20250929"
    claude_binary: str = "claude"
    poll_interval_seconds: float = 0.5
    agent_timeout_seconds: float = 3600.0
    swarm_timeout_seconds: float = 7200.0
    health_check_interval_seconds: float = 5.0
    shutdown_grace_seconds: float = 10.0
    custom_presets: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscordAlertConfig:
    enabled: bool = True
    alert_role_id: Optional[int] = None
    per_task_cooldown_seconds: int = 900
    global_cooldown_seconds: int = 10


@dataclass(frozen=True)
class DiscordBotProgressStreamConfig:
    enabled: bool
    max_actions: int
    max_output_chars: int
    min_edit_interval_seconds: float


@dataclass(frozen=True)
class PauseDispatchNotifications:
    enabled: bool
    send_attachments: bool
    max_file_size_bytes: int
    chunk_long_messages: bool


@dataclass(frozen=True)
class DiscordAllowlist:
    allowed_guild_ids: set[int]
    allowed_channel_ids: set[int]
    allowed_role_ids: set[int]
    allowed_user_ids: set[int]


def _parse_int_list(value: Any) -> list[int]:
    """Parse a value into a list of ints. Accepts list, csv string, or single int."""
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, (list, tuple)):
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result
    if isinstance(value, str):
        result = []
        for part in value.split(","):
            part = part.strip()
            if part:
                try:
                    result.append(int(part))
                except ValueError:
                    continue
        return result
    return []


def _parse_command(value: Any) -> list[str]:
    """Parse a command value into a list of strings."""
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str) and value.strip():
        return shlex.split(value)
    return []


@dataclass(frozen=True)
class DiscordBotConfig:
    root: Path
    enabled: bool
    bot_token_env: str
    bot_token: Optional[str]
    allowed_guild_ids: set[int]
    allowed_channel_ids: set[int]
    allowed_role_ids: set[int]
    allowed_user_ids: set[int]
    trigger_mode: str
    defaults: DiscordBotDefaults
    concurrency: DiscordBotConcurrency
    media: DiscordBotMediaConfig
    shell: DiscordBotShellConfig
    cache: DiscordBotCacheConfig
    scaffold: DiscordScaffoldConfig
    rbac: DiscordRBACConfig
    alerts: DiscordAlertConfig
    progress_stream: DiscordBotProgressStreamConfig
    state_file: Path
    app_server_command_env: str
    app_server_command: list[str]
    app_server_max_handles: Optional[int]
    app_server_idle_ttl_seconds: Optional[int]
    app_server_start_timeout_seconds: float
    app_server_start_max_attempts: Optional[int]
    app_server_turn_timeout_seconds: Optional[float]
    agent_turn_timeout_seconds: dict[str, Optional[float]]
    message_overflow: str
    metrics_mode: str
    coalesce_window_seconds: float
    agent_binaries: dict[str, str]
    opencode_command: list[str]
    ticket_flow_auto_resume: bool
    pause_dispatch_notifications: PauseDispatchNotifications
    default_notification_channel_id: Optional[int]
    swarm: SwarmConfig = field(default_factory=SwarmConfig)
    dashboard_channel_id: Optional[int] = None
    agent_bus_channel_id: Optional[int] = None

    @classmethod
    def from_raw(
        cls,
        raw: Optional[dict[str, Any]],
        *,
        root: Path,
        agent_binaries: Optional[dict[str, str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> "DiscordBotConfig":
        env = env or dict(os.environ)
        cfg: dict[str, Any] = raw if isinstance(raw, dict) else {}

        def _positive_float(value: Any, default: float) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return default
            if parsed <= 0:
                return default
            return parsed

        enabled = bool(cfg.get("enabled", False))
        bot_token_env = str(cfg.get("bot_token_env", "CAR_DISCORD_BOT_TOKEN"))
        bot_token = env.get(bot_token_env)

        allowed_guild_ids = set(_parse_int_list(cfg.get("allowed_guild_ids")))
        allowed_guild_ids.update(_parse_int_list(env.get("CAR_DISCORD_GUILD_IDS")))
        allowed_channel_ids = set(_parse_int_list(cfg.get("allowed_channel_ids")))
        allowed_channel_ids.update(_parse_int_list(env.get("CAR_DISCORD_CHANNEL_IDS")))
        allowed_role_ids = set(_parse_int_list(cfg.get("allowed_role_ids")))
        allowed_user_ids = set(_parse_int_list(cfg.get("allowed_user_ids")))

        trigger_mode = (
            str(cfg.get("trigger_mode", DEFAULT_TRIGGER_MODE)).strip().lower()
        )
        if trigger_mode not in TRIGGER_MODE_OPTIONS:
            trigger_mode = DEFAULT_TRIGGER_MODE

        # Defaults
        defaults_raw_value = cfg.get("defaults")
        defaults_raw: dict[str, Any] = (
            defaults_raw_value if isinstance(defaults_raw_value, dict) else {}
        )
        approval_mode = normalize_approval_mode(
            defaults_raw.get("approval_mode"), default=APPROVAL_MODE_YOLO
        )
        approval_policy = defaults_raw.get(
            "approval_policy", DEFAULT_SAFE_APPROVAL_POLICY
        )
        sandbox_policy = defaults_raw.get("sandbox_policy")
        if sandbox_policy is not None:
            sandbox_policy = str(sandbox_policy)
        yolo_approval_policy = str(
            defaults_raw.get("yolo_approval_policy", DEFAULT_YOLO_APPROVAL_POLICY)
        )
        yolo_sandbox_policy = str(
            defaults_raw.get("yolo_sandbox_policy", DEFAULT_YOLO_SANDBOX_POLICY)
        )
        defaults = DiscordBotDefaults(
            approval_mode=approval_mode,
            approval_policy=(
                str(approval_policy) if approval_policy is not None else None
            ),
            sandbox_policy=sandbox_policy,
            yolo_approval_policy=yolo_approval_policy,
            yolo_sandbox_policy=yolo_sandbox_policy,
        )

        # Concurrency
        concurrency_raw_value = cfg.get("concurrency")
        concurrency_raw: dict[str, Any] = (
            concurrency_raw_value if isinstance(concurrency_raw_value, dict) else {}
        )
        max_parallel_turns = int(concurrency_raw.get("max_parallel_turns", 4))
        if max_parallel_turns <= 0:
            max_parallel_turns = 1
        per_topic_queue = bool(concurrency_raw.get("per_topic_queue", True))
        concurrency = DiscordBotConcurrency(
            max_parallel_turns=max_parallel_turns,
            per_topic_queue=per_topic_queue,
        )

        # Media
        media_raw_value = cfg.get("media")
        media_raw: dict[str, Any] = (
            media_raw_value if isinstance(media_raw_value, dict) else {}
        )
        media_enabled = bool(media_raw.get("enabled", True))
        media_images = bool(media_raw.get("images", True))
        media_files = bool(media_raw.get("files", True))
        max_image_bytes = int(
            media_raw.get("max_image_bytes", DEFAULT_MEDIA_MAX_IMAGE_BYTES)
        )
        if max_image_bytes <= 0:
            max_image_bytes = DEFAULT_MEDIA_MAX_IMAGE_BYTES
        max_file_bytes = int(
            media_raw.get("max_file_bytes", DEFAULT_MEDIA_MAX_FILE_BYTES)
        )
        if max_file_bytes <= 0:
            max_file_bytes = DEFAULT_MEDIA_MAX_FILE_BYTES
        image_prompt = str(
            media_raw.get("image_prompt", DEFAULT_MEDIA_IMAGE_PROMPT)
        ).strip()
        if not image_prompt:
            image_prompt = DEFAULT_MEDIA_IMAGE_PROMPT
        media_batch_uploads = bool(
            media_raw.get("batch_uploads", DEFAULT_MEDIA_BATCH_UPLOADS)
        )
        media_batch_window_seconds = float(
            media_raw.get("batch_window_seconds", DEFAULT_MEDIA_BATCH_WINDOW_SECONDS)
        )
        if media_batch_window_seconds <= 0:
            media_batch_window_seconds = DEFAULT_MEDIA_BATCH_WINDOW_SECONDS
        media = DiscordBotMediaConfig(
            enabled=media_enabled,
            images=media_images,
            files=media_files,
            max_image_bytes=max_image_bytes,
            max_file_bytes=max_file_bytes,
            image_prompt=image_prompt,
            batch_uploads=media_batch_uploads,
            batch_window_seconds=media_batch_window_seconds,
        )

        # Shell
        shell_raw_value = cfg.get("shell")
        shell_raw: dict[str, Any] = (
            shell_raw_value if isinstance(shell_raw_value, dict) else {}
        )
        shell_enabled = bool(shell_raw.get("enabled", False))
        shell_timeout_ms = int(shell_raw.get("timeout_ms", DEFAULT_SHELL_TIMEOUT_MS))
        if shell_timeout_ms <= 0:
            shell_timeout_ms = DEFAULT_SHELL_TIMEOUT_MS
        shell_max_output_chars = int(
            shell_raw.get("max_output_chars", DEFAULT_SHELL_MAX_OUTPUT_CHARS)
        )
        if shell_max_output_chars <= 0:
            shell_max_output_chars = DEFAULT_SHELL_MAX_OUTPUT_CHARS
        shell = DiscordBotShellConfig(
            enabled=shell_enabled,
            timeout_ms=shell_timeout_ms,
            max_output_chars=shell_max_output_chars,
        )

        # Cache
        cache_raw_value = cfg.get("cache")
        cache_raw: dict[str, Any] = (
            cache_raw_value if isinstance(cache_raw_value, dict) else {}
        )
        cache = DiscordBotCacheConfig(
            cleanup_interval_seconds=_positive_float(
                cache_raw.get("cleanup_interval_seconds"),
                CACHE_CLEANUP_INTERVAL_SECONDS,
            ),
            coalesce_buffer_ttl_seconds=_positive_float(
                cache_raw.get("coalesce_buffer_ttl_seconds"),
                COALESCE_BUFFER_TTL_SECONDS,
            ),
            media_batch_buffer_ttl_seconds=_positive_float(
                cache_raw.get("media_batch_buffer_ttl_seconds"),
                MEDIA_BATCH_BUFFER_TTL_SECONDS,
            ),
            model_pending_ttl_seconds=_positive_float(
                cache_raw.get("model_pending_ttl_seconds"),
                MODEL_PENDING_TTL_SECONDS,
            ),
            pending_approval_ttl_seconds=_positive_float(
                cache_raw.get("pending_approval_ttl_seconds"),
                PENDING_APPROVAL_TTL_SECONDS,
            ),
            pending_question_ttl_seconds=_positive_float(
                cache_raw.get("pending_question_ttl_seconds"),
                PENDING_QUESTION_TTL_SECONDS,
            ),
            reasoning_buffer_ttl_seconds=_positive_float(
                cache_raw.get("reasoning_buffer_ttl_seconds"),
                REASONING_BUFFER_TTL_SECONDS,
            ),
            selection_state_ttl_seconds=_positive_float(
                cache_raw.get("selection_state_ttl_seconds"),
                SELECTION_STATE_TTL_SECONDS,
            ),
            turn_preview_ttl_seconds=_positive_float(
                cache_raw.get("turn_preview_ttl_seconds"),
                TURN_PREVIEW_TTL_SECONDS,
            ),
            progress_stream_ttl_seconds=_positive_float(
                cache_raw.get("progress_stream_ttl_seconds"),
                PROGRESS_STREAM_TTL_SECONDS,
            ),
            oversize_warning_ttl_seconds=_positive_float(
                cache_raw.get("oversize_warning_ttl_seconds"),
                OVERSIZE_WARNING_TTL_SECONDS,
            ),
            update_id_persist_interval_seconds=_positive_float(
                cache_raw.get("update_id_persist_interval_seconds"),
                UPDATE_ID_PERSIST_INTERVAL_SECONDS,
            ),
        )

        # Scaffold
        scaffold_raw_value = cfg.get("scaffold")
        scaffold_raw: dict[str, Any] = (
            scaffold_raw_value if isinstance(scaffold_raw_value, dict) else {}
        )
        scaffold_enabled = bool(scaffold_raw.get("enabled", False))
        scaffold_auto_bind = bool(scaffold_raw.get("auto_bind", True))
        scaffold_category_prefix = str(scaffold_raw.get("category_prefix", "")).strip()

        scaffold_tasks_channel_kind = str(
            scaffold_raw.get("tasks_channel_kind", "forum")
        ).strip()
        if not scaffold_tasks_channel_kind:
            scaffold_tasks_channel_kind = "forum"

        scaffold_tasks_channel_name = str(
            scaffold_raw.get("tasks_channel_name", "tasks")
        ).strip()
        if not scaffold_tasks_channel_name:
            scaffold_tasks_channel_name = "tasks"

        scaffold_task_forum_tags = DiscordScaffoldConfig.task_forum_tags
        scaffold_task_forum_tags_value = scaffold_raw.get("task_forum_tags")
        if isinstance(scaffold_task_forum_tags_value, (list, tuple)):
            tags: list[str] = []
            for item in scaffold_task_forum_tags_value:
                tag = str(item).strip()
                if tag:
                    tags.append(tag)
            if tags:
                scaffold_task_forum_tags = tuple(tags)
        elif isinstance(scaffold_task_forum_tags_value, str):
            tags = []
            for part in scaffold_task_forum_tags_value.split(","):
                part = part.strip()
                if part:
                    tags.append(part)
            if tags:
                scaffold_task_forum_tags = tuple(tags)

        scaffold_activity_channel_name = str(
            scaffold_raw.get("activity_channel_name", "activity-feed")
        ).strip()
        if not scaffold_activity_channel_name:
            scaffold_activity_channel_name = "activity-feed"

        scaffold_approval_channel_name = str(
            scaffold_raw.get("approval_channel_name", "approvals")
        ).strip()
        if not scaffold_approval_channel_name:
            scaffold_approval_channel_name = "approvals"

        scaffold_run_channel_name = str(
            scaffold_raw.get("run_channel_name", "run")
        ).strip()
        if not scaffold_run_channel_name:
            scaffold_run_channel_name = "run"

        scaffold_dashboard_channel_name = str(
            scaffold_raw.get("dashboard_channel_name", "dashboard")
        ).strip()
        if not scaffold_dashboard_channel_name:
            scaffold_dashboard_channel_name = "dashboard"

        scaffold_agent_bus_channel_name = str(
            scaffold_raw.get("agent_bus_channel_name", "agent-bus")
        ).strip()
        if not scaffold_agent_bus_channel_name:
            scaffold_agent_bus_channel_name = "agent-bus"

        scaffold_notifications_channel_name = str(
            scaffold_raw.get("notifications_channel_name", "notifications")
        ).strip()
        if not scaffold_notifications_channel_name:
            scaffold_notifications_channel_name = "notifications"

        scaffold = DiscordScaffoldConfig(
            enabled=scaffold_enabled,
            auto_bind=scaffold_auto_bind,
            category_prefix=scaffold_category_prefix,
            tasks_channel_kind=scaffold_tasks_channel_kind,
            tasks_channel_name=scaffold_tasks_channel_name,
            task_forum_tags=scaffold_task_forum_tags,
            activity_channel_name=scaffold_activity_channel_name,
            approval_channel_name=scaffold_approval_channel_name,
            run_channel_name=scaffold_run_channel_name,
            dashboard_channel_name=scaffold_dashboard_channel_name,
            agent_bus_channel_name=scaffold_agent_bus_channel_name,
            notifications_channel_name=scaffold_notifications_channel_name,
        )

        # RBAC
        rbac_raw_value = cfg.get("rbac")
        rbac_raw: dict[str, Any] = (
            rbac_raw_value if isinstance(rbac_raw_value, dict) else {}
        )
        rbac_enabled = bool(rbac_raw.get("enabled", False))
        rbac_use_default_member_permissions = bool(
            rbac_raw.get("use_default_member_permissions", True)
        )
        rbac_tiers_value = rbac_raw.get("tiers")
        rbac_tiers: list[DiscordRoleTier] = []
        if isinstance(rbac_tiers_value, (list, tuple)):
            for item in rbac_tiers_value:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                role_ids = tuple(_parse_int_list(item.get("role_ids")))
                approval_mode = normalize_approval_mode(
                    item.get("approval_mode"), default=APPROVAL_MODE_SAFE
                )
                rbac_tiers.append(
                    DiscordRoleTier(
                        name=name,
                        role_ids=role_ids,
                        approval_mode=approval_mode,
                        can_setup=bool(item.get("can_setup", False)),
                        can_run=bool(item.get("can_run", False)),
                        can_stop=bool(item.get("can_stop", False)),
                        can_bind=bool(item.get("can_bind", False)),
                        read_only=bool(item.get("read_only", False)),
                    )
                )
        rbac_default_tier = str(rbac_raw.get("default_tier", "viewer")).strip()
        if not rbac_default_tier:
            rbac_default_tier = "viewer"
        rbac = DiscordRBACConfig(
            enabled=rbac_enabled,
            use_default_member_permissions=rbac_use_default_member_permissions,
            tiers=tuple(rbac_tiers),
            default_tier=rbac_default_tier,
        )

        # Alerts
        alerts_raw_value = cfg.get("alerts")
        alerts_raw: dict[str, Any] = (
            alerts_raw_value if isinstance(alerts_raw_value, dict) else {}
        )
        alerts_enabled = bool(alerts_raw.get("enabled", True))
        alert_role_id_raw = alerts_raw.get("alert_role_id")
        alert_role_id: Optional[int] = None
        if alert_role_id_raw is not None:
            try:
                alert_role_id = int(alert_role_id_raw)
            except (TypeError, ValueError):
                alert_role_id = None
        per_task_cooldown_seconds = int(
            alerts_raw.get("per_task_cooldown_seconds", 900)
        )
        if per_task_cooldown_seconds <= 0:
            per_task_cooldown_seconds = 900
        global_cooldown_seconds = int(alerts_raw.get("global_cooldown_seconds", 10))
        if global_cooldown_seconds <= 0:
            global_cooldown_seconds = 10
        alerts = DiscordAlertConfig(
            enabled=alerts_enabled,
            alert_role_id=alert_role_id,
            per_task_cooldown_seconds=per_task_cooldown_seconds,
            global_cooldown_seconds=global_cooldown_seconds,
        )

        # Progress stream
        progress_raw_value = cfg.get("progress_stream")
        progress_raw: dict[str, Any] = (
            progress_raw_value if isinstance(progress_raw_value, dict) else {}
        )
        progress_enabled = bool(
            progress_raw.get("enabled", DEFAULT_PROGRESS_STREAM_ENABLED)
        )
        progress_max_actions = int(
            progress_raw.get("max_actions", DEFAULT_PROGRESS_STREAM_MAX_ACTIONS)
        )
        if progress_max_actions <= 0:
            progress_max_actions = DEFAULT_PROGRESS_STREAM_MAX_ACTIONS
        progress_max_output_chars = int(
            progress_raw.get(
                "max_output_chars", DEFAULT_PROGRESS_STREAM_MAX_OUTPUT_CHARS
            )
        )
        if progress_max_output_chars <= 0:
            progress_max_output_chars = DEFAULT_PROGRESS_STREAM_MAX_OUTPUT_CHARS
        progress_min_edit_interval_seconds = float(
            progress_raw.get(
                "min_edit_interval_seconds",
                DEFAULT_PROGRESS_STREAM_MIN_EDIT_INTERVAL_SECONDS,
            )
        )
        if progress_min_edit_interval_seconds <= 0:
            progress_min_edit_interval_seconds = (
                DEFAULT_PROGRESS_STREAM_MIN_EDIT_INTERVAL_SECONDS
            )
        progress_stream = DiscordBotProgressStreamConfig(
            enabled=progress_enabled,
            max_actions=progress_max_actions,
            max_output_chars=progress_max_output_chars,
            min_edit_interval_seconds=progress_min_edit_interval_seconds,
        )

        # Message overflow & metrics
        message_overflow = str(
            cfg.get("message_overflow", DEFAULT_MESSAGE_OVERFLOW)
        ).strip()
        if message_overflow:
            message_overflow = message_overflow.lower()
        if message_overflow not in MESSAGE_OVERFLOW_OPTIONS:
            message_overflow = DEFAULT_MESSAGE_OVERFLOW

        metrics_raw_value = cfg.get("metrics")
        metrics_raw: dict[str, Any] = (
            metrics_raw_value if isinstance(metrics_raw_value, dict) else {}
        )
        metrics_mode = str(metrics_raw.get("mode", DEFAULT_METRICS_MODE)).strip()
        if metrics_mode:
            metrics_mode = metrics_mode.lower()
        if metrics_mode not in METRICS_MODE_OPTIONS:
            metrics_mode = DEFAULT_METRICS_MODE

        coalesce_window_seconds = float(
            cfg.get("coalesce_window_seconds", DEFAULT_COALESCE_WINDOW_SECONDS)
        )
        if coalesce_window_seconds <= 0:
            coalesce_window_seconds = DEFAULT_COALESCE_WINDOW_SECONDS

        # Ticket flow
        ticket_flow_raw = (
            cfg.get("ticket_flow") if isinstance(cfg.get("ticket_flow"), dict) else {}
        )
        ticket_flow_auto_resume = bool(ticket_flow_raw.get("auto_resume", False))

        # Pause dispatch
        pause_raw_value = cfg.get("pause_dispatch_notifications")
        pause_raw: dict[str, Any] = (
            pause_raw_value if isinstance(pause_raw_value, dict) else {}
        )
        pause_enabled = bool(pause_raw.get("enabled", enabled))
        pause_send_attachments = bool(pause_raw.get("send_attachments", True))
        pause_max_file_size_bytes = int(
            pause_raw.get("max_file_size_bytes", DEFAULT_PAUSE_DISPATCH_MAX_FILE_BYTES)
        )
        if pause_max_file_size_bytes <= 0:
            pause_max_file_size_bytes = DEFAULT_PAUSE_DISPATCH_MAX_FILE_BYTES
        pause_chunk_long_messages = bool(pause_raw.get("chunk_long_messages", True))
        pause_dispatch_notifications = PauseDispatchNotifications(
            enabled=pause_enabled,
            send_attachments=pause_send_attachments,
            max_file_size_bytes=pause_max_file_size_bytes,
            chunk_long_messages=pause_chunk_long_messages,
        )

        # Default notification channel
        default_notification_channel_raw = cfg.get("default_notification_channel_id")
        default_notification_channel_id: Optional[int] = None
        if default_notification_channel_raw is not None:
            try:
                default_notification_channel_id = int(default_notification_channel_raw)
            except (TypeError, ValueError):
                default_notification_channel_id = None

        # Swarm
        swarm_raw_value = cfg.get("swarm")
        swarm_raw: dict[str, Any] = (
            swarm_raw_value if isinstance(swarm_raw_value, dict) else {}
        )
        swarm_enabled = bool(swarm_raw.get("enabled", True))
        swarm_max_agents = int(swarm_raw.get("max_agents", 6))
        if swarm_max_agents <= 0:
            swarm_max_agents = 6
        swarm_default_lead_model = str(
            swarm_raw.get("default_lead_model", "claude-opus-4-6")
        ).strip()
        if not swarm_default_lead_model:
            swarm_default_lead_model = "claude-opus-4-6"
        swarm_default_worker_model = str(
            swarm_raw.get("default_worker_model", "claude-sonnet-4-5-20250929")
        ).strip()
        if not swarm_default_worker_model:
            swarm_default_worker_model = "claude-sonnet-4-5-20250929"
        swarm_claude_binary = str(swarm_raw.get("claude_binary", "claude")).strip()
        if not swarm_claude_binary:
            swarm_claude_binary = "claude"
        swarm_poll_interval = _positive_float(
            swarm_raw.get("poll_interval_seconds"), 0.5
        )
        swarm_agent_timeout = _positive_float(
            swarm_raw.get("agent_timeout_seconds"), 3600.0
        )
        swarm_timeout = _positive_float(
            swarm_raw.get("swarm_timeout_seconds"), 7200.0
        )
        swarm_health_check = _positive_float(
            swarm_raw.get("health_check_interval_seconds"), 5.0
        )
        swarm_shutdown_grace = _positive_float(
            swarm_raw.get("shutdown_grace_seconds"), 10.0
        )
        swarm_custom_presets_raw = swarm_raw.get("custom_presets")
        swarm_custom_presets: dict[str, Any] = (
            dict(swarm_custom_presets_raw)
            if isinstance(swarm_custom_presets_raw, dict)
            else {}
        )
        swarm_config = SwarmConfig(
            enabled=swarm_enabled,
            max_agents=swarm_max_agents,
            default_lead_model=swarm_default_lead_model,
            default_worker_model=swarm_default_worker_model,
            claude_binary=swarm_claude_binary,
            poll_interval_seconds=swarm_poll_interval,
            agent_timeout_seconds=swarm_agent_timeout,
            swarm_timeout_seconds=swarm_timeout,
            health_check_interval_seconds=swarm_health_check,
            shutdown_grace_seconds=swarm_shutdown_grace,
            custom_presets=swarm_custom_presets,
        )

        # Dashboard & agent bus channels
        dashboard_channel_raw = cfg.get("dashboard_channel_id")
        dashboard_channel_id: Optional[int] = None
        if dashboard_channel_raw is not None:
            try:
                dashboard_channel_id = int(dashboard_channel_raw)
            except (TypeError, ValueError):
                dashboard_channel_id = None

        agent_bus_channel_raw = cfg.get("agent_bus_channel_id")
        agent_bus_channel_id: Optional[int] = None
        if agent_bus_channel_raw is not None:
            try:
                agent_bus_channel_id = int(agent_bus_channel_raw)
            except (TypeError, ValueError):
                agent_bus_channel_id = None

        # Agent binaries & commands
        agent_binaries = dict(agent_binaries or {})

        opencode_command: list[str] = []
        opencode_env_command = env.get("CAR_OPENCODE_COMMAND")
        if opencode_env_command:
            opencode_command = _parse_command(opencode_env_command)
        if not opencode_command:
            opencode_command = _parse_command(cfg.get("opencode_command"))

        # State file
        state_file = Path(cfg.get("state_file", DEFAULT_STATE_FILE))
        if not state_file.is_absolute():
            state_file = (root / state_file).resolve()
        if state_file.suffix == ".json":
            raise DiscordBotConfigError(
                "discord_bot.state_file must point to a SQLite database "
                "(.sqlite3). Use .codex-autorunner/discord_state.sqlite3"
            )

        # App server
        app_server_command_env = str(
            cfg.get("app_server_command_env", "CAR_DISCORD_APP_SERVER_COMMAND")
        )
        app_server_command: list[str] = []
        if app_server_command_env:
            env_command = env.get(app_server_command_env)
            if env_command:
                app_server_command = _parse_command(env_command)
        if not app_server_command:
            app_server_command = _parse_command(cfg.get("app_server_command"))
        if not app_server_command:
            app_server_command = list(DEFAULT_APP_SERVER_COMMAND)

        app_server_raw_value = cfg.get("app_server")
        app_server_raw: dict[str, Any] = (
            app_server_raw_value if isinstance(app_server_raw_value, dict) else {}
        )
        app_server_max_handles = int(
            app_server_raw.get("max_handles", DEFAULT_APP_SERVER_MAX_HANDLES)
        )
        if app_server_max_handles <= 0:
            app_server_max_handles = None
        app_server_idle_ttl_seconds = int(
            app_server_raw.get("idle_ttl_seconds", DEFAULT_APP_SERVER_IDLE_TTL_SECONDS)
        )
        if app_server_idle_ttl_seconds <= 0:
            app_server_idle_ttl_seconds = None
        app_server_start_timeout_seconds = float(
            app_server_raw.get(
                "start_timeout_seconds", DEFAULT_APP_SERVER_START_TIMEOUT_SECONDS
            )
        )
        if app_server_start_timeout_seconds <= 0:
            app_server_start_timeout_seconds = DEFAULT_APP_SERVER_START_TIMEOUT_SECONDS
        app_server_start_max_attempts_raw = app_server_raw.get("max_attempts")
        if app_server_start_max_attempts_raw is not None:
            app_server_start_max_attempts = int(app_server_start_max_attempts_raw)
            if app_server_start_max_attempts <= 0:
                app_server_start_max_attempts = None
        else:
            app_server_start_max_attempts = None
        app_server_turn_timeout_raw = app_server_raw.get(
            "turn_timeout_seconds", DEFAULT_APP_SERVER_TURN_TIMEOUT_SECONDS
        )
        if app_server_turn_timeout_raw is None:
            app_server_turn_timeout_seconds = None
        else:
            app_server_turn_timeout_seconds = float(app_server_turn_timeout_raw)
            if app_server_turn_timeout_seconds <= 0:
                app_server_turn_timeout_seconds = None

        # Agent timeouts
        agent_timeouts_raw = cfg.get("agent_timeouts")
        has_explicit_codex_timeout = False
        agent_timeouts: dict[str, Optional[float]] = dict(
            DEFAULT_AGENT_TURN_TIMEOUT_SECONDS
        )
        if isinstance(agent_timeouts_raw, dict):
            for key, value in agent_timeouts_raw.items():
                if str(key) == "codex":
                    has_explicit_codex_timeout = True
                if value is None:
                    agent_timeouts[str(key)] = None
                    continue
                try:
                    timeout_value = float(value)
                except (TypeError, ValueError):
                    continue
                if timeout_value <= 0:
                    agent_timeouts[str(key)] = None
                else:
                    agent_timeouts[str(key)] = timeout_value
        if not has_explicit_codex_timeout:
            agent_timeouts["codex"] = app_server_turn_timeout_seconds

        return cls(
            root=root,
            enabled=enabled,
            bot_token_env=bot_token_env,
            bot_token=bot_token,
            allowed_guild_ids=allowed_guild_ids,
            allowed_channel_ids=allowed_channel_ids,
            allowed_role_ids=allowed_role_ids,
            allowed_user_ids=allowed_user_ids,
            trigger_mode=trigger_mode,
            defaults=defaults,
            concurrency=concurrency,
            media=media,
            shell=shell,
            cache=cache,
            scaffold=scaffold,
            rbac=rbac,
            alerts=alerts,
            progress_stream=progress_stream,
            state_file=state_file,
            app_server_command_env=app_server_command_env,
            app_server_command=app_server_command,
            app_server_max_handles=app_server_max_handles,
            app_server_idle_ttl_seconds=app_server_idle_ttl_seconds,
            app_server_start_timeout_seconds=app_server_start_timeout_seconds,
            app_server_start_max_attempts=app_server_start_max_attempts,
            app_server_turn_timeout_seconds=app_server_turn_timeout_seconds,
            agent_turn_timeout_seconds=agent_timeouts,
            message_overflow=message_overflow,
            metrics_mode=metrics_mode,
            coalesce_window_seconds=coalesce_window_seconds,
            swarm=swarm_config,
            agent_binaries=agent_binaries,
            opencode_command=opencode_command,
            ticket_flow_auto_resume=ticket_flow_auto_resume,
            pause_dispatch_notifications=pause_dispatch_notifications,
            default_notification_channel_id=default_notification_channel_id,
            dashboard_channel_id=dashboard_channel_id,
            agent_bus_channel_id=agent_bus_channel_id,
        )

    def validate(self) -> None:
        issues: list[str] = []
        if not self.bot_token:
            issues.append(f"missing bot token env '{self.bot_token_env}'")
        if not self.allowed_guild_ids:
            issues.append(
                "no allowed guild ids configured "
                "(set allowed_guild_ids or CAR_DISCORD_GUILD_IDS env)"
            )
        if not self.app_server_command:
            issues.append("app_server_command must be set")
        if issues:
            raise DiscordBotConfigError(
                "discord_bot config issues: " + "; ".join(issues)
            )

    def allowlist(self) -> DiscordAllowlist:
        return DiscordAllowlist(
            allowed_guild_ids=set(self.allowed_guild_ids),
            allowed_channel_ids=set(self.allowed_channel_ids),
            allowed_role_ids=set(self.allowed_role_ids),
            allowed_user_ids=set(self.allowed_user_ids),
        )
