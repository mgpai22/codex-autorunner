from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from ...core.logging_utils import log_event
from ...workspace import canonical_workspace_root, workspace_id_for_path
from ..app_server.client import CodexAppServerClient
from ..app_server.env import build_app_server_env
from .config import AppServerUnavailableError
from .constants import (
    APP_SERVER_START_BACKOFF_INITIAL_SECONDS,
    APP_SERVER_START_BACKOFF_MAX_SECONDS,
)


class DiscordRuntimeHelpers:
    """Mixin providing workspace/client resolution helpers for the Discord bot service."""

    async def _resolve_topic_key(
        self, guild_id: int, channel_id: int, thread_id: Optional[int]
    ) -> str:
        return await self._router.resolve_key(guild_id, channel_id, thread_id)

    def _canonical_workspace_root(
        self, workspace_path: Optional[str]
    ) -> Optional[Path]:
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            return None
        try:
            return canonical_workspace_root(Path(workspace_path))
        except Exception:
            return None

    def _workspace_id_for_path(self, workspace_path: Optional[str]) -> Optional[str]:
        root = self._canonical_workspace_root(workspace_path)
        if root is None:
            return None
        return workspace_id_for_path(root)

    def _build_workspace_env(
        self, workspace_root: Path, workspace_id: str, state_dir: Path
    ) -> dict[str, str]:
        return build_app_server_env(
            self._config.app_server_command,
            workspace_root,
            state_dir,
            logger=self._logger,
            event_prefix="discord",
        )

    async def _client_for_workspace(
        self, workspace_path: Optional[str]
    ) -> Optional[CodexAppServerClient]:
        workspace_root = self._canonical_workspace_root(workspace_path)
        if workspace_root is None:
            return None
        delay = APP_SERVER_START_BACKOFF_INITIAL_SECONDS
        timeout = self._config.app_server_start_timeout_seconds
        max_attempts = self._config.app_server_start_max_attempts
        started_at = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            if max_attempts is not None and attempt > max_attempts:
                raise AppServerUnavailableError(
                    f"App-server unavailable after {max_attempts} attempts"
                )
            try:
                return await self._app_server_supervisor.get_client(workspace_root)
            except Exception as exc:
                self._log_app_server_start_failure(workspace_root, exc)
                elapsed = time.monotonic() - started_at
                if elapsed >= timeout:
                    raise AppServerUnavailableError(
                        f"App-server unavailable after {timeout:.1f}s"
                    ) from exc
                sleep_time = min(delay, timeout - elapsed)
                await asyncio.sleep(sleep_time)
                delay = min(delay * 2, APP_SERVER_START_BACKOFF_MAX_SECONDS)

    def _log_app_server_start_failure(
        self, workspace_root: Path, exc: Exception
    ) -> None:
        log_event(
            self._logger,
            logging.WARNING,
            "discord.app_server.start_failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
