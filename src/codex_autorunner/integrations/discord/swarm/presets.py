from __future__ import annotations

from typing import Any, Optional

from .types import SwarmAgentRole, SwarmPreset

_OPUS = "claude-opus-4-6"
_SONNET = "claude-sonnet-4-5-20250929"

SWARM_PRESETS: dict[str, SwarmPreset] = {
    "code-review": SwarmPreset(
        name="code-review",
        description="3-agent code review: lead orchestrator + security reviewer + quality reviewer",
        roles=(
            SwarmAgentRole(
                name="team-lead",
                model=_OPUS,
                is_lead=True,
                prompt_template=(
                    "You are the lead code reviewer for the following task:\n\n"
                    "{prompt}\n\n"
                    "Your teammates 'security-reviewer' and 'quality-reviewer' are working "
                    "in parallel on this same codebase. They will each send you their findings "
                    "via message. Wait for BOTH of them to report back, then synthesize all "
                    "findings into a final comprehensive review summary. Do not exit until you "
                    "have received and synthesized both reports. After you have synthesized, "
                    "send the final summary to the 'controller' teammate as a single message. "
                    "Do not exit until you have sent that message."
                ),
            ),
            SwarmAgentRole(
                name="security-reviewer",
                model=_SONNET,
                prompt_template=(
                    "You are a security reviewer. Your task:\n\n"
                    "{prompt}\n\n"
                    "Immediately begin reviewing the codebase for security issues. Focus on "
                    "OWASP top-10 vulnerabilities, auth bypasses, injection risks, and data "
                    "exposure. Do NOT wait for task assignment — start reviewing now. "
                    "When done, send your complete findings to the 'team-lead' teammate."
                ),
            ),
            SwarmAgentRole(
                name="quality-reviewer",
                model=_SONNET,
                prompt_template=(
                    "You are a quality reviewer. Your task:\n\n"
                    "{prompt}\n\n"
                    "Immediately begin reviewing the codebase for quality issues. Focus on "
                    "code quality, error handling, test coverage, naming conventions, and "
                    "maintainability. Do NOT wait for task assignment — start reviewing now. "
                    "When done, send your complete findings to the 'team-lead' teammate."
                ),
            ),
        ),
    ),
    "full-build": SwarmPreset(
        name="full-build",
        description="4-agent full build: lead + architect + implementer + tester",
        roles=(
            SwarmAgentRole(
                name="team-lead",
                model=_OPUS,
                is_lead=True,
                prompt_template=(
                    "You are the lead agent for the following task:\n\n"
                    "{prompt}\n\n"
                    "Your teammates are 'architect', 'implementer', and 'tester'. "
                    "The architect will send you a design, the implementer will build it, "
                    "and the tester will verify it. Coordinate the workflow: wait for the "
                    "architect's design first, then direct the implementer, then the tester. "
                    "Synthesize all results into a final summary. Do not exit until all "
                    "phases are complete. After you have synthesized, send the final summary "
                    "to the 'controller' teammate as a single message. Do not exit until you "
                    "have sent that message."
                ),
            ),
            SwarmAgentRole(
                name="architect",
                model=_OPUS,
                prompt_template=(
                    "You are the architect. Your task:\n\n"
                    "{prompt}\n\n"
                    "Immediately begin designing the solution architecture. Define interfaces, "
                    "data models, and an implementation plan. Do NOT wait for task assignment — "
                    "start designing now. When done, send your complete design to the 'team-lead' teammate."
                ),
            ),
            SwarmAgentRole(
                name="implementer",
                model=_SONNET,
                prompt_template=(
                    "You are the implementer. Your task:\n\n"
                    "{prompt}\n\n"
                    "Wait for the 'team-lead' to send you the architect's design, then implement it. "
                    "Write production-quality code following the design. Report progress and "
                    "any blockers to the 'team-lead' teammate."
                ),
            ),
            SwarmAgentRole(
                name="tester",
                model=_SONNET,
                prompt_template=(
                    "You are the tester. Your task:\n\n"
                    "{prompt}\n\n"
                    "Wait for the 'team-lead' to tell you implementation is ready, then write "
                    "comprehensive tests, run them, and verify the implementation meets "
                    "requirements. Report results to the 'team-lead' teammate."
                ),
            ),
        ),
    ),
    "research-deep": SwarmPreset(
        name="research-deep",
        description="3-agent deep research: lead + 2 researchers exploring different angles",
        roles=(
            SwarmAgentRole(
                name="team-lead",
                model=_OPUS,
                is_lead=True,
                prompt_template=(
                    "You are the research lead for the following question:\n\n"
                    "{prompt}\n\n"
                    "Your teammates 'researcher-1' and 'researcher-2' are each investigating "
                    "this from different angles in parallel. Wait for BOTH of them to send you "
                    "their findings, then synthesize a comprehensive answer. Do not exit until "
                    "you have received and synthesized both reports. After you have synthesized, "
                    "send the final answer to the 'controller' teammate as a single message. "
                    "Do not exit until you have sent that message."
                ),
            ),
            SwarmAgentRole(
                name="researcher-1",
                model=_SONNET,
                prompt_template=(
                    "You are researcher 1. Your research question:\n\n"
                    "{prompt}\n\n"
                    "Immediately begin investigating this question. Focus on code analysis, "
                    "reading relevant source files, and documenting what you find. "
                    "Do NOT wait for task assignment — start researching now. "
                    "When done, send your complete findings to the 'team-lead' teammate."
                ),
            ),
            SwarmAgentRole(
                name="researcher-2",
                model=_SONNET,
                prompt_template=(
                    "You are researcher 2. Your research question:\n\n"
                    "{prompt}\n\n"
                    "Immediately begin investigating this question. Focus on documentation, "
                    "configuration, dependencies, and external interfaces. "
                    "Do NOT wait for task assignment — start researching now. "
                    "When done, send your complete findings to the 'team-lead' teammate."
                ),
            ),
        ),
    ),
}


def get_preset(
    name: str,
    custom_presets: Optional[dict[str, Any]] = None,
) -> Optional[SwarmPreset]:
    """Look up a preset by name. Custom presets take precedence over built-ins."""
    if custom_presets and name in custom_presets:
        raw = custom_presets[name]
        if isinstance(raw, SwarmPreset):
            return raw
        if isinstance(raw, dict):
            return _parse_custom_preset(name, raw)
    return SWARM_PRESETS.get(name)


def list_presets(
    custom_presets: Optional[dict[str, Any]] = None,
) -> dict[str, SwarmPreset]:
    """Return all available presets (built-in + custom)."""
    merged: dict[str, SwarmPreset] = dict(SWARM_PRESETS)
    if custom_presets:
        for key, raw in custom_presets.items():
            if isinstance(raw, SwarmPreset):
                merged[key] = raw
            elif isinstance(raw, dict):
                parsed = _parse_custom_preset(key, raw)
                if parsed is not None:
                    merged[key] = parsed
    return merged


def _parse_custom_preset(name: str, raw: dict[str, Any]) -> Optional[SwarmPreset]:
    """Parse a custom preset from a YAML-style dict."""
    description = str(raw.get("description", ""))
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, (list, tuple)):
        return None
    roles: list[SwarmAgentRole] = []
    for item in roles_raw:
        if not isinstance(item, dict):
            continue
        role_name = str(item.get("name", "")).strip()
        if not role_name:
            continue
        roles.append(
            SwarmAgentRole(
                name=role_name,
                model=str(item.get("model", _SONNET)),
                agent_type=str(item.get("agent_type", "general-purpose")),
                is_lead=bool(item.get("is_lead", False)),
                prompt_template=str(item.get("prompt_template", "")),
            )
        )
    if not roles:
        return None
    return SwarmPreset(name=name, description=description, roles=tuple(roles))
