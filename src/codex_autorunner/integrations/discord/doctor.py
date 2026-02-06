from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("codex_autorunner.integrations.discord.doctor")


class DiagnosticResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.message = ""
        self.detail: Optional[str] = None

    def pass_(self, message: str, detail: Optional[str] = None) -> "DiagnosticResult":
        self.ok = True
        self.message = message
        self.detail = detail
        return self

    def fail(self, message: str, detail: Optional[str] = None) -> "DiagnosticResult":
        self.ok = False
        self.message = message
        self.detail = detail
        return self


async def check_discord_dependency() -> DiagnosticResult:
    result = DiagnosticResult("discord.py installed")
    try:
        import discord

        version = getattr(discord, "__version__", "unknown")
        return result.pass_(f"discord.py {version}")
    except ImportError:
        return result.fail(
            "discord.py not installed",
            "Install with: pip install 'codex-autorunner[discord]'",
        )


async def check_bot_token(token: Optional[str]) -> DiagnosticResult:
    result = DiagnosticResult("bot token configured")
    if not token:
        return result.fail("No bot token found in environment")
    # Mask the token for display
    masked = token[:8] + "..." + token[-4:] if len(token) > 12 else "***"
    return result.pass_(f"Token present ({masked})")


async def check_gateway_connectivity(token: Optional[str]) -> DiagnosticResult:
    result = DiagnosticResult("gateway connectivity")
    if not token:
        return result.fail("Cannot test without a bot token")

    try:
        import discord

        intents = discord.Intents.default()
        client = discord.Client(intents=intents)

        ready = asyncio.Event()

        @client.event
        async def on_ready() -> None:
            ready.set()
            await client.close()

        task = asyncio.create_task(client.start(token))
        try:
            await asyncio.wait_for(ready.wait(), timeout=15.0)
            return result.pass_("Connected to Discord gateway")
        except asyncio.TimeoutError:
            return result.fail("Timed out connecting to Discord gateway")
        finally:
            if not client.is_closed():
                await client.close()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as exc:
        return result.fail(f"Gateway connection failed: {exc}")


async def check_state_file(state_path: Path) -> DiagnosticResult:
    result = DiagnosticResult("state file")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        from ...core.sqlite_utils import connect_sqlite

        conn = connect_sqlite(state_path)
        conn.close()
        return result.pass_(f"SQLite accessible at {state_path}")
    except Exception as exc:
        return result.fail(f"State file error: {exc}")


async def run_health_check(
    token: Optional[str] = None,
    state_path: Optional[Path] = None,
    *,
    skip_gateway: bool = False,
) -> list[DiagnosticResult]:
    """Run all health check diagnostics and return results."""
    results: list[DiagnosticResult] = []

    results.append(await check_discord_dependency())
    results.append(await check_bot_token(token))

    if not skip_gateway and token:
        results.append(await check_gateway_connectivity(token))

    if state_path:
        results.append(await check_state_file(state_path))

    return results


def format_health_results(results: list[DiagnosticResult]) -> str:
    """Format health check results for display."""
    lines: list[str] = []
    for r in results:
        icon = "PASS" if r.ok else "FAIL"
        lines.append(f"[{icon}] {r.name}: {r.message}")
        if r.detail:
            lines.append(f"       {r.detail}")
    all_ok = all(r.ok for r in results)
    lines.append("")
    lines.append("Overall: HEALTHY" if all_ok else "Overall: UNHEALTHY")
    return "\n".join(lines)
