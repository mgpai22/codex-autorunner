from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

from ...core.logging_utils import log_event
from ...core.state import now_iso
from .constants import (
    OUTBOX_IMMEDIATE_RETRY_DELAYS,
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_RETRY_INTERVAL_SECONDS,
)

logger = logging.getLogger("codex_autorunner.integrations.discord.outbox")


class DiscordOutboxManager:
    """Reliable message delivery queue with retry and coalescing.

    Mirrors TelegramOutboxManager pattern: immediate delivery with retries,
    background flush loop for persistent failures, and DB-backed storage for
    restart recovery.
    """

    def __init__(
        self,
        store: Any,
        *,
        send_message: Callable[..., Awaitable[Optional[int]]],
        edit_message: Callable[..., Awaitable[bool]],
        delete_message: Callable[..., Awaitable[bool]],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._store = store
        self._send_message = send_message
        self._edit_message = edit_message
        self._delete_message = delete_message
        self._logger = logger or logging.getLogger(__name__)
        self._running = False
        self._loop_task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()

    async def send_message_with_outbox(
        self,
        *,
        channel_id: int,
        text: Optional[str] = None,
        embed: Optional[Any] = None,
        view: Optional[Any] = None,
        thread_id: Optional[int] = None,
        outbox_key: Optional[str] = None,
    ) -> Optional[int]:
        """Send a message immediately, falling back to outbox on failure."""
        record_id = str(uuid.uuid4())
        record = {
            "id": record_id,
            "channel_id": channel_id,
            "thread_id": thread_id,
            "text": text,
            "outbox_key": outbox_key,
            "created_at": now_iso(),
        }

        # Try immediate delivery with retries
        for delay in OUTBOX_IMMEDIATE_RETRY_DELAYS:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                msg_id = await self._send_message(
                    channel_id, text, embed=embed, view=view, thread_id=thread_id
                )
                if msg_id is not None:
                    return msg_id
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.outbox.immediate_retry",
                    record_id=record_id,
                    delay=delay,
                    exc=exc,
                )

        # Persist to outbox for background retry
        try:
            await self._store.enqueue_outbox(record_id, json.dumps(record))
            log_event(
                self._logger,
                logging.INFO,
                "discord.outbox.enqueued",
                record_id=record_id,
                channel_id=channel_id,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.outbox.enqueue_failed",
                record_id=record_id,
                exc=exc,
            )
        return None

    async def run_loop(self) -> None:
        """Background loop that flushes pending outbox records."""
        while self._running:
            try:
                await self._flush_pending()
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.outbox.flush_error",
                    exc=exc,
                )
            await asyncio.sleep(OUTBOX_RETRY_INTERVAL_SECONDS)

    async def _flush_pending(self) -> None:
        records = await self._store.list_pending_outbox()
        for record_row in records:
            record_id = record_row.get("outbox_id") or record_row.get("id")
            attempts = record_row.get("attempts", 0)
            if attempts >= OUTBOX_MAX_ATTEMPTS:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.outbox.max_attempts",
                    record_id=record_id,
                    attempts=attempts,
                )
                await self._store.delete_outbox_item(record_id)
                continue

            data_str = record_row.get("data", "{}")
            try:
                data = json.loads(data_str) if isinstance(data_str, str) else data_str
            except (json.JSONDecodeError, TypeError):
                await self._store.delete_outbox_item(record_id)
                continue

            channel_id = data.get("channel_id")
            text = data.get("text")
            thread_id = data.get("thread_id")
            if channel_id is None:
                await self._store.delete_outbox_item(record_id)
                continue

            try:
                msg_id = await self._send_message(channel_id, text, thread_id=thread_id)
                if msg_id is not None:
                    await self._store.delete_outbox_item(record_id)
                    log_event(
                        self._logger,
                        logging.INFO,
                        "discord.outbox.delivered",
                        record_id=record_id,
                        attempts=attempts + 1,
                    )
                else:
                    await self._store.mark_outbox_sent(record_id)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "discord.outbox.retry_failed",
                    record_id=record_id,
                    attempts=attempts + 1,
                    exc=exc,
                )
                await self._store.mark_outbox_sent(record_id)

    async def restore(self) -> None:
        """Recover pending outbox entries from DB on startup."""
        try:
            records = await self._store.list_pending_outbox()
            if records:
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.outbox.restore",
                    pending_count=len(records),
                )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.outbox.restore_failed",
                exc=exc,
            )
