"""PTY wrapper for spawning a Claude CLI teammate with a real terminal.

This module is executed as a separate Python process by SwarmController. It
forks a PTY for the child command and bridges stdin/stdout so interactive TUIs
behave correctly.
"""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import sys
from typing import NoReturn


def _try_kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except Exception:
        pass


def main() -> NoReturn:
    if len(sys.argv) < 2:
        print("usage: pty_wrapper.py <cmd_json>", file=sys.stderr)
        raise SystemExit(2)

    cmd = json.loads(sys.argv[1])
    if not isinstance(cmd, list) or not cmd:
        print("cmd_json must be a JSON list of argv", file=sys.stderr)
        raise SystemExit(2)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)
        raise SystemExit(127)

    def _handle_term(*_args: object) -> None:
        _try_kill(pid, signal.SIGTERM)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    try:
        while True:
            r, _, _ = select.select([fd, 0], [], [], 1.0)
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                try:
                    os.write(1, data)
                except OSError:
                    break
            if 0 in r:
                try:
                    data = os.read(0, 4096)
                except OSError:
                    break
                if not data:
                    break
                try:
                    os.write(fd, data)
                except OSError:
                    break
    finally:
        _try_kill(pid, signal.SIGTERM)
        try:
            _, status = os.waitpid(pid, 0)
        except Exception:
            raise SystemExit(1)

        if os.WIFEXITED(status):
            raise SystemExit(os.WEXITSTATUS(status))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

