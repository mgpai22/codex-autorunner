"""Pure Python reimplementation of the claude-code-controller filesystem protocol.

Provides file-based team config, inbox messaging, and task management
matching the ``~/.claude/teams/`` and ``~/.claude/tasks/`` layout used by
Claude Code's ``--teammate-mode``.

All file I/O runs on a single-thread ``ThreadPoolExecutor`` to avoid blocking
the asyncio event loop (same pattern as ``state.py``).
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import random
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("codex_autorunner.integrations.discord.swarm.protocol")

_CLAUDE_DIR = Path.home() / ".claude"
_LOCK_RETRIES = 5
_LOCK_MIN_BACKOFF_MS = 50
_LOCK_MAX_BACKOFF_MS = 500

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="swarm-protocol")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def teams_dir() -> Path:
    return _CLAUDE_DIR / "teams"


def team_dir(team_name: str) -> Path:
    return teams_dir() / team_name


def team_config_path(team_name: str) -> Path:
    return team_dir(team_name) / "config.json"


def inboxes_dir(team_name: str) -> Path:
    return team_dir(team_name) / "inboxes"


def inbox_path(team_name: str, agent_name: str) -> Path:
    return inboxes_dir(team_name) / f"{agent_name}.json"


def tasks_base_dir() -> Path:
    return _CLAUDE_DIR / "tasks"


def tasks_dir(team_name: str) -> Path:
    return tasks_base_dir() / team_name


def task_path(team_name: str, task_id: str) -> Path:
    return tasks_dir(team_name) / f"{task_id}.json"


# ---------------------------------------------------------------------------
# File locking helper
# ---------------------------------------------------------------------------


def _with_file_lock(path: Path, fn: Callable[[], Any]) -> Any:
    """Execute *fn* while holding an exclusive ``flock`` on *path*.

    Retries up to ``_LOCK_RETRIES`` times with randomised back-off on
    failure, matching the behaviour of ``proper-lockfile`` in the reference
    Node.js implementation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("[]", encoding="utf-8")

    last_exc: Optional[Exception] = None
    for attempt in range(_LOCK_RETRIES):
        fd: Optional[int] = None
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = fn()
            return result
        except (BlockingIOError, OSError) as exc:
            last_exc = exc
            backoff_ms = random.randint(_LOCK_MIN_BACKOFF_MS, _LOCK_MAX_BACKOFF_MS)
            time.sleep(backoff_ms / 1000.0)
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass

    raise RuntimeError(
        f"Failed to acquire file lock on {path} after {_LOCK_RETRIES} attempts"
    ) from last_exc


# ---------------------------------------------------------------------------
# Team config
# ---------------------------------------------------------------------------


def _write_team_config_sync(
    team_name: str,
    *,
    description: str = "",
    lead_agent_id: str = "",
    lead_session_id: str = "",
    members: Optional[list[dict[str, Any]]] = None,
) -> None:
    config = {
        "name": team_name,
        "description": description,
        "createdAt": int(time.time() * 1000),
        "leadAgentId": lead_agent_id,
        "leadSessionId": lead_session_id,
        "members": members or [],
    }
    path = team_config_path(team_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _read_team_config_sync(team_name: str) -> Optional[dict[str, Any]]:
    path = team_config_path(team_name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _add_member_sync(team_name: str, member: dict[str, Any]) -> None:
    config = _read_team_config_sync(team_name)
    if config is None:
        raise RuntimeError(f"Team config not found for {team_name}")
    members = config.get("members", [])
    if not isinstance(members, list):
        members = []
    # Remove existing member with same name if present
    agent_id = member.get("agentId", "")
    members = [m for m in members if m.get("agentId") != agent_id]
    members.append(member)
    config["members"] = members
    path = team_config_path(team_name)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Inbox operations
# ---------------------------------------------------------------------------


def _write_inbox_sync(
    team_name: str,
    agent_name: str,
    *,
    from_name: str,
    text: str,
    summary: str = "",
) -> None:
    path = inbox_path(team_name, agent_name)

    def _do_write() -> None:
        messages: list[dict[str, Any]] = []
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
                parsed = json.loads(raw) if raw.strip() else []
                if isinstance(parsed, list):
                    messages = parsed
            except (json.JSONDecodeError, OSError):
                messages = []
        messages.append(
            {
                "from": from_name,
                "text": text,
                "timestamp": _iso_now(),
                "summary": summary,
                "read": False,
            }
        )
        path.write_text(json.dumps(messages, indent=2) + "\n", encoding="utf-8")

    _with_file_lock(path, _do_write)


def _read_inbox_sync(team_name: str, agent_name: str) -> list[dict[str, Any]]:
    path = inbox_path(team_name, agent_name)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else []
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _list_inbox_names_sync(team_name: str) -> list[str]:
    """Return all inbox names (without .json suffix) in the team's inboxes dir."""
    d = inboxes_dir(team_name)
    if not d.exists():
        return []
    return [
        p.stem for p in sorted(d.glob("*.json")) if p.is_file()
    ]


def _peek_unread_sync(team_name: str, agent_name: str) -> list[dict[str, Any]]:
    """Return unread messages WITHOUT marking them as read.

    Used by the controller to observe agent-to-agent messages for Discord
    routing without consuming them (agents still need to read them).
    """
    path = inbox_path(team_name, agent_name)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        messages = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(messages, list):
        return []
    return [dict(m) for m in messages if isinstance(m, dict) and not m.get("read", True)]


def _mark_messages_read_sync(team_name: str, agent_name: str, count: int) -> None:
    """Mark the first *count* unread messages as read."""
    path = inbox_path(team_name, agent_name)
    if not path.exists():
        return

    def _do_mark() -> None:
        try:
            raw = path.read_text(encoding="utf-8")
            messages = json.loads(raw) if raw.strip() else []
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(messages, list):
            return
        marked = 0
        changed = False
        for msg in messages:
            if marked >= count:
                break
            if isinstance(msg, dict) and not msg.get("read", True):
                msg["read"] = True
                marked += 1
                changed = True
        if changed:
            path.write_text(
                json.dumps(messages, indent=2) + "\n", encoding="utf-8"
            )

    _with_file_lock(path, _do_mark)


def _read_unread_sync(team_name: str, agent_name: str) -> list[dict[str, Any]]:
    """Read all unread messages and mark them as read atomically."""
    path = inbox_path(team_name, agent_name)
    if not path.exists():
        return []

    unread: list[dict[str, Any]] = []

    def _do_read_unread() -> None:
        nonlocal unread
        try:
            raw = path.read_text(encoding="utf-8")
            messages = json.loads(raw) if raw.strip() else []
        except (json.JSONDecodeError, OSError):
            messages = []
        if not isinstance(messages, list):
            return
        changed = False
        for msg in messages:
            if isinstance(msg, dict) and not msg.get("read", True):
                unread.append(dict(msg))
                msg["read"] = True
                changed = True
        if changed:
            path.write_text(
                json.dumps(messages, indent=2) + "\n", encoding="utf-8"
            )

    _with_file_lock(path, _do_read_unread)
    return unread


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


def _create_task_sync(
    team_name: str,
    *,
    subject: str,
    description: str,
    active_form: str = "",
    owner: str = "",
    status: str = "pending",
    blocks: Optional[list[str]] = None,
    blocked_by: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    task_id = str(uuid.uuid4())[:8]
    task: dict[str, Any] = {
        "id": task_id,
        "subject": subject,
        "description": description,
        "activeForm": active_form,
        "owner": owner,
        "status": status,
        "blocks": blocks or [],
        "blockedBy": blocked_by or [],
        "metadata": metadata or {},
    }
    path = task_path(team_name, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    return task_id


def _get_task_sync(team_name: str, task_id: str) -> Optional[dict[str, Any]]:
    path = task_path(team_name, task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _update_task_sync(
    team_name: str,
    task_id: str,
    updates: dict[str, Any],
) -> Optional[dict[str, Any]]:
    task = _get_task_sync(team_name, task_id)
    if task is None:
        return None
    task.update(updates)
    path = task_path(team_name, task_id)
    path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
    return task


def _list_tasks_sync(team_name: str) -> list[dict[str, Any]]:
    d = tasks_dir(team_name)
    if not d.exists():
        return []
    results: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                results.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return results


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _cleanup_team_sync(team_name: str) -> None:
    td = team_dir(team_name)
    if td.exists():
        shutil.rmtree(td, ignore_errors=True)
    tkd = tasks_dir(team_name)
    if tkd.exists():
        shutil.rmtree(tkd, ignore_errors=True)


# ---------------------------------------------------------------------------
# ISO timestamp helper
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Async wrappers
# ---------------------------------------------------------------------------


async def _run(fn: Callable[..., Any], *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn, *args)


async def write_team_config(
    team_name: str,
    *,
    description: str = "",
    lead_agent_id: str = "",
    lead_session_id: str = "",
    members: Optional[list[dict[str, Any]]] = None,
) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _executor,
        lambda: _write_team_config_sync(
            team_name,
            description=description,
            lead_agent_id=lead_agent_id,
            lead_session_id=lead_session_id,
            members=members,
        ),
    )


async def read_team_config(team_name: str) -> Optional[dict[str, Any]]:
    return await _run(_read_team_config_sync, team_name)


async def add_member(team_name: str, member: dict[str, Any]) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _executor, lambda: _add_member_sync(team_name, member)
    )


async def write_inbox(
    team_name: str,
    agent_name: str,
    *,
    from_name: str,
    text: str,
    summary: str = "",
) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _executor,
        lambda: _write_inbox_sync(
            team_name, agent_name, from_name=from_name, text=text, summary=summary
        ),
    )


async def read_inbox(team_name: str, agent_name: str) -> list[dict[str, Any]]:
    return await _run(_read_inbox_sync, team_name, agent_name)


async def list_inbox_names(team_name: str) -> list[str]:
    return await _run(_list_inbox_names_sync, team_name)


async def peek_unread(team_name: str, agent_name: str) -> list[dict[str, Any]]:
    """Return unread messages WITHOUT marking them as read."""
    return await _run(_peek_unread_sync, team_name, agent_name)


async def mark_messages_read(team_name: str, agent_name: str, count: int) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _executor, lambda: _mark_messages_read_sync(team_name, agent_name, count)
    )


async def read_unread(team_name: str, agent_name: str) -> list[dict[str, Any]]:
    return await _run(_read_unread_sync, team_name, agent_name)


async def create_task(
    team_name: str,
    *,
    subject: str,
    description: str,
    active_form: str = "",
    owner: str = "",
    status: str = "pending",
    blocks: Optional[list[str]] = None,
    blocked_by: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor,
        lambda: _create_task_sync(
            team_name,
            subject=subject,
            description=description,
            active_form=active_form,
            owner=owner,
            status=status,
            blocks=blocks,
            blocked_by=blocked_by,
            metadata=metadata,
        ),
    )


async def get_task(team_name: str, task_id: str) -> Optional[dict[str, Any]]:
    return await _run(_get_task_sync, team_name, task_id)


async def update_task(
    team_name: str,
    task_id: str,
    updates: dict[str, Any],
) -> Optional[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor, lambda: _update_task_sync(team_name, task_id, updates)
    )


async def list_tasks(team_name: str) -> list[dict[str, Any]]:
    return await _run(_list_tasks_sync, team_name)


async def cleanup_team(team_name: str) -> None:
    return await _run(_cleanup_team_sync, team_name)
