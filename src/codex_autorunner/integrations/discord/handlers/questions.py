from __future__ import annotations

import asyncio
import logging
from typing import Any, Union

from ....core.logging_utils import log_event
from ....core.state import now_iso
from ..adapter import build_question_view
from ..config import DEFAULT_APPROVAL_TIMEOUT_SECONDS
from ..types import PendingQuestion

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.questions")


def _extract_question_text(question: dict[str, Any]) -> str:
    for key in ("text", "prompt", "title", "label", "question"):
        value = question.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Question"


def _extract_question_options(
    question: dict[str, Any],
) -> tuple[list[str], bool, bool]:
    multiple = bool(question.get("multiple"))
    custom = question.get("custom", True)
    for key in ("options", "choices"):
        raw = question.get(key)
        if isinstance(raw, list):
            options: list[str] = []
            for option in raw:
                if isinstance(option, str) and option.strip():
                    options.append(option.strip())
                    continue
                if isinstance(option, dict):
                    for label_key in ("label", "text", "value", "name", "id"):
                        value = option.get(label_key)
                        if isinstance(value, str) and value.strip():
                            options.append(value.strip())
                            break
            return options, multiple, custom
    return [], multiple, custom


def _format_question_prompt(question: dict[str, Any], *, index: int, total: int) -> str:
    title = _extract_question_text(question)
    if total > 1:
        prefix = f"Question {index + 1} of {total}"
    else:
        prefix = "Question"
    return f"**{prefix}**:\n{title}"


class DiscordQuestionHandlers:
    """Mixin providing question handling for the Discord bot service."""

    async def _handle_question_request(
        self,
        message: dict[str, Any],
    ) -> Union[list[int], str, None]:
        """Handle a question request from the app server.

        Sends a Discord message with Select menu and Other button,
        then blocks on an asyncio.Future.
        """
        params = (
            message.get("params") if isinstance(message.get("params"), dict) else {}
        )
        turn_id = params.get("turnId")
        req_id = message.get("id")
        if not req_id or not turn_id:
            return None

        codex_thread_id = params.get("threadId")
        questions = params.get("questions", [])
        if not isinstance(questions, list) or not questions:
            return None

        # Find turn context
        ctx = None
        turn_id_str = str(turn_id)
        for tk, tc in self._turn_contexts.items():
            if tk[1] == turn_id_str or tc.codex_thread_id == codex_thread_id:
                ctx = tc
                break
        if ctx is None and len(self._turn_contexts) == 1:
            ctx = next(iter(self._turn_contexts.values()))
        if ctx is None:
            return None

        # Process each question sequentially
        all_answers: list[Any] = []
        for i, question in enumerate(questions):
            if not isinstance(question, dict):
                all_answers.append(None)
                continue

            request_id = f"{req_id}_{i}"
            prompt_text = _format_question_prompt(
                question, index=i, total=len(questions)
            )
            options, multiple, custom = _extract_question_options(question)

            loop = asyncio.get_running_loop()
            future: asyncio.Future[Union[list[int], str, None]] = loop.create_future()

            pending = PendingQuestion(
                request_id=request_id,
                turn_id=turn_id_str,
                codex_thread_id=codex_thread_id,
                guild_id=ctx.guild_id,
                channel_id=ctx.channel_id,
                thread_id=ctx.thread_id,
                topic_key=ctx.topic_key,
                message_id=None,
                created_at=now_iso(),
                question_index=i,
                prompt=prompt_text,
                options=options,
                future=future,
                multiple=multiple,
                custom=custom,
            )
            self._pending_questions[request_id] = pending

            target_id = ctx.thread_id or ctx.channel_id
            view = build_question_view(request_id, options, multiple=multiple)
            try:
                msg_id = await self._send_message(target_id, prompt_text, view=view)
                pending.message_id = msg_id
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.ERROR,
                    "discord.question.send_failed",
                    request_id=request_id,
                    exc=exc,
                )
                self._pending_questions.pop(request_id, None)
                all_answers.append(None)
                continue

            try:
                result = await asyncio.wait_for(
                    future, timeout=DEFAULT_APPROVAL_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                result = None
                log_event(
                    self._logger,
                    logging.INFO,
                    "discord.question.timeout",
                    request_id=request_id,
                )
            finally:
                self._pending_questions.pop(request_id, None)

            all_answers.append(result)

        # Return single answer for single question, list for multiple
        if len(all_answers) == 1:
            return all_answers[0]
        return all_answers
