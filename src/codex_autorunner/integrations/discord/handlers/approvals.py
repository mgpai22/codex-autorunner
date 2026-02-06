from __future__ import annotations

import asyncio
import logging
from typing import Any

from ....core.logging_utils import log_event
from ....core.state import now_iso
from ...app_server.client import ApprovalDecision
from ..adapter import build_approval_view
from ..config import DEFAULT_APPROVAL_TIMEOUT_SECONDS
from ..state import PendingApprovalRecord
from ..types import PendingApproval

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.approvals")


def _format_approval_prompt(message: dict[str, Any]) -> str:
    """Format an approval request into a human-readable prompt."""
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    tool_name = params.get("toolName", "unknown tool")
    description = params.get("description", "")
    args = params.get("arguments", {})

    lines = [f"**Approval Required**: `{tool_name}`"]
    if description:
        lines.append(description[:500])
    if isinstance(args, dict):
        for key, value in list(args.items())[:5]:
            val_str = str(value)[:200]
            lines.append(f"  `{key}`: {val_str}")
    return "\n".join(lines)


class DiscordApprovalHandlers:
    """Mixin providing approval request handling for the Discord bot service."""

    async def _restore_pending_approvals(self) -> None:
        """Clear stale approval requests from a previous session."""
        try:
            records = await self._store.list_pending_approvals()
        except Exception:
            return
        if not records:
            return
        for record in records:
            await self._store.delete_pending_approval(record.request_id)
        log_event(
            self._logger,
            logging.INFO,
            "discord.approval.restore",
            cleared_count=len(records),
        )

    async def _handle_approval_request(
        self, message: dict[str, Any]
    ) -> ApprovalDecision:
        """Handle an approval request from the app server.

        Creates a Discord message with approval buttons, then blocks
        on an asyncio.Future until the user clicks a button or timeout.
        """
        req_id = message.get("id")
        params = (
            message.get("params") if isinstance(message.get("params"), dict) else {}
        )
        turn_id = params.get("turnId")
        if not req_id or not turn_id:
            return "cancel"

        codex_thread_id = params.get("threadId")

        # Find the turn context
        ctx = None
        turn_id_str = str(turn_id)
        for tk, tc in self._turn_contexts.items():
            if tk[1] == turn_id_str or tc.codex_thread_id == codex_thread_id:
                ctx = tc
                break

        if ctx is None and len(self._turn_contexts) == 1:
            ctx = next(iter(self._turn_contexts.values()))

        if ctx is None:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.approval.no_context",
                turn_id=turn_id,
            )
            return "cancel"

        request_id = str(req_id)
        prompt = _format_approval_prompt(message)
        created_at = now_iso()

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()

        pending = PendingApproval(
            request_id=request_id,
            turn_id=turn_id_str,
            codex_thread_id=codex_thread_id,
            guild_id=ctx.guild_id,
            channel_id=ctx.channel_id,
            thread_id=ctx.thread_id,
            topic_key=ctx.topic_key,
            message_id=None,
            created_at=created_at,
            future=future,
        )

        self._pending_approvals[request_id] = pending

        # Send approval message with buttons
        target_id = ctx.thread_id or ctx.channel_id
        view = build_approval_view(request_id)
        try:
            msg_id = await self._send_message(target_id, prompt, view=view)
            pending.message_id = msg_id
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.approval.send_failed",
                request_id=request_id,
                exc=exc,
            )
            self._pending_approvals.pop(request_id, None)
            return "cancel"

        # Persist to DB for restart recovery
        try:
            approval_record = PendingApprovalRecord(
                request_id=request_id,
                turn_id=turn_id_str,
                guild_id=ctx.guild_id,
                channel_id=ctx.channel_id,
                thread_id=ctx.thread_id,
                message_id=pending.message_id,
                prompt=prompt,
                created_at=created_at,
                topic_key=ctx.topic_key,
            )
            await self._store.save_pending_approval(approval_record)
        except Exception:
            pass

        # Block until button click or timeout
        try:
            result = await asyncio.wait_for(
                future, timeout=DEFAULT_APPROVAL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            result = "cancel"
            log_event(
                self._logger,
                logging.INFO,
                "discord.approval.timeout",
                request_id=request_id,
            )
        finally:
            self._pending_approvals.pop(request_id, None)
            try:
                await self._store.delete_pending_approval(request_id)
            except Exception:
                pass

        return result
