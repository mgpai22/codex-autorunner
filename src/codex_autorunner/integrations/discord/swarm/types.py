from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class SwarmAgentState(str, enum.Enum):
    SPAWNING = "spawning"
    RUNNING = "running"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SwarmAgentRole:
    name: str
    model: str
    agent_type: str = "general-purpose"
    is_lead: bool = False
    prompt_template: str = ""


@dataclass(frozen=True)
class SwarmPreset:
    name: str
    description: str
    roles: tuple[SwarmAgentRole, ...]


@dataclass
class SwarmAgentInfo:
    agent_name: str
    agent_id: str
    role_name: str
    model: Optional[str] = None
    is_lead: bool = False
    discord_thread_id: Optional[int] = None
    discord_root_message_id: Optional[int] = None
    status: str = SwarmAgentState.SPAWNING.value
    pid: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_message_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "agent_id": self.agent_id,
            "role_name": self.role_name,
            "model": self.model,
            "is_lead": self.is_lead,
            "discord_thread_id": self.discord_thread_id,
            "discord_root_message_id": self.discord_root_message_id,
            "status": self.status,
            "pid": self.pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_message_at": self.last_message_at,
        }


@dataclass
class SwarmSession:
    swarm_id: str
    team_name: str
    preset_name: str
    prompt: str
    guild_id: int
    workspace_id: str
    workspace_path: str
    forum_channel_id: int
    user_id: int
    status: str = "starting"
    agents: dict[str, SwarmAgentInfo] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "swarm_id": self.swarm_id,
            "team_name": self.team_name,
            "preset_name": self.preset_name,
            "prompt": self.prompt,
            "guild_id": self.guild_id,
            "workspace_id": self.workspace_id,
            "workspace_path": self.workspace_path,
            "forum_channel_id": self.forum_channel_id,
            "user_id": self.user_id,
            "status": self.status,
            "agents": {k: v.to_dict() for k, v in self.agents.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
