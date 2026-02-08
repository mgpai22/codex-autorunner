"""SwarmController — spawn and manage Claude CLI teammate processes.

Wraps the filesystem protocol with subprocess management for Claude Code
agents running in ``--teammate-mode auto``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from typing import Any, Callable, Coroutine, Optional

from ....core.utils import resolve_executable, subprocess_env
from . import protocol
from .types import SwarmAgentState

logger = logging.getLogger("codex_autorunner.integrations.discord.swarm.controller")

# Python PTY wrapper script — provides a real terminal for the Claude TUI.
# Identical to the approach used by claude-code-controller's process-manager.ts.
_PTY_WRAPPER = r"""
import pty, os, sys, json, signal, select

cmd = json.loads(sys.argv[1])
pid, fd = pty.fork()
if pid == 0:
    os.execvp(cmd[0], cmd)
else:
    signal.signal(signal.SIGTERM, lambda *a: (os.kill(pid, signal.SIGTERM), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda *a: (os.kill(pid, signal.SIGTERM), sys.exit(0)))
    try:
        while True:
            r, _, _ = select.select([fd, 0], [], [], 1.0)
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    os.write(1, data)
                except OSError:
                    break
            if 0 in r:
                try:
                    data = os.read(0, 4096)
                    if not data:
                        break
                    os.write(fd, data)
                except OSError:
                    break
    except:
        pass
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except:
            pass
        _, status = os.waitpid(pid, 0)
        sys.exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1)
"""

MessageCallback = Callable[
    [str, dict[str, Any]], Coroutine[Any, Any, None]
]


class SwarmController:
    """Manage a team of Claude CLI teammate processes."""

    def __init__(
        self,
        team_name: str,
        cwd: str,
        *,
        claude_binary: str = "claude",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.team_name = team_name
        self.cwd = cwd
        self.claude_binary = claude_binary
        self._log = logger or logging.getLogger(__name__)
        self._session_id = str(uuid.uuid4())
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._agent_pids: dict[str, int] = {}
        self._message_callbacks: list[MessageCallback] = []
        self._output_callbacks: list[MessageCallback] = []
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._output_readers: dict[str, asyncio.Task[None]] = {}
        self._polling = False
        # Deduplicate messages observed from non-controller inboxes. These
        # messages may already be marked read by a recipient agent by the time
        # we poll, so we cannot rely on the `read` flag for idempotence.
        self._seen_inbox_message_keys: set[str] = set()
        self._seen_inbox_message_key_order: deque[str] = deque()
        self._seen_inbox_message_key_cap = 5000

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize(self, *, lead_name: str = "lead") -> None:
        """Create the team directory structure and initial config."""
        td = protocol.team_dir(self.team_name)
        td.mkdir(parents=True, exist_ok=True)
        protocol.inboxes_dir(self.team_name).mkdir(parents=True, exist_ok=True)
        protocol.tasks_dir(self.team_name).mkdir(parents=True, exist_ok=True)

        # Claude Code teammate-mode routes agent SendMessage output to the
        # team's leadAgentId inbox. To mirror claude-code-controller and avoid
        # races with a real CLI agent consuming its own inbox, we keep a
        # virtual "controller" as the lead and consume `controller.json`.
        controller_id = f"controller@{self.team_name}"
        now_ms = int(time.time() * 1000)
        # Keep track of which role is the "lead" for UI/metadata, but do not
        # use it as leadAgentId in the teammate protocol.
        self._lead_name = lead_name
        await protocol.write_team_config(
            self.team_name,
            description=f"Swarm team {self.team_name}",
            lead_agent_id=controller_id,
            lead_session_id=self._session_id,
            members=[
                {
                    "agentId": controller_id,
                    "name": "controller",
                    "agentType": "controller",
                    "joinedAt": now_ms,
                    "tmuxPaneId": "",
                    "cwd": self.cwd,
                    "subscriptions": [],
                }
            ],
        )
        # Ensure the controller inbox exists.
        inbox = protocol.inbox_path(self.team_name, "controller")
        inbox.parent.mkdir(parents=True, exist_ok=True)
        if not inbox.exists():
            inbox.write_text("[]\n", encoding="utf-8")
        self._log.info("Swarm controller initialized: team=%s", self.team_name)

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    async def spawn_agent(
        self,
        name: str,
        *,
        model: str = "claude-sonnet-4-5-20250929",
        agent_type: str = "general-purpose",
        permission_mode: str = "bypassPermissions",
        prompt: str = "",
    ) -> str:
        """Spawn a Claude CLI agent in teammate mode. Returns the agent ID."""
        agent_id = f"{name}@{self.team_name}"

        # Register member in team config
        member = {
            "agentId": agent_id,
            "name": name,
            "agentType": agent_type,
            "model": model,
            "joinedAt": int(time.time() * 1000),
            "tmuxPaneId": "",
            "cwd": self.cwd,
            "subscriptions": [],
        }
        await protocol.add_member(self.team_name, member)

        # Build the claude CLI command — do NOT use -p, which makes the
        # agent run in one-shot mode and exit.  Instead send the initial
        # prompt via the inbox so the agent stays alive in teammate-mode
        # and keeps polling for follow-up messages.
        env = subprocess_env(base_env=os.environ)
        env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
        resolved_claude = resolve_executable(self.claude_binary, env=env)
        claude_bin = resolved_claude or self.claude_binary
        claude_args = [
            claude_bin,
            "--teammate-mode", "auto",
            "--agent-id", agent_id,
            "--agent-name", name,
            "--team-name", self.team_name,
            "--agent-type", agent_type,
            "--model", model,
            "--permission-mode", permission_mode,
            "--parent-session-id", self._session_id,
        ]

        cmd_json = json.dumps(claude_args)

        self._log.info(
            "Spawning agent %r: %s %s",
            name,
            claude_bin,
            " ".join(claude_args[1:]),
        )

        proc = subprocess.Popen(
            [sys.executable, "-c", _PTY_WRAPPER, cmd_json],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            preexec_fn=os.setsid,
        )

        self._processes[name] = proc
        if proc.pid is not None:
            self._agent_pids[name] = proc.pid

        # Start reading stdout in background
        self._output_readers[name] = asyncio.create_task(
            self._read_agent_output(name, proc)
        )

        # Send the initial prompt via inbox (not -p) so the agent stays
        # alive in teammate-mode and can receive follow-up messages.
        if prompt:
            # Small delay to let the agent process start and begin
            # polling its inbox.
            await asyncio.sleep(1.0)
            await self.send_message(name, prompt, summary="Initial task")

        return agent_id

    async def send_message(
        self, agent_name: str, text: str, *, summary: str = ""
    ) -> None:
        """Send a message to a specific agent's inbox."""
        await protocol.write_inbox(
            self.team_name,
            agent_name,
            from_name="controller",
            text=text,
            summary=summary,
        )

    async def broadcast(self, text: str, *, summary: str = "") -> None:
        """Send a message to all agents."""
        config = await protocol.read_team_config(self.team_name)
        if config is None:
            return
        members = config.get("members", [])
        for member in members:
            name = member.get("name", "")
            if name and name != "controller":
                await self.send_message(name, text, summary=summary)

    # ------------------------------------------------------------------
    # Inbox polling
    # ------------------------------------------------------------------

    async def start_polling(self, interval: float = 0.5) -> None:
        """Start polling the controller's inbox for agent messages."""
        if self._polling:
            return
        self._polling = True
        self._poll_task = asyncio.create_task(self._poll_loop(interval))

    async def stop_polling(self) -> None:
        """Stop inbox polling."""
        self._polling = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def _poll_loop(self, interval: float) -> None:
        """Poll inboxes for messages and route them to callbacks.

        The controller consumes `inboxes/controller.json` (read + mark as read).
        This mirrors claude-code-controller and avoids races with a real CLI
        agent consuming the inbox before we can observe it.
        """
        while self._polling:
            try:
                unread = await protocol.read_unread(self.team_name, "controller")
                for msg in unread:
                    from_agent = msg.get("from")
                    if not isinstance(from_agent, str) or not from_agent:
                        continue
                    if from_agent == "controller":
                        continue
                    for cb in self._message_callbacks:
                        try:
                            await cb(from_agent, msg)
                        except Exception as exc:
                            self._log.error("Message callback error: %s", exc)

                # Observe inter-agent messages. These are written to the
                # recipient's inbox (not controller.json), and can be marked
                # read quickly by the recipient, so read the full inbox and
                # dedupe locally.
                inbox_names = await protocol.list_inbox_names(self.team_name)
                for inbox_name in inbox_names:
                    if inbox_name == "controller":
                        continue
                    messages = await protocol.read_inbox(self.team_name, inbox_name)
                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        from_agent = msg.get("from")
                        if not isinstance(from_agent, str) or not from_agent:
                            continue
                        if from_agent == "controller":
                            continue
                        ts = msg.get("timestamp", "")
                        summary = msg.get("summary", "")
                        text = msg.get("text", "")
                        text_hash = ""
                        if isinstance(text, str) and text:
                            text_hash = hashlib.sha1(
                                text.encode("utf-8", errors="replace")
                            ).hexdigest()
                        key = f"{inbox_name}|{from_agent}|{ts}|{summary}|{text_hash}"
                        if key in self._seen_inbox_message_keys:
                            continue
                        self._seen_inbox_message_keys.add(key)
                        self._seen_inbox_message_key_order.append(key)
                        while (
                            len(self._seen_inbox_message_key_order)
                            > self._seen_inbox_message_key_cap
                        ):
                            old = self._seen_inbox_message_key_order.popleft()
                            self._seen_inbox_message_keys.discard(old)
                        for cb in self._message_callbacks:
                            try:
                                await cb(from_agent, msg)
                            except Exception as exc:
                                self._log.error("Message callback error: %s", exc)
            except Exception as exc:
                self._log.error("Poll error: %s", exc)
            await asyncio.sleep(interval)

    def on_message(self, callback: MessageCallback) -> None:
        """Register a callback for incoming agent messages."""
        self._message_callbacks.append(callback)

    def on_output(self, callback: MessageCallback) -> None:
        """Register a callback for agent stdout output."""
        self._output_callbacks.append(callback)

    async def _read_agent_output(
        self, name: str, proc: subprocess.Popen[bytes]
    ) -> None:
        """Read an agent's PTY stdout in a background thread and fire callbacks."""
        import re

        _ANSI_RE = re.compile(
            r"\x1b\[[\?<>=]?[0-9;]*[A-Za-z~]"  # CSI sequences (incl. private/mouse modes)
            r"|\x1b\][\s\S]*?\x07"               # OSC sequences
            r"|\x1b[()][AB012]"                   # Character set selection
            r"|\x1b[=>]"                           # Keypad mode
            r"|\x1b."                              # Any other single-char ESC sequence
            r"|\r"                                 # Carriage return
        )
        loop = asyncio.get_running_loop()
        buf = b""

        def _read_chunk() -> bytes:
            if proc.stdout is None:
                return b""
            try:
                return proc.stdout.read(4096) or b""
            except (OSError, ValueError):
                return b""

        try:
            while proc.poll() is None:
                chunk = await loop.run_in_executor(None, _read_chunk)
                if not chunk:
                    # Process may still be running but no data yet
                    await asyncio.sleep(0.2)
                    continue

                buf += chunk

                # Process complete lines
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    try:
                        line = line_bytes.decode("utf-8", errors="replace")
                    except Exception:
                        continue

                    # Strip ANSI escape codes and stray terminal artifacts
                    line = _ANSI_RE.sub("", line)
                    # Remove leftover partial escape fragments like [<u, [?25h etc.
                    line = re.sub(r"\[[\?<>=]?[0-9;]*[A-Za-z~]?", "", line)
                    # Strip any remaining control chars (ESC, BEL, etc.)
                    line = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", line)
                    line = line.strip()
                    if not line:
                        continue

                    # Fire output callbacks
                    msg = {"text": line, "type": "stdout"}
                    for cb in self._output_callbacks:
                        try:
                            await cb(name, msg)
                        except Exception as exc:
                            self._log.error(
                                "Output callback error for %s: %s", name, exc
                            )

            # Process remaining buffer
            if buf:
                try:
                    remaining = buf.decode("utf-8", errors="replace")
                    remaining = _ANSI_RE.sub("", remaining).strip()
                    if remaining:
                        msg = {"text": remaining, "type": "stdout"}
                        for cb in self._output_callbacks:
                            try:
                                await cb(name, msg)
                            except Exception as exc:
                                self._log.error(
                                    "Output callback error for %s: %s", name, exc
                                )
                except Exception:
                    pass

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error("Output reader error for %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def request_shutdown(self, agent_name: str) -> None:
        """Send a shutdown request to an agent via inbox."""
        request_id = str(uuid.uuid4())[:8]
        shutdown_msg = json.dumps(
            {
                "type": "shutdown_request",
                "requestId": request_id,
                "from": "controller",
                "reason": "Swarm stopping",
                "timestamp": protocol._iso_now(),
            }
        )
        await self.send_message(
            agent_name, shutdown_msg, summary="Shutdown request"
        )

    async def kill_agent(self, agent_name: str) -> None:
        """Kill a specific agent process."""
        # Cancel the output reader first
        reader = self._output_readers.pop(agent_name, None)
        if reader and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass

        proc = self._processes.pop(agent_name, None)
        if proc is None:
            return

        # Try SIGTERM to the process group first
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        # Wait briefly, then SIGKILL if still alive
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

        self._agent_pids.pop(agent_name, None)
        self._log.info("Killed agent %r", agent_name)

    async def kill_all(self) -> None:
        """Kill all agent processes."""
        names = list(self._processes.keys())
        for name in names:
            await self.kill_agent(name)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def create_task(
        self, subject: str, description: str, **kwargs: Any
    ) -> str:
        """Create a task in the team's task directory."""
        return await protocol.create_task(
            self.team_name,
            subject=subject,
            description=description,
            **kwargs,
        )

    async def assign_task(self, task_id: str, agent_name: str) -> None:
        """Assign a task to an agent and notify them."""
        task = await protocol.update_task(
            self.team_name, task_id, {"owner": agent_name}
        )
        if task is None:
            return

        assignment_msg = json.dumps(
            {
                "type": "task_assignment",
                "taskId": task_id,
                "subject": task.get("subject", ""),
                "description": task.get("description", ""),
                "assignedBy": "controller",
                "timestamp": protocol._iso_now(),
            }
        )
        await self.send_message(
            agent_name, assignment_msg, summary=f"Task: {task.get('subject', '')}"
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def is_agent_running(self, name: str) -> bool:
        """Check if an agent process is still running."""
        proc = self._processes.get(name)
        if proc is None:
            return False
        return proc.poll() is None

    def get_agent_pid(self, name: str) -> Optional[int]:
        """Get the PID of a running agent."""
        return self._agent_pids.get(name)

    def running_agents(self) -> list[str]:
        """Return names of all running agents."""
        return [n for n in self._processes if self.is_agent_running(n)]

    def agent_exit_code(self, name: str) -> Optional[int]:
        """Return the exit code of a finished agent, or None if still running."""
        proc = self._processes.get(name)
        if proc is None:
            return None
        return proc.poll()
