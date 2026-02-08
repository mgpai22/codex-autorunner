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

    parts = custom_id.split(":")
    kind = parts[0] if parts else ""

    try:
        if kind == "task":
            # Format: task:{action}:{guild_id}:{thread_id}
            if len(parts) >= 4:
                action = parts[1]
                guild_raw = parts[2]
                thread_raw = parts[3]
                try:
                    guild_id = int(guild_raw)
                    thread_id = int(thread_raw)
                except (TypeError, ValueError):
                    return
                await service._handle_task_button(
                    interaction, action, guild_id, thread_id
                )
            return

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
            or ":select:" in custom_id
            or ":page:" in custom_id
        ):
            await _handle_selection_interaction(service, interaction, custom_id)
            return

        if kind == "wscreate":
            await service._handle_workspace_create_interaction(interaction, custom_id)
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

    if len(parts) >= 2 and parts[0] == "wscreate":
        await service._handle_workspace_create_modal(interaction, custom_id)
        return


async def _handle_selection_interaction(
    service: "DiscordBotService", interaction: Any, custom_id: str
) -> None:
    """Handle a selection menu interaction (pagination, choice).

    Custom ID formats from build_selection_view:
    - ``{kind}:select:{page}`` — dropdown value selected
    - ``{kind}:page:{page_num}`` — pagination button
    - ``{kind}:cancel`` — cancel button
    """
    parts = custom_id.split(":")
    if len(parts) < 2:
        return

    kind = parts[0]
    action = parts[1]

    # Resolve the topic key from the interaction context
    try:
        import discord as _discord

        guild_id = interaction.guild_id
        channel = interaction.channel
        if isinstance(channel, _discord.Thread):
            channel_id = channel.parent_id
            thread_id = channel.id
        else:
            channel_id = channel.id
            thread_id = None

        from ..helpers import build_topic_key

        topic_key = build_topic_key(guild_id, channel_id, thread_id)
    except Exception:
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        return

    # Route based on action
    if action == "cancel":
        # Clear the relevant state dict and acknowledge
        _clear_selection_state(service, kind, topic_key)
        try:
            await interaction.response.edit_message(
                content=f"{kind.title()} selection cancelled.", view=None
            )
        except Exception:
            try:
                await interaction.response.send_message(
                    "Selection cancelled.", ephemeral=True
                )
            except Exception:
                pass
        return

    if action == "page" and len(parts) >= 3:
        # Pagination — rebuild the view for the requested page
        try:
            page = int(parts[2])
        except (ValueError, IndexError):
            page = 0

        state = _get_selection_state(service, kind, topic_key)
        if state is None:
            try:
                await interaction.response.send_message(
                    "Selection expired.", ephemeral=True
                )
            except Exception:
                pass
            return

        await service._handle_selection_page(
            interaction, state, page, kind=kind, prompt=f"Select ({kind}):"
        )
        return

    if action == "select":
        # User picked a value from the dropdown
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass
            return

        selected_value = values[0]

        # Route the selection to the appropriate handler based on kind
        await _dispatch_selection_choice(
            service, interaction, kind, topic_key, selected_value
        )
        return

    # Unknown action — just defer
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass


def _get_selection_state(
    service: "DiscordBotService", kind: str, topic_key: str
) -> Any:
    """Look up the in-memory selection state for the given kind."""
    state_maps: dict[str, dict[str, Any]] = {
        "model": service._model_options,
        "agent": service._agent_options,
        "resume": service._resume_options,
        "bind": service._bind_options,
    }
    state_map = state_maps.get(kind)
    if state_map is None:
        return None
    return state_map.get(topic_key)


def _clear_selection_state(
    service: "DiscordBotService", kind: str, topic_key: str
) -> None:
    """Remove the in-memory selection state for the given kind."""
    state_maps: dict[str, dict[str, Any]] = {
        "model": service._model_options,
        "agent": service._agent_options,
        "resume": service._resume_options,
        "bind": service._bind_options,
    }
    state_map = state_maps.get(kind)
    if state_map is not None:
        state_map.pop(topic_key, None)
    # Also clear secondary model state
    if kind == "model":
        service._model_pending.pop(topic_key, None)


async def _dispatch_selection_choice(
    service: "DiscordBotService",
    interaction: Any,
    kind: str,
    topic_key: str,
    selected_value: str,
) -> None:
    """Dispatch a selection choice to the appropriate handler based on kind."""
    from ....core.state import now_iso
    from ..constants import DEFAULT_AGENT_MODELS

    if kind == "agent":
        from ..state import normalize_agent

        normalized = normalize_agent(selected_value)
        if normalized is None:
            try:
                await interaction.response.send_message(
                    f"Unknown agent: {selected_value}", ephemeral=True
                )
            except Exception:
                pass
            return

        def apply_agent(record: Any) -> None:
            record.agent = normalized
            record.model = DEFAULT_AGENT_MODELS.get(normalized)
            record.codex_thread_id = None
            record.reasoning_effort = None
            record.updated_at = now_iso()

        await service._store.update_topic_field(topic_key, apply_agent)
        _clear_selection_state(service, kind, topic_key)
        try:
            await interaction.response.edit_message(
                content=f"Agent set to `{normalized}`. Thread reset.", view=None
            )
        except Exception:
            pass
        return

    if kind == "model":

        def apply_model(record: Any) -> None:
            record.model = selected_value
            record.updated_at = now_iso()

        await service._store.update_topic_field(topic_key, apply_model)
        _clear_selection_state(service, kind, topic_key)
        try:
            await interaction.response.edit_message(
                content=f"Model set to `{selected_value}`. Will apply on the next turn.",
                view=None,
            )
        except Exception:
            pass
        return

    if kind == "resume":

        def apply_resume(record: Any) -> None:
            record.codex_thread_id = selected_value
            record.updated_at = now_iso()

        await service._store.update_topic_field(topic_key, apply_resume)
        _clear_selection_state(service, kind, topic_key)
        try:
            await interaction.response.edit_message(
                content=f"Resumed thread `{selected_value}`.", view=None
            )
        except Exception:
            pass
        return

    if kind == "bind":
        guild_id = interaction.guild_id
        channel = interaction.channel
        import discord as _discord

        if isinstance(channel, _discord.Thread):
            channel_id = channel.parent_id
        else:
            channel_id = channel.id
        channel_key = f"{guild_id}:{channel_id}"
        await service._store.set_channel_binding(channel_key, selected_value)
        _clear_selection_state(service, kind, topic_key)
        try:
            await interaction.response.edit_message(
                content=f"Bound to `{selected_value}`.", view=None
            )
        except Exception:
            pass
        return

    # Unknown kind — just acknowledge
    _clear_selection_state(service, kind, topic_key)
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass
