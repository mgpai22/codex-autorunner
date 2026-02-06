from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from ...core.request_context import reset_conversation_id, set_conversation_id
from ...core.state_roots import resolve_global_state_root
from ...core.utils import (
    RepoNotFoundError,
    canonicalize_path,
    find_repo_root,
    is_within,
)
from .constants import THREAD_NAME_MAX_LEN

# ---------------------------------------------------------------------------
# Shared data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelOption:
    model_id: str
    label: str
    efforts: tuple[str, ...]
    default_effort: Optional[str] = None


@dataclass(frozen=True)
class CodexFeatureRow:
    key: str
    stage: str
    enabled: bool


# ---------------------------------------------------------------------------
# Topic key helpers  (guild:channel:thread_id|root)
# ---------------------------------------------------------------------------


def build_topic_key(
    guild_id: int, channel_id: int, thread_id: Optional[int] = None
) -> str:
    """Build a topic key from Discord IDs.

    Format: ``'{guild_id}:{channel_id}:{thread_id_or_root}'``.
    """
    tid = str(thread_id) if thread_id is not None else "root"
    return f"{guild_id}:{channel_id}:{tid}"


def split_topic_key(key: str) -> tuple[int, int, Optional[int]]:
    """Parse a topic key back into ``(guild_id, channel_id, thread_id_or_None)``."""
    parts = key.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid topic key: {key}")
    guild_id = int(parts[0])
    channel_id = int(parts[1])
    thread_id = int(parts[2]) if parts[2] != "root" else None
    return guild_id, channel_id, thread_id


# ---------------------------------------------------------------------------
# Text formatting utilities
# ---------------------------------------------------------------------------


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds into a human-readable string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs:.0f}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h {mins}m"


def truncate(text: str, max_len: int, suffix: str = "...") -> str:
    """Truncate *text* to *max_len*, appending *suffix* if truncated."""
    if len(text) <= max_len:
        return text
    return text[: max_len - len(suffix)] + suffix


def sanitize_thread_name(prompt: str) -> str:
    """Sanitize a prompt into a Discord thread name (max 100 chars)."""
    if not isinstance(prompt, str):
        prompt = str(prompt) if prompt is not None else ""
    text = prompt.strip()
    if not text:
        return "task"

    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "task"

    prefix = "task-"
    max_body = max(0, THREAD_NAME_MAX_LEN - len(prefix))
    body = text[:max_body].strip()
    if not body:
        return "task"
    return prefix + body


# ---------------------------------------------------------------------------
# Lock-file helpers
# ---------------------------------------------------------------------------


def _discord_lock_path(token: str) -> Path:
    """Return the path to the Discord bot instance lock file.

    Uses a hash of the bot token so multiple bots can coexist.
    """
    import hashlib

    if not isinstance(token, str) or not token:
        raise ValueError("token is required")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return resolve_global_state_root() / "locks" / f"discord_bot_{digest}.lock"


def _read_lock_payload(lock_path: Path) -> Optional[dict[str, Any]]:
    """Read JSON payload from a lock file.  Returns ``None`` if missing or invalid."""
    if not lock_path.exists():
        return None
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _lock_payload_summary(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Extract loggable fields from a lock-file payload."""
    if not isinstance(payload, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in ("pid", "started_at", "host", "cwd", "config_root"):
        if key in payload:
            summary[key] = payload.get(key)
    return summary


# ---------------------------------------------------------------------------
# Conversation-id context manager
# ---------------------------------------------------------------------------


@contextmanager
def _with_conversation_id(coro: Any, conversation_id: str):
    """Context manager that sets *conversation_id* in :mod:`request_context` for the
    duration of the block, then resets it.

    Usage::

        with _with_conversation_id(None, conversation_id):
            await some_operation()
    """
    token = set_conversation_id(conversation_id)
    try:
        yield
    finally:
        reset_conversation_id(token)


# ---------------------------------------------------------------------------
# Codex features helpers
# ---------------------------------------------------------------------------


def derive_codex_features_command(app_server_command: Sequence[str]) -> list[str]:
    """Build a Codex CLI invocation for ``features list``.

    Strips a trailing ``"app-server"`` subcommand (plus keeps any preceding
    flags/binary) so custom binaries or wrapper scripts stay aligned with the
    running app server.
    """
    base = list(app_server_command or [])
    if base and base[-1] == "app-server":
        base = base[:-1]
    if not base:
        base = ["codex"]
    return [*base, "features", "list"]


def parse_codex_features_list(stdout: str) -> list[CodexFeatureRow]:
    """Parse the tab-separated output of ``codex features list``."""
    rows: list[CodexFeatureRow] = []
    if not isinstance(stdout, str):
        return rows
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        key, stage, enabled_raw = parts
        key = key.strip()
        stage = stage.strip()
        enabled_raw = enabled_raw.strip().lower()
        if not key or not stage:
            continue
        if enabled_raw in ("true", "1", "yes", "y", "on"):
            enabled = True
        elif enabled_raw in ("false", "0", "no", "n", "off"):
            enabled = False
        else:
            continue
        rows.append(CodexFeatureRow(key=key, stage=stage, enabled=enabled))
    return rows


# ---------------------------------------------------------------------------
# Path utilities  (mirrors telegram/helpers.py)
# ---------------------------------------------------------------------------


def _path_within(root: Path, target: Path) -> bool:
    try:
        root = canonicalize_path(root)
        target = canonicalize_path(target)
    except Exception:
        return False
    return is_within(root, target)


def _repo_root(path: Path) -> Optional[Path]:
    try:
        return find_repo_root(path)
    except RepoNotFoundError:
        return None


def _paths_compatible(workspace_root: Path, resumed_root: Path) -> bool:
    if _path_within(workspace_root, resumed_root):
        return True
    if _path_within(resumed_root, workspace_root):
        workspace_repo = _repo_root(workspace_root)
        resumed_repo = _repo_root(resumed_root)
        if workspace_repo is None or resumed_repo is None:
            return False
        if workspace_repo != resumed_repo:
            return False
        return resumed_root == workspace_repo
    workspace_repo = _repo_root(workspace_root)
    resumed_repo = _repo_root(resumed_root)
    if workspace_repo is None or resumed_repo is None:
        return False
    if workspace_repo != resumed_repo:
        return False
    return _path_within(workspace_repo, resumed_root)
