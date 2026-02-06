from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ....core.logging_utils import log_event

if TYPE_CHECKING:
    from ..service import DiscordBotService

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.callbacks")


async def dispatch_component_interaction(
    service: "DiscordBotService", interaction: Any
) -> None:
    """Parse custom_id from a component interaction and route to the appropriate handler."""
    custom_id = ""
    if interaction.data:
        custom_id = interaction.data.get("custom_id", "")

    if not custom_id:
        return

    parts = custom_id.split(":", 2)
    kind = parts[0] if parts else ""

    try:
        if kind == "approval":
            # Format: approval:{request_id}:{decision}
            if len(parts) >= 3:
                request_id = parts[1]
                decision = parts[2]
                await _handle_approval_interaction(
                    service, interaction, request_id, decision
                )
            return

        if kind == "question":
            # Format: question:{request_id}:{action}
            if len(parts) >= 3:
                request_id = parts[1]
                action = parts[2]
                await _handle_question_interaction(
                    service, interaction, request_id, action
                )
            return

        if (
            kind == "selection"
            or custom_id.endswith(":select")
            or ":page:" in custom_id
        ):
            await _handle_selection_interaction(service, interaction, custom_id)
            return

        log_event(
            logger, logging.DEBUG, "discord.callback.unknown", custom_id=custom_id
        )

    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "discord.callback.error",
            custom_id=custom_id,
            exc=exc,
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "An error occurred.", ephemeral=True
                )
        except Exception:
            pass


async def _handle_approval_interaction(
    service: "DiscordBotService",
    interaction: Any,
    request_id: str,
    decision: str,
) -> None:
    """Handle an approval button click."""
    pending = service._pending_approvals.get(request_id)
    if pending is None:
        try:
            await interaction.response.send_message(
                "This approval has expired.", ephemeral=True
            )
        except Exception:
            pass
        return

    from ...app_server.client import ApprovalDecision

    decision_map: dict[str, ApprovalDecision] = {
        "accept": "approve",
        "accept_session": "approve_session",
        "decline": "reject",
        "cancel": "cancel",
    }
    mapped: ApprovalDecision = decision_map.get(decision, "cancel")

    if not pending.future.done():
        pending.future.set_result(mapped)

    service._pending_approvals.pop(request_id, None)

    try:
        await interaction.response.edit_message(
            content=f"Approval: **{decision}** by {interaction.user.display_name}",
            view=None,
        )
    except Exception:
        try:
            await interaction.response.send_message(
                f"Decision: {decision}", ephemeral=True
            )
        except Exception:
            pass

    log_event(
        logger,
        logging.INFO,
        "discord.approval.resolved",
        request_id=request_id,
        decision=decision,
        user_id=interaction.user.id if interaction.user else None,
    )


async def _handle_question_interaction(
    service: "DiscordBotService",
    interaction: Any,
    request_id: str,
    action: str,
) -> None:
    """Handle a question select/button/modal interaction."""
    pending = service._pending_questions.get(request_id)
    if pending is None:
        try:
            await interaction.response.send_message(
                "This question has expired.", ephemeral=True
            )
        except Exception:
            pass
        return

    if action == "select":
        values = interaction.data.get("values", [])
        indices = [int(v) for v in values if v.isdigit()]
        if pending.multiple:
            pending.selected_indices.update(indices)
            try:
                await interaction.response.send_message(
                    f"Selected {len(pending.selected_indices)} option(s). "
                    "Click Done when finished.",
                    ephemeral=True,
                )
            except Exception:
                pass
        else:
            if not pending.future.done():
                pending.future.set_result(indices)
            service._pending_questions.pop(request_id, None)
            try:
                await interaction.response.edit_message(content="Answered.", view=None)
            except Exception:
                pass
        return

    if action == "done":
        if not pending.future.done():
            pending.future.set_result(list(pending.selected_indices))
        service._pending_questions.pop(request_id, None)
        try:
            await interaction.response.edit_message(content="Answered.", view=None)
        except Exception:
            pass
        return

    if action == "other":
        from ..adapter import build_custom_input_modal

        modal = build_custom_input_modal(request_id)
        try:
            await interaction.response.send_modal(modal)
        except Exception as exc:
            log_event(logger, logging.WARNING, "discord.question.modal_failed", exc=exc)
        return

    if action == "cancel":
        if not pending.future.done():
            pending.future.set_result(None)
        service._pending_questions.pop(request_id, None)
        try:
            await interaction.response.edit_message(content="Cancelled.", view=None)
        except Exception:
            pass
        return


async def dispatch_modal_submit(service: "DiscordBotService", interaction: Any) -> None:
    """Handle modal form submission."""
    custom_id = interaction.data.get("custom_id", "") if interaction.data else ""
    if not custom_id:
        return

    parts = custom_id.split(":", 2)
    if len(parts) >= 2 and parts[0] == "question":
        request_id = parts[1]
        pending = service._pending_questions.get(request_id)
        if pending is None:
            try:
                await interaction.response.send_message(
                    "This question has expired.", ephemeral=True
                )
            except Exception:
                pass
            return

        # Extract text from modal components
        text = ""
        for component_row in interaction.data.get("components") or []:
            for component in component_row.get("components") or []:
                if component.get("value"):
                    text = component["value"]
                    break

        if text and not pending.future.done():
            pending.future.set_result(text)
        service._pending_questions.pop(request_id, None)
        try:
            await interaction.response.send_message(
                "Response recorded.", ephemeral=True
            )
        except Exception:
            pass
        return


async def _handle_selection_interaction(
    service: "DiscordBotService", interaction: Any, custom_id: str
) -> None:
    """Handle a selection menu interaction (pagination, choice)."""
    # Placeholder - will be wired to selection handlers
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass
