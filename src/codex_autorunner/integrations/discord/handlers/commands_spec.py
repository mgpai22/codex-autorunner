from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommandSpec:
    name: str
    description: str
    allow_during_turn: bool = False


def build_slash_command_specs() -> dict[str, SlashCommandSpec]:
    """Return the registry of all slash commands."""
    return {
        "setup": SlashCommandSpec(
            "setup", "Scaffold swarm control surface channels", allow_during_turn=True
        ),
        "run": SlashCommandSpec("run", "Run an agent task", allow_during_turn=False),
        "stop": SlashCommandSpec(
            "stop", "Stop the active task", allow_during_turn=True
        ),
        "bind": SlashCommandSpec("bind", "Bind this channel to a workspace"),
        "workspaces": SlashCommandSpec(
            "workspaces", "List scaffolded workspaces", allow_during_turn=True
        ),
        "tasks": SlashCommandSpec(
            "tasks", "Task triage and navigation", allow_during_turn=True
        ),
        "repos": SlashCommandSpec(
            "repos", "List available repositories", allow_during_turn=True
        ),
        "status": SlashCommandSpec(
            "status", "Show current configuration", allow_during_turn=True
        ),
        "new": SlashCommandSpec("new", "Start a new conversation"),
        "resume": SlashCommandSpec("resume", "Resume a previous conversation"),
        "model": SlashCommandSpec("model", "Show or change the model"),
        "agent": SlashCommandSpec("agent", "Show or change the agent"),
        "approvals": SlashCommandSpec("approvals", "Set approval and sandbox policy"),
        "review": SlashCommandSpec("review", "Run a code review"),
        "flow": SlashCommandSpec(
            "flow", "Ticket flow controls", allow_during_turn=True
        ),
        "compact": SlashCommandSpec(
            "compact", "Generate a summary of the conversation"
        ),
        "files": SlashCommandSpec(
            "files", "View inbox/outbox files", allow_during_turn=True
        ),
        "health": SlashCommandSpec(
            "health", "Run health diagnostics", allow_during_turn=True
        ),
        "workspace": SlashCommandSpec("workspace", "Workspace management", allow_during_turn=True),
    }
