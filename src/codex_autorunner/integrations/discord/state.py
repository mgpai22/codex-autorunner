from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast

from ...core.sqlite_utils import connect_sqlite
from ...core.state import now_iso

logger = logging.getLogger("codex_autorunner.integrations.discord.state")

STATE_VERSION = 1
TOPIC_ROOT = "root"
APPROVAL_MODE_YOLO = "yolo"
APPROVAL_MODE_SAFE = "safe"
APPROVAL_MODES = {APPROVAL_MODE_YOLO, APPROVAL_MODE_SAFE}
AGENT_VALUES = {"codex", "opencode"}

DISCORD_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_approval_mode(
    mode: Optional[str], *, default: str = APPROVAL_MODE_YOLO
) -> str:
    if not isinstance(mode, str):
        return default
    key = mode.strip().lower()
    if key in APPROVAL_MODES:
        return key
    return default


def normalize_agent(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    compact = "".join(ch for ch in normalized if ch.isalnum())
    if normalized in AGENT_VALUES:
        return normalized
    if compact in AGENT_VALUES:
        return compact
    return None


def topic_key(guild_id: int, channel_id: int, thread_id: Optional[int] = None) -> str:
    """Build a unique topic key from Discord IDs.

    Format: ``<guild_id>:<channel_id>:<thread_id|root>``
    """
    if not isinstance(guild_id, int):
        raise TypeError("guild_id must be int")
    if not isinstance(channel_id, int):
        raise TypeError("channel_id must be int")
    suffix = str(thread_id) if thread_id is not None else TOPIC_ROOT
    return f"{guild_id}:{channel_id}:{suffix}"


def parse_topic_key(key: str) -> tuple[int, int, Optional[int]]:
    """Parse a topic key back into ``(guild_id, channel_id, thread_id)``."""
    parts = key.split(":", 2)
    if len(parts) < 3:
        raise ValueError("invalid topic key")
    guild_raw, channel_raw, thread_raw = parts[0], parts[1], parts[2]
    if not guild_raw or not channel_raw or not thread_raw:
        raise ValueError("invalid topic key")
    try:
        guild_id = int(guild_raw)
    except ValueError as exc:
        raise ValueError("invalid guild id in topic key") from exc
    try:
        channel_id = int(channel_raw)
    except ValueError as exc:
        raise ValueError("invalid channel id in topic key") from exc
    if thread_raw == TOPIC_ROOT:
        thread_id: Optional[int] = None
    else:
        try:
            thread_id = int(thread_raw)
        except ValueError as exc:
            raise ValueError("invalid thread id in topic key") from exc
    return guild_id, channel_id, thread_id


def _parse_json_payload(raw: Optional[str]) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass
class ThreadSummary:
    user_preview: Optional[str] = None
    assistant_preview: Optional[str] = None
    last_used_at: Optional[str] = None
    workspace_path: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["ThreadSummary"]:
        if not isinstance(payload, dict):
            return None
        user_preview = payload.get("user_preview") or payload.get("userPreview")
        assistant_preview = payload.get("assistant_preview") or payload.get(
            "assistantPreview"
        )
        last_used_at = payload.get("last_used_at") or payload.get("lastUsedAt")
        workspace_path = payload.get("workspace_path") or payload.get("workspacePath")
        if not isinstance(user_preview, str):
            user_preview = None
        if not isinstance(assistant_preview, str):
            assistant_preview = None
        if not isinstance(last_used_at, str):
            last_used_at = None
        if not isinstance(workspace_path, str):
            workspace_path = None
        return cls(
            user_preview=user_preview,
            assistant_preview=assistant_preview,
            last_used_at=last_used_at,
            workspace_path=workspace_path,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_preview": self.user_preview,
            "assistant_preview": self.assistant_preview,
            "last_used_at": self.last_used_at,
            "workspace_path": self.workspace_path,
        }


@dataclass
class DiscordTopicRecord:
    topic_key: Optional[str] = None
    guild_id: Optional[int] = None
    channel_id: Optional[int] = None
    thread_id: Optional[int] = None
    workspace_path: Optional[str] = None
    codex_thread_id: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    approval_mode: str = APPROVAL_MODE_YOLO
    approval_policy: Optional[str] = None
    sandbox_policy: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_turn_at: Optional[str] = None
    turn_count: int = 0
    active_turn_id: Optional[str] = None

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], *, default_approval_mode: str
    ) -> "DiscordTopicRecord":
        tk = payload.get("topic_key")
        if not isinstance(tk, str):
            tk = None

        guild_id = payload.get("guild_id")
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            guild_id = None

        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            channel_id = None

        thread_id = payload.get("thread_id")
        if thread_id is not None and (
            not isinstance(thread_id, int) or isinstance(thread_id, bool)
        ):
            thread_id = None

        workspace_path = payload.get("workspace_path") or payload.get("workspacePath")
        if not isinstance(workspace_path, str):
            workspace_path = None

        codex_thread_id = payload.get("codex_thread_id") or payload.get("codexThreadId")
        if not isinstance(codex_thread_id, str):
            codex_thread_id = None

        agent = normalize_agent(payload.get("agent"))

        model = payload.get("model")
        if not isinstance(model, str):
            model = None

        reasoning_effort = payload.get("reasoning_effort") or payload.get(
            "reasoningEffort"
        )
        if not isinstance(reasoning_effort, str):
            reasoning_effort = None

        approval_mode = payload.get("approval_mode") or payload.get("approvalMode")
        approval_mode = normalize_approval_mode(
            approval_mode, default=default_approval_mode
        )

        approval_policy = payload.get("approval_policy") or payload.get(
            "approvalPolicy"
        )
        if not isinstance(approval_policy, str):
            approval_policy = None

        sandbox_policy = payload.get("sandbox_policy") or payload.get("sandboxPolicy")
        if not isinstance(sandbox_policy, str):
            sandbox_policy = None

        created_at = payload.get("created_at")
        if not isinstance(created_at, str):
            created_at = None

        updated_at = payload.get("updated_at")
        if not isinstance(updated_at, str):
            updated_at = None

        last_turn_at = payload.get("last_turn_at") or payload.get("lastTurnAt")
        if not isinstance(last_turn_at, str):
            last_turn_at = None

        turn_count = payload.get("turn_count", 0)
        if not isinstance(turn_count, int) or isinstance(turn_count, bool):
            turn_count = 0

        active_turn_id = payload.get("active_turn_id") or payload.get("activeTurnId")
        if not isinstance(active_turn_id, str):
            active_turn_id = None

        return cls(
            topic_key=tk,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=thread_id,
            workspace_path=workspace_path,
            codex_thread_id=codex_thread_id,
            agent=agent,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_mode=approval_mode,
            approval_policy=approval_policy,
            sandbox_policy=sandbox_policy,
            created_at=created_at,
            updated_at=updated_at,
            last_turn_at=last_turn_at,
            turn_count=turn_count,
            active_turn_id=active_turn_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_key": self.topic_key,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "thread_id": self.thread_id,
            "workspace_path": self.workspace_path,
            "codex_thread_id": self.codex_thread_id,
            "agent": self.agent,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "approval_mode": self.approval_mode,
            "approval_policy": self.approval_policy,
            "sandbox_policy": self.sandbox_policy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_turn_at": self.last_turn_at,
            "turn_count": self.turn_count,
            "active_turn_id": self.active_turn_id,
        }


@dataclass
class PendingApprovalRecord:
    request_id: str
    turn_id: str
    guild_id: int
    channel_id: int
    thread_id: Optional[int]
    message_id: Optional[int]
    prompt: str
    created_at: str
    topic_key: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["PendingApprovalRecord"]:
        if not isinstance(payload, dict):
            return None
        request_id = payload.get("request_id")
        turn_id = payload.get("turn_id")
        guild_id = payload.get("guild_id")
        channel_id = payload.get("channel_id")
        thread_id = payload.get("thread_id")
        message_id = payload.get("message_id")
        prompt = payload.get("prompt") or ""
        created_at = payload.get("created_at")
        tk = payload.get("topic_key") or payload.get("topicKey")
        if not isinstance(request_id, str) or not request_id:
            return None
        if not isinstance(turn_id, str) or not turn_id:
            return None
        if not isinstance(guild_id, int):
            return None
        if not isinstance(channel_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if message_id is not None and not isinstance(message_id, int):
            message_id = None
        if not isinstance(prompt, str):
            prompt = ""
        if not isinstance(created_at, str) or not created_at:
            return None
        if not isinstance(tk, str) or not tk:
            tk = None
        return cls(
            request_id=request_id,
            turn_id=turn_id,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=thread_id,
            message_id=message_id,
            prompt=prompt,
            created_at=created_at,
            topic_key=tk,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "topic_key": self.topic_key,
        }


@dataclass
class OutboxRecord:
    record_id: str
    guild_id: int
    channel_id: int
    thread_id: Optional[int]
    reply_to_message_id: Optional[int]
    text: str
    created_at: str
    attempts: int = 0
    last_error: Optional[str] = None
    last_attempt_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    operation: Optional[str] = None
    message_id: Optional[int] = None
    outbox_key: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["OutboxRecord"]:
        if not isinstance(payload, dict):
            return None
        record_id = payload.get("record_id")
        guild_id = payload.get("guild_id")
        channel_id = payload.get("channel_id")
        thread_id = payload.get("thread_id")
        reply_to_message_id = payload.get("reply_to_message_id")
        text = payload.get("text") or ""
        created_at = payload.get("created_at")
        attempts = payload.get("attempts", 0)
        last_error = payload.get("last_error")
        last_attempt_at = payload.get("last_attempt_at")
        next_attempt_at = payload.get("next_attempt_at")
        operation = payload.get("operation")
        message_id = payload.get("message_id")
        outbox_key = payload.get("outbox_key")
        if not isinstance(record_id, str) or not record_id:
            return None
        if not isinstance(guild_id, int):
            return None
        if not isinstance(channel_id, int):
            return None
        if thread_id is not None and not isinstance(thread_id, int):
            thread_id = None
        if reply_to_message_id is not None and not isinstance(reply_to_message_id, int):
            reply_to_message_id = None
        if not isinstance(text, str):
            text = ""
        if not isinstance(created_at, str) or not created_at:
            return None
        if not isinstance(attempts, int) or attempts < 0:
            attempts = 0
        if not isinstance(last_error, str):
            last_error = None
        if not isinstance(last_attempt_at, str):
            last_attempt_at = None
        if not isinstance(next_attempt_at, str):
            next_attempt_at = None
        if not isinstance(operation, str):
            operation = None
        if message_id is not None and not isinstance(message_id, int):
            message_id = None
        if not isinstance(outbox_key, str):
            outbox_key = None
        return cls(
            record_id=record_id,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            text=text,
            created_at=created_at,
            attempts=attempts,
            last_error=last_error,
            last_attempt_at=last_attempt_at,
            next_attempt_at=next_attempt_at,
            operation=operation,
            message_id=message_id,
            outbox_key=outbox_key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "thread_id": self.thread_id,
            "reply_to_message_id": self.reply_to_message_id,
            "text": self.text,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "next_attempt_at": self.next_attempt_at,
            "operation": self.operation,
            "message_id": self.message_id,
            "outbox_key": self.outbox_key,
        }


# ---------------------------------------------------------------------------
# Aggregate state (in-memory snapshot)
# ---------------------------------------------------------------------------


@dataclass
class DiscordState:
    version: int = STATE_VERSION
    topics: dict[str, DiscordTopicRecord] = dataclasses.field(default_factory=dict)
    channel_bindings: dict[str, str] = dataclasses.field(default_factory=dict)
    meta: dict[str, str] = dataclasses.field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "topics": {key: record.to_dict() for key, record in self.topics.items()},
            "channel_bindings": dict(self.channel_bindings),
            "meta": dict(self.meta),
        }
        return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# DiscordStateStore -- SQLite-backed, async-safe via a single-thread executor
# ---------------------------------------------------------------------------


class DiscordStateStore:
    def __init__(
        self, db_path: Path, *, default_approval_mode: str = APPROVAL_MODE_YOLO
    ) -> None:
        self._path = db_path
        self._default_approval_mode = normalize_approval_mode(default_approval_mode)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="discord-state"
        )
        self._connection: Optional[sqlite3.Connection] = None

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._run(self._close_sync)
        self._executor.shutdown(wait=True)

    # ------------------------------------------------------------------
    # Full state load / save
    # ------------------------------------------------------------------

    async def load(self) -> DiscordState:
        return await self._run(self._load_state_sync)

    async def save(self, state: DiscordState) -> None:
        await self._run(self._save_state_sync, state)

    # ------------------------------------------------------------------
    # Topics CRUD
    # ------------------------------------------------------------------

    async def get_topic(self, key: str) -> Optional[DiscordTopicRecord]:
        return await self._run(self._get_topic_sync, key)

    async def save_topic(self, key: str, record: DiscordTopicRecord) -> None:
        await self._run(self._save_topic_sync, key, record)

    async def list_topics(self) -> dict[str, DiscordTopicRecord]:
        return await self._run(self._list_topics_sync)

    async def update_topic_field(
        self, key: str, apply: Callable[[DiscordTopicRecord], None]
    ) -> DiscordTopicRecord:
        return await self._run(self._update_topic_field_sync, key, apply)

    async def ensure_topic(self, key: str) -> DiscordTopicRecord:
        def apply(_record: DiscordTopicRecord) -> None:
            pass

        return await self.update_topic_field(key, apply)

    async def delete_topic(self, key: str) -> None:
        await self._run(self._delete_topic_sync, key)

    # ------------------------------------------------------------------
    # Channel bindings
    # ------------------------------------------------------------------

    async def set_channel_binding(self, channel_key: str, workspace_path: str) -> None:
        await self._run(self._set_channel_binding_sync, channel_key, workspace_path)

    async def get_channel_binding(self, channel_key: str) -> Optional[str]:
        return await self._run(self._get_channel_binding_sync, channel_key)

    async def list_channel_bindings(self) -> dict[str, str]:
        return await self._run(self._list_channel_bindings_sync)

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------

    async def enqueue_outbox(self, record: OutboxRecord) -> OutboxRecord:
        return await self._run(self._upsert_outbox_sync, record)

    async def update_outbox(self, record: OutboxRecord) -> OutboxRecord:
        return await self._run(self._upsert_outbox_sync, record)

    async def list_pending_outbox(self) -> list[OutboxRecord]:
        return await self._run(self._list_outbox_sync)

    async def get_outbox(self, record_id: str) -> Optional[OutboxRecord]:
        return await self._run(self._get_outbox_sync, record_id)

    async def mark_outbox_sent(self, record_id: str) -> None:
        await self._run(self._delete_outbox_sync, record_id)

    async def delete_outbox_item(self, record_id: str) -> None:
        await self._run(self._delete_outbox_sync, record_id)

    # ------------------------------------------------------------------
    # Pending approvals
    # ------------------------------------------------------------------

    async def save_pending_approval(
        self, record: PendingApprovalRecord
    ) -> PendingApprovalRecord:
        return await self._run(self._upsert_pending_approval_sync, record)

    async def get_pending_approval(
        self, request_id: str
    ) -> Optional[PendingApprovalRecord]:
        return await self._run(self._get_pending_approval_sync, request_id)

    async def delete_pending_approval(self, request_id: str) -> None:
        await self._run(self._delete_pending_approval_sync, request_id)

    async def list_pending_approvals(self) -> list[PendingApprovalRecord]:
        return await self._run(self._list_pending_approvals_sync)

    async def pending_approvals_for_topic(
        self, key: str
    ) -> list[PendingApprovalRecord]:
        return await self._run(self._pending_approvals_for_topic_sync, key)

    async def clear_pending_approvals_for_topic(self, key: str) -> None:
        await self._run(self._clear_pending_approvals_for_topic_sync, key)

    # ------------------------------------------------------------------
    # Scaffolded channels
    # ------------------------------------------------------------------

    async def save_scaffolded_channel(
        self,
        guild_id: int,
        workspace_id: str,
        channel_type: str,
        discord_channel_id: int,
    ) -> None:
        await self._run(
            self._save_scaffolded_channel_sync,
            guild_id,
            workspace_id,
            channel_type,
            discord_channel_id,
        )

    async def get_scaffolded_channel(
        self, guild_id: int, workspace_id: str, channel_type: str
    ) -> Optional[int]:
        return await self._run(
            self._get_scaffolded_channel_sync, guild_id, workspace_id, channel_type
        )

    async def list_scaffolded_channels(
        self, guild_id: int
    ) -> list[tuple[int, str, str, int, str]]:
        return await self._run(self._list_scaffolded_channels_sync, guild_id)

    async def delete_scaffolded_channel(
        self, guild_id: int, workspace_id: str, channel_type: str
    ) -> None:
        await self._run(
            self._delete_scaffolded_channel_sync, guild_id, workspace_id, channel_type
        )

    # ------------------------------------------------------------------
    # Forum tags
    # ------------------------------------------------------------------

    async def save_forum_tag(
        self,
        guild_id: int,
        workspace_id: str,
        forum_channel_id: int,
        tag_name: str,
        tag_id: int,
    ) -> None:
        await self._run(
            self._save_forum_tag_sync,
            guild_id,
            workspace_id,
            forum_channel_id,
            tag_name,
            tag_id,
        )

    async def get_forum_tags(self, guild_id: int, workspace_id: str) -> dict[str, int]:
        return await self._run(self._get_forum_tags_sync, guild_id, workspace_id)

    async def delete_forum_tag(self, guild_id: int, workspace_id: str, tag_name: str) -> None:
        await self._run(self._delete_forum_tag_sync, guild_id, workspace_id, tag_name)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def save_task(
        self,
        guild_id: int,
        workspace_id: str,
        forum_channel_id: int,
        thread_id: int,
        root_message_id: int,
        created_by_user_id: Optional[int],
        initial_prompt: Optional[str],
    ) -> None:
        await self._run(
            self._save_task_sync,
            guild_id,
            workspace_id,
            forum_channel_id,
            thread_id,
            root_message_id,
            created_by_user_id,
            initial_prompt,
        )

    async def get_task(self, guild_id: int, thread_id: int) -> Optional[dict[str, Any]]:
        return await self._run(self._get_task_sync, guild_id, thread_id)

    async def list_tasks(
        self,
        guild_id: int,
        workspace_id: Optional[str] = None,
        state: Optional[str] = None,
        created_by_user_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_tasks_sync,
            guild_id,
            workspace_id,
            state,
            created_by_user_id,
        )

    async def update_task_state(self, guild_id: int, thread_id: int, state: str) -> None:
        await self._run(self._update_task_state_sync, guild_id, thread_id, state)

    async def update_task_activity(
        self, guild_id: int, thread_id: int, message_id: int
    ) -> None:
        await self._run(self._update_task_activity_sync, guild_id, thread_id, message_id)

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    async def save_alert(self, guild_id: int, thread_id: int, alert_type: str) -> None:
        await self._run(self._save_alert_sync, guild_id, thread_id, alert_type)

    async def get_alert(
        self, guild_id: int, thread_id: int, alert_type: str
    ) -> Optional[str]:
        return await self._run(self._get_alert_sync, guild_id, thread_id, alert_type)

    async def should_alert(
        self,
        guild_id: int,
        thread_id: int,
        alert_type: str,
        cooldown_seconds: int,
    ) -> bool:
        return await self._run(
            self._should_alert_sync,
            guild_id,
            thread_id,
            alert_type,
            cooldown_seconds,
        )

    # ------------------------------------------------------------------
    # Agent webhooks
    # ------------------------------------------------------------------

    async def save_webhook(
        self,
        guild_id: int,
        channel_id: int,
        agent_name: str,
        webhook_id: int,
        webhook_token: str,
    ) -> None:
        await self._run(
            self._save_webhook_sync,
            guild_id,
            channel_id,
            agent_name,
            webhook_id,
            webhook_token,
        )

    async def get_webhook(
        self, guild_id: int, channel_id: int, agent_name: str
    ) -> Optional[tuple[int, str]]:
        return await self._run(
            self._get_webhook_sync, guild_id, channel_id, agent_name
        )

    async def list_webhooks(
        self, guild_id: int, channel_id: int
    ) -> list[tuple[str, int, str, str]]:
        return await self._run(self._list_webhooks_sync, guild_id, channel_id)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    async def save_dashboard(self, guild_id: int, channel_id: int, message_id: int) -> None:
        await self._run(self._save_dashboard_sync, guild_id, channel_id, message_id)

    async def get_dashboard(
        self, guild_id: int
    ) -> Optional[tuple[int, int, str, Optional[str]]]:
        return await self._run(self._get_dashboard_sync, guild_id)

    async def update_dashboard_timestamp(self, guild_id: int) -> None:
        await self._run(self._update_dashboard_timestamp_sync, guild_id)

    # ------------------------------------------------------------------
    # Internal: async executor bridge
    # ------------------------------------------------------------------

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, func, *args)

    # ------------------------------------------------------------------
    # Internal: connection management
    # ------------------------------------------------------------------

    def _connection_sync(self) -> sqlite3.Connection:
        if self._connection is None:
            conn = connect_sqlite(self._path)
            self._ensure_schema(conn)
            self._connection = conn
        return self._connection

    def _close_sync(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_topics (
                    topic_key TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_channel_bindings (
                    channel_key TEXT PRIMARY KEY,
                    workspace_path TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_pending_approvals (
                    request_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dc_approvals_topic
                    ON discord_pending_approvals(json_extract(data, '$.topic_key'))
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    next_retry_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dc_outbox_created
                    ON discord_outbox(created_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_scaffolded_channels (
                    guild_id INTEGER NOT NULL,
                    workspace_id TEXT NOT NULL,
                    channel_type TEXT NOT NULL,
                    discord_channel_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, workspace_id, channel_type)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_forum_tags (
                    guild_id INTEGER NOT NULL,
                    workspace_id TEXT NOT NULL,
                    forum_channel_id INTEGER NOT NULL,
                    tag_name TEXT NOT NULL,
                    tag_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, workspace_id, tag_name)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_tasks (
                    guild_id INTEGER NOT NULL,
                    workspace_id TEXT NOT NULL,
                    forum_channel_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    root_message_id INTEGER NOT NULL,
                    created_by_user_id INTEGER,
                    initial_prompt TEXT,
                    created_at TEXT NOT NULL,
                    last_state TEXT,
                    last_state_at TEXT,
                    last_activity_message_id INTEGER,
                    PRIMARY KEY (guild_id, thread_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_alerts (
                    guild_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    alert_type TEXT NOT NULL,
                    last_sent_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, thread_id, alert_type)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_agent_webhooks (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    agent_name TEXT NOT NULL,
                    webhook_id INTEGER NOT NULL,
                    webhook_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id, agent_name)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_dashboard (
                    guild_id INTEGER NOT NULL PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    pinned_at TEXT NOT NULL,
                    last_updated_at TEXT
                )
                """
            )
            now = now_iso()
            self._set_meta(conn, "schema_version", str(DISCORD_SCHEMA_VERSION), now)
            self._set_meta(conn, "state_version", str(STATE_VERSION), now)

    # ------------------------------------------------------------------
    # Meta helpers
    # ------------------------------------------------------------------

    def _set_meta(
        self, conn: sqlite3.Connection, key: str, value: str, _updated_at: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO discord_meta (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def _get_meta(self, conn: sqlite3.Connection, key: str) -> Optional[str]:
        row = conn.execute(
            "SELECT value FROM discord_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        value = row["value"]
        return value if isinstance(value, str) else None

    # ------------------------------------------------------------------
    # Sync: full state load / save
    # ------------------------------------------------------------------

    def _load_state_sync(self) -> DiscordState:
        conn = self._connection_sync()
        meta: dict[str, str] = {}
        for row in conn.execute("SELECT key, value FROM discord_meta"):
            val = row["value"]
            if isinstance(val, str):
                meta[row["key"]] = val

        version = STATE_VERSION
        raw_version = meta.get("state_version")
        if isinstance(raw_version, str):
            try:
                version = int(raw_version)
            except ValueError:
                version = STATE_VERSION

        topics: dict[str, DiscordTopicRecord] = {}
        for row in conn.execute("SELECT topic_key, data FROM discord_topics"):
            payload = _parse_json_payload(row["data"])
            record = DiscordTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
            topics[row["topic_key"]] = record

        channel_bindings: dict[str, str] = {}
        for row in conn.execute(
            "SELECT channel_key, workspace_path FROM discord_channel_bindings"
        ):
            channel_bindings[row["channel_key"]] = row["workspace_path"]

        return DiscordState(
            version=version,
            topics=topics,
            channel_bindings=channel_bindings,
            meta=meta,
        )

    def _save_state_sync(self, state: DiscordState) -> None:
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute("DELETE FROM discord_topics")
            for key, record in state.topics.items():
                payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
                conn.execute(
                    """
                    INSERT INTO discord_topics (topic_key, data, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (key, payload_json, now),
                )

            conn.execute("DELETE FROM discord_channel_bindings")
            for channel_key, workspace_path in state.channel_bindings.items():
                conn.execute(
                    """
                    INSERT INTO discord_channel_bindings
                        (channel_key, workspace_path, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (channel_key, workspace_path, now),
                )

            self._set_meta(conn, "state_version", str(state.version), now)
            for mk, mv in state.meta.items():
                self._set_meta(conn, mk, mv, now)

    # ------------------------------------------------------------------
    # Sync: topics CRUD
    # ------------------------------------------------------------------

    def _get_topic_sync(self, key: str) -> Optional[DiscordTopicRecord]:
        if not isinstance(key, str) or not key:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT data FROM discord_topics WHERE topic_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["data"])
        return DiscordTopicRecord.from_dict(
            payload, default_approval_mode=self._default_approval_mode
        )

    def _save_topic_sync(self, key: str, record: DiscordTopicRecord) -> None:
        if not isinstance(key, str) or not key:
            return
        conn = self._connection_sync()
        now = now_iso()
        record.updated_at = now
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        with conn:
            conn.execute(
                """
                INSERT INTO discord_topics (topic_key, data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (key, payload_json, now),
            )

    def _list_topics_sync(self) -> dict[str, DiscordTopicRecord]:
        conn = self._connection_sync()
        topics: dict[str, DiscordTopicRecord] = {}
        for row in conn.execute("SELECT topic_key, data FROM discord_topics"):
            payload = _parse_json_payload(row["data"])
            record = DiscordTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
            topics[str(row["topic_key"])] = record
        return topics

    def _update_topic_field_sync(
        self, key: str, apply: Callable[[DiscordTopicRecord], None]
    ) -> DiscordTopicRecord:
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT data FROM discord_topics WHERE topic_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            record = DiscordTopicRecord(approval_mode=self._default_approval_mode)
        else:
            payload = _parse_json_payload(row["data"])
            record = DiscordTopicRecord.from_dict(
                payload, default_approval_mode=self._default_approval_mode
            )
        apply(record)
        record.approval_mode = normalize_approval_mode(
            record.approval_mode, default=self._default_approval_mode
        )
        now = now_iso()
        record.updated_at = now
        if record.created_at is None:
            record.created_at = now
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        with conn:
            conn.execute(
                """
                INSERT INTO discord_topics (topic_key, data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (key, payload_json, now),
            )
        return record

    def _delete_topic_sync(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM discord_topics WHERE topic_key = ?",
                (key,),
            )

    # ------------------------------------------------------------------
    # Sync: channel bindings
    # ------------------------------------------------------------------

    def _set_channel_binding_sync(self, channel_key: str, workspace_path: str) -> None:
        if not isinstance(channel_key, str) or not channel_key:
            return
        if not isinstance(workspace_path, str) or not workspace_path:
            return
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO discord_channel_bindings
                    (channel_key, workspace_path, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_key) DO UPDATE SET
                    workspace_path=excluded.workspace_path,
                    updated_at=excluded.updated_at
                """,
                (channel_key, workspace_path, now),
            )

    def _get_channel_binding_sync(self, channel_key: str) -> Optional[str]:
        if not isinstance(channel_key, str) or not channel_key:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT workspace_path FROM discord_channel_bindings WHERE channel_key = ?",
            (channel_key,),
        ).fetchone()
        if row is None:
            return None
        return row["workspace_path"]

    def _list_channel_bindings_sync(self) -> dict[str, str]:
        conn = self._connection_sync()
        bindings: dict[str, str] = {}
        for row in conn.execute(
            "SELECT channel_key, workspace_path FROM discord_channel_bindings"
        ):
            bindings[row["channel_key"]] = row["workspace_path"]
        return bindings

    # ------------------------------------------------------------------
    # Sync: outbox
    # ------------------------------------------------------------------

    def _upsert_outbox_sync(self, record: OutboxRecord) -> OutboxRecord:
        conn = self._connection_sync()
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        with conn:
            conn.execute(
                """
                INSERT INTO discord_outbox
                    (outbox_id, data, attempts, created_at, next_retry_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(outbox_id) DO UPDATE SET
                    data=excluded.data,
                    attempts=excluded.attempts,
                    created_at=excluded.created_at,
                    next_retry_at=excluded.next_retry_at
                """,
                (
                    record.record_id,
                    payload_json,
                    record.attempts,
                    record.created_at,
                    record.next_attempt_at,
                ),
            )
        return record

    def _list_outbox_sync(self) -> list[OutboxRecord]:
        conn = self._connection_sync()
        records: list[OutboxRecord] = []
        for row in conn.execute("SELECT data FROM discord_outbox ORDER BY created_at"):
            payload = _parse_json_payload(row["data"])
            record = OutboxRecord.from_dict(payload)
            if record is not None:
                records.append(record)
        return records

    def _get_outbox_sync(self, record_id: str) -> Optional[OutboxRecord]:
        if not isinstance(record_id, str) or not record_id:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT data FROM discord_outbox WHERE outbox_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["data"])
        return OutboxRecord.from_dict(payload)

    def _delete_outbox_sync(self, record_id: str) -> None:
        if not isinstance(record_id, str) or not record_id:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM discord_outbox WHERE outbox_id = ?",
                (record_id,),
            )

    # ------------------------------------------------------------------
    # Sync: pending approvals
    # ------------------------------------------------------------------

    def _upsert_pending_approval_sync(
        self, record: PendingApprovalRecord
    ) -> PendingApprovalRecord:
        conn = self._connection_sync()
        payload_json = json.dumps(record.to_dict(), ensure_ascii=True)
        with conn:
            conn.execute(
                """
                INSERT INTO discord_pending_approvals
                    (request_id, data, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    data=excluded.data,
                    created_at=excluded.created_at
                """,
                (record.request_id, payload_json, record.created_at),
            )
        return record

    def _get_pending_approval_sync(
        self, request_id: str
    ) -> Optional[PendingApprovalRecord]:
        if not isinstance(request_id, str) or not request_id:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            "SELECT data FROM discord_pending_approvals WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _parse_json_payload(row["data"])
        return PendingApprovalRecord.from_dict(payload)

    def _delete_pending_approval_sync(self, request_id: str) -> None:
        if not isinstance(request_id, str) or not request_id:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                "DELETE FROM discord_pending_approvals WHERE request_id = ?",
                (request_id,),
            )

    def _list_pending_approvals_sync(self) -> list[PendingApprovalRecord]:
        conn = self._connection_sync()
        records: list[PendingApprovalRecord] = []
        for row in conn.execute(
            "SELECT data FROM discord_pending_approvals ORDER BY created_at"
        ):
            payload = _parse_json_payload(row["data"])
            record = PendingApprovalRecord.from_dict(payload)
            if record is not None:
                records.append(record)
        return records

    def _pending_approvals_for_topic_sync(
        self, key: str
    ) -> list[PendingApprovalRecord]:
        if not isinstance(key, str) or not key:
            return []
        conn = self._connection_sync()
        pending: list[PendingApprovalRecord] = []
        for row in conn.execute(
            """
            SELECT data FROM discord_pending_approvals
             WHERE json_extract(data, '$.topic_key') = ?
            """,
            (key,),
        ):
            payload = _parse_json_payload(row["data"])
            record = PendingApprovalRecord.from_dict(payload)
            if record is not None:
                pending.append(record)
        return pending

    def _clear_pending_approvals_for_topic_sync(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                DELETE FROM discord_pending_approvals
                 WHERE json_extract(data, '$.topic_key') = ?
                """,
                (key,),
            )

    # ------------------------------------------------------------------
    # Sync: scaffolded channels
    # ------------------------------------------------------------------

    def _save_scaffolded_channel_sync(
        self,
        guild_id: int,
        workspace_id: str,
        channel_type: str,
        discord_channel_id: int,
    ) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(workspace_id, str) or not workspace_id:
            return
        if not isinstance(channel_type, str) or not channel_type:
            return
        if not isinstance(discord_channel_id, int) or isinstance(discord_channel_id, bool):
            return
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO discord_scaffolded_channels
                    (guild_id, workspace_id, channel_type, discord_channel_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, workspace_id, channel_type) DO UPDATE SET
                    discord_channel_id=excluded.discord_channel_id,
                    created_at=excluded.created_at
                """,
                (guild_id, workspace_id, channel_type, discord_channel_id, now),
            )

    def _get_scaffolded_channel_sync(
        self, guild_id: int, workspace_id: str, channel_type: str
    ) -> Optional[int]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return None
        if not isinstance(workspace_id, str) or not workspace_id:
            return None
        if not isinstance(channel_type, str) or not channel_type:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            """
            SELECT discord_channel_id FROM discord_scaffolded_channels
             WHERE guild_id = ? AND workspace_id = ? AND channel_type = ?
            """,
            (guild_id, workspace_id, channel_type),
        ).fetchone()
        if row is None:
            return None
        channel_id = row["discord_channel_id"]
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return None
        return channel_id

    def _list_scaffolded_channels_sync(
        self, guild_id: int
    ) -> list[tuple[int, str, str, int, str]]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return []
        conn = self._connection_sync()
        rows = conn.execute(
            """
            SELECT guild_id, workspace_id, channel_type, discord_channel_id, created_at
              FROM discord_scaffolded_channels
             WHERE guild_id = ?
             ORDER BY workspace_id, channel_type
            """,
            (guild_id,),
        )
        records: list[tuple[int, str, str, int, str]] = []
        for row in rows:
            created_at = row["created_at"]
            workspace_id = row["workspace_id"]
            channel_type = row["channel_type"]
            channel_id = row["discord_channel_id"]
            if (
                not isinstance(created_at, str)
                or not isinstance(workspace_id, str)
                or not isinstance(channel_type, str)
                or not isinstance(channel_id, int)
                or isinstance(channel_id, bool)
            ):
                continue
            records.append((guild_id, workspace_id, channel_type, channel_id, created_at))
        return records

    def _delete_scaffolded_channel_sync(
        self, guild_id: int, workspace_id: str, channel_type: str
    ) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(workspace_id, str) or not workspace_id:
            return
        if not isinstance(channel_type, str) or not channel_type:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                DELETE FROM discord_scaffolded_channels
                 WHERE guild_id = ? AND workspace_id = ? AND channel_type = ?
                """,
                (guild_id, workspace_id, channel_type),
            )

    # ------------------------------------------------------------------
    # Sync: forum tags
    # ------------------------------------------------------------------

    def _save_forum_tag_sync(
        self,
        guild_id: int,
        workspace_id: str,
        forum_channel_id: int,
        tag_name: str,
        tag_id: int,
    ) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(workspace_id, str) or not workspace_id:
            return
        if not isinstance(forum_channel_id, int) or isinstance(forum_channel_id, bool):
            return
        if not isinstance(tag_name, str) or not tag_name:
            return
        if not isinstance(tag_id, int) or isinstance(tag_id, bool):
            return
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO discord_forum_tags
                    (guild_id, workspace_id, forum_channel_id, tag_name, tag_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, workspace_id, tag_name) DO UPDATE SET
                    forum_channel_id=excluded.forum_channel_id,
                    tag_id=excluded.tag_id,
                    created_at=excluded.created_at
                """,
                (guild_id, workspace_id, forum_channel_id, tag_name, tag_id, now),
            )

    def _get_forum_tags_sync(self, guild_id: int, workspace_id: str) -> dict[str, int]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return {}
        if not isinstance(workspace_id, str) or not workspace_id:
            return {}
        conn = self._connection_sync()
        tags: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT tag_name, tag_id FROM discord_forum_tags
             WHERE guild_id = ? AND workspace_id = ?
             ORDER BY tag_name
            """,
            (guild_id, workspace_id),
        ):
            tag_name = row["tag_name"]
            tag_id = row["tag_id"]
            if not isinstance(tag_name, str) or not tag_name:
                continue
            if not isinstance(tag_id, int) or isinstance(tag_id, bool):
                continue
            tags[tag_name] = tag_id
        return tags

    def _delete_forum_tag_sync(self, guild_id: int, workspace_id: str, tag_name: str) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(workspace_id, str) or not workspace_id:
            return
        if not isinstance(tag_name, str) or not tag_name:
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                DELETE FROM discord_forum_tags
                 WHERE guild_id = ? AND workspace_id = ? AND tag_name = ?
                """,
                (guild_id, workspace_id, tag_name),
            )

    # ------------------------------------------------------------------
    # Sync: tasks
    # ------------------------------------------------------------------

    def _save_task_sync(
        self,
        guild_id: int,
        workspace_id: str,
        forum_channel_id: int,
        thread_id: int,
        root_message_id: int,
        created_by_user_id: Optional[int],
        initial_prompt: Optional[str],
    ) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(workspace_id, str) or not workspace_id:
            return
        if not isinstance(forum_channel_id, int) or isinstance(forum_channel_id, bool):
            return
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return
        if not isinstance(root_message_id, int) or isinstance(root_message_id, bool):
            return
        if created_by_user_id is not None and (
            not isinstance(created_by_user_id, int) or isinstance(created_by_user_id, bool)
        ):
            created_by_user_id = None
        if initial_prompt is not None and not isinstance(initial_prompt, str):
            initial_prompt = None
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO discord_tasks (
                    guild_id,
                    workspace_id,
                    forum_channel_id,
                    thread_id,
                    root_message_id,
                    created_by_user_id,
                    initial_prompt,
                    created_at,
                    last_state,
                    last_state_at,
                    last_activity_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, thread_id) DO UPDATE SET
                    workspace_id=excluded.workspace_id,
                    forum_channel_id=excluded.forum_channel_id,
                    root_message_id=excluded.root_message_id,
                    created_by_user_id=excluded.created_by_user_id,
                    initial_prompt=excluded.initial_prompt
                """,
                (
                    guild_id,
                    workspace_id,
                    forum_channel_id,
                    thread_id,
                    root_message_id,
                    created_by_user_id,
                    initial_prompt,
                    now,
                    "queued",
                    now,
                    None,
                ),
            )

    def _get_task_sync(self, guild_id: int, thread_id: int) -> Optional[dict[str, Any]]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return None
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return None
        conn = self._connection_sync()
        row = conn.execute(
            """
            SELECT
                guild_id,
                workspace_id,
                forum_channel_id,
                thread_id,
                root_message_id,
                created_by_user_id,
                initial_prompt,
                created_at,
                last_state,
                last_state_at,
                last_activity_message_id
            FROM discord_tasks
             WHERE guild_id = ? AND thread_id = ?
            """,
            (guild_id, thread_id),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _list_tasks_sync(
        self,
        guild_id: int,
        workspace_id: Optional[str],
        state: Optional[str],
        created_by_user_id: Optional[int],
    ) -> list[dict[str, Any]]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return []
        clauses = ["guild_id = ?"]
        params: list[Any] = [guild_id]
        if isinstance(workspace_id, str) and workspace_id:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if isinstance(state, str) and state:
            clauses.append("last_state = ?")
            params.append(state)
        if (
            created_by_user_id is not None
            and isinstance(created_by_user_id, int)
            and not isinstance(created_by_user_id, bool)
        ):
            clauses.append("created_by_user_id = ?")
            params.append(created_by_user_id)
        query = (
            "SELECT "
            "guild_id, "
            "workspace_id, "
            "forum_channel_id, "
            "thread_id, "
            "root_message_id, "
            "created_by_user_id, "
            "initial_prompt, "
            "created_at, "
            "last_state, "
            "last_state_at, "
            "last_activity_message_id "
            "FROM discord_tasks "
            "WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC"
        )
        conn = self._connection_sync()
        records: list[dict[str, Any]] = []
        for row in conn.execute(query, tuple(params)):
            records.append(dict(row))
        return records

    def _update_task_state_sync(self, guild_id: int, thread_id: int, state: str) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return
        if not isinstance(state, str) or not state:
            return
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                UPDATE discord_tasks
                   SET last_state = ?, last_state_at = ?
                 WHERE guild_id = ? AND thread_id = ?
                """,
                (state, now, guild_id, thread_id),
            )

    def _update_task_activity_sync(
        self, guild_id: int, thread_id: int, message_id: int
    ) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        conn = self._connection_sync()
        with conn:
            conn.execute(
                """
                UPDATE discord_tasks
                   SET last_activity_message_id = ?
                 WHERE guild_id = ? AND thread_id = ?
                """,
                (message_id, guild_id, thread_id),
            )

    # ------------------------------------------------------------------
    # Sync: alerts
    # ------------------------------------------------------------------

    def _save_alert_sync(self, guild_id: int, thread_id: int, alert_type: str) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return
        if not isinstance(alert_type, str) or not alert_type:
            return
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO discord_alerts (guild_id, thread_id, alert_type, last_sent_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, thread_id, alert_type) DO UPDATE SET
                    last_sent_at=excluded.last_sent_at
                """,
                (guild_id, thread_id, alert_type, now),
            )

    def _get_alert_sync(
        self, guild_id: int, thread_id: int, alert_type: str
    ) -> Optional[str]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return None
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return None
        if not isinstance(alert_type, str) or not alert_type:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            """
            SELECT last_sent_at FROM discord_alerts
             WHERE guild_id = ? AND thread_id = ? AND alert_type = ?
            """,
            (guild_id, thread_id, alert_type),
        ).fetchone()
        if row is None:
            return None
        last_sent_at = row["last_sent_at"]
        return last_sent_at if isinstance(last_sent_at, str) else None

    def _should_alert_sync(
        self,
        guild_id: int,
        thread_id: int,
        alert_type: str,
        cooldown_seconds: int,
    ) -> bool:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return False
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            return False
        if not isinstance(alert_type, str) or not alert_type:
            return False
        try:
            cooldown = int(cooldown_seconds)
        except (TypeError, ValueError):
            cooldown = 0
        if cooldown <= 0:
            return True
        last_sent_at = self._get_alert_sync(guild_id, thread_id, alert_type)
        if last_sent_at is None:
            return True
        last_dt = _parse_timestamp(last_sent_at)
        if last_dt is None:
            return True
        now_dt = _parse_timestamp(now_iso())
        if now_dt is None:
            return True
        elapsed = (now_dt - last_dt).total_seconds()
        return elapsed >= cooldown

    # ------------------------------------------------------------------
    # Sync: agent webhooks
    # ------------------------------------------------------------------

    def _save_webhook_sync(
        self,
        guild_id: int,
        channel_id: int,
        agent_name: str,
        webhook_id: int,
        webhook_token: str,
    ) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return
        if not isinstance(agent_name, str) or not agent_name:
            return
        if not isinstance(webhook_id, int) or isinstance(webhook_id, bool):
            return
        if not isinstance(webhook_token, str) or not webhook_token:
            return
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO discord_agent_webhooks
                    (guild_id, channel_id, agent_name, webhook_id, webhook_token, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, channel_id, agent_name) DO UPDATE SET
                    webhook_id=excluded.webhook_id,
                    webhook_token=excluded.webhook_token,
                    created_at=excluded.created_at
                """,
                (guild_id, channel_id, agent_name, webhook_id, webhook_token, now),
            )

    def _get_webhook_sync(
        self, guild_id: int, channel_id: int, agent_name: str
    ) -> Optional[tuple[int, str]]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return None
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return None
        if not isinstance(agent_name, str) or not agent_name:
            return None
        conn = self._connection_sync()
        row = conn.execute(
            """
            SELECT webhook_id, webhook_token FROM discord_agent_webhooks
             WHERE guild_id = ? AND channel_id = ? AND agent_name = ?
            """,
            (guild_id, channel_id, agent_name),
        ).fetchone()
        if row is None:
            return None
        webhook_id = row["webhook_id"]
        webhook_token = row["webhook_token"]
        if not isinstance(webhook_id, int) or isinstance(webhook_id, bool):
            return None
        if not isinstance(webhook_token, str) or not webhook_token:
            return None
        return webhook_id, webhook_token

    def _list_webhooks_sync(
        self, guild_id: int, channel_id: int
    ) -> list[tuple[str, int, str, str]]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return []
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return []
        conn = self._connection_sync()
        records: list[tuple[str, int, str, str]] = []
        for row in conn.execute(
            """
            SELECT agent_name, webhook_id, webhook_token, created_at
              FROM discord_agent_webhooks
             WHERE guild_id = ? AND channel_id = ?
             ORDER BY agent_name
            """,
            (guild_id, channel_id),
        ):
            agent_name = row["agent_name"]
            webhook_id = row["webhook_id"]
            webhook_token = row["webhook_token"]
            created_at = row["created_at"]
            if not isinstance(agent_name, str) or not agent_name:
                continue
            if not isinstance(webhook_id, int) or isinstance(webhook_id, bool):
                continue
            if not isinstance(webhook_token, str) or not webhook_token:
                continue
            if not isinstance(created_at, str) or not created_at:
                continue
            records.append((agent_name, webhook_id, webhook_token, created_at))
        return records

    # ------------------------------------------------------------------
    # Sync: dashboard
    # ------------------------------------------------------------------

    def _save_dashboard_sync(self, guild_id: int, channel_id: int, message_id: int) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                INSERT INTO discord_dashboard
                    (guild_id, channel_id, message_id, pinned_at, last_updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    message_id=excluded.message_id,
                    pinned_at=excluded.pinned_at,
                    last_updated_at=excluded.last_updated_at
                """,
                (guild_id, channel_id, message_id, now, now),
            )

    def _get_dashboard_sync(
        self, guild_id: int
    ) -> Optional[tuple[int, int, str, Optional[str]]]:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return None
        conn = self._connection_sync()
        row = conn.execute(
            """
            SELECT channel_id, message_id, pinned_at, last_updated_at
              FROM discord_dashboard
             WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        if row is None:
            return None
        channel_id = row["channel_id"]
        message_id = row["message_id"]
        pinned_at = row["pinned_at"]
        last_updated_at = row["last_updated_at"]
        if not isinstance(channel_id, int) or isinstance(channel_id, bool):
            return None
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return None
        if not isinstance(pinned_at, str) or not pinned_at:
            return None
        if last_updated_at is not None and not isinstance(last_updated_at, str):
            last_updated_at = None
        return channel_id, message_id, pinned_at, last_updated_at

    def _update_dashboard_timestamp_sync(self, guild_id: int) -> None:
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            return
        conn = self._connection_sync()
        now = now_iso()
        with conn:
            conn.execute(
                """
                UPDATE discord_dashboard
                   SET last_updated_at = ?
                 WHERE guild_id = ?
                """,
                (now, guild_id),
            )


# ---------------------------------------------------------------------------
# TopicRouter -- resolve topic key from Discord guild/channel/thread context
# ---------------------------------------------------------------------------


T = TypeVar("T")
_QUEUE_STOP = object()


class TopicQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._worker: Optional[asyncio.Task[None]] = None
        self._closed = False
        self._current_task: Optional[asyncio.Task[Any]] = None
        self._cancel_active_requested = False

    def pending(self) -> int:
        return self._queue.qsize()

    def cancel_active(self) -> bool:
        task = self._current_task
        if task is None or task.done():
            return False
        self._cancel_active_requested = True
        task.cancel()
        return True

    def cancel_pending(self) -> int:
        cancelled = 0
        requeue_stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if item is _QUEUE_STOP:
                    requeue_stop = True
                    continue
                work, future = cast(
                    tuple[Callable[[], Awaitable[Any]], asyncio.Future[Any]], item
                )
                if not future.done():
                    future.cancel()
                    cancelled += 1
            finally:
                self._queue.task_done()
        if requeue_stop:
            try:
                self._queue.put_nowait(_QUEUE_STOP)
            except asyncio.QueueFull:
                pass
        return cancelled

    async def enqueue(self, work: Callable[[], Awaitable[T]]) -> T:
        if self._closed:
            raise RuntimeError("topic queue is closed")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        await self._queue.put((work, future))
        self._ensure_worker()
        return await future

    async def close(self) -> None:
        self._closed = True
        if self._worker is None or self._worker.done():
            return
        await self._queue.put(_QUEUE_STOP)
        await self._worker

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _QUEUE_STOP:
                    return
                work, future = cast(
                    tuple[Callable[[], Awaitable[Any]], asyncio.Future[Any]], item
                )
                if future.cancelled():
                    continue
                try:
                    self._current_task = asyncio.create_task(work())
                    result: Any = await self._current_task
                except asyncio.CancelledError:
                    if self._cancel_active_requested:
                        self._cancel_active_requested = False
                        if not future.cancelled():
                            future.cancel()
                    else:
                        if (
                            self._current_task is not None
                            and not self._current_task.done()
                        ):
                            self._current_task.cancel()
                        raise
                except Exception as exc:
                    if not future.cancelled():
                        future.set_exception(exc)
                else:
                    if not future.cancelled():
                        future.set_result(result)
            finally:
                self._current_task = None
                self._cancel_active_requested = False
                self._queue.task_done()


@dataclass
class TopicRuntime:
    queue: TopicQueue = dataclasses.field(default_factory=TopicQueue)
    current_turn_id: Optional[str] = None
    current_turn_key: Optional[tuple[str, str]] = None
    pending_request_id: Optional[str] = None
    interrupt_requested: bool = False
    interrupt_message_id: Optional[int] = None
    interrupt_turn_id: Optional[str] = None
    queued_turn_cancel: Optional[asyncio.Event] = None


class TopicRouter:
    """Resolve and cache topic keys from Discord guild/channel/thread context."""

    def __init__(self, store: DiscordStateStore) -> None:
        self._store = store
        self._topics: dict[str, TopicRuntime] = {}

    def runtime_for(self, key: str) -> TopicRuntime:
        runtime = self._topics.get(key)
        if runtime is None:
            runtime = TopicRuntime()
            self._topics[key] = runtime
        return runtime

    async def resolve_key(
        self, guild_id: int, channel_id: int, thread_id: Optional[int] = None
    ) -> str:
        return topic_key(guild_id, channel_id, thread_id)
