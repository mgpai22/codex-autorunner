"""SwarmCommands mixin — /swarm, /swarm-stop, /swarm-status."""
from __future__ import annotations

import logging
from typing import Any, Optional

from .....core.logging_utils import log_event

logger = logging.getLogger(
    "codex_autorunner.integrations.discord.handlers.commands.swarm_commands"
)


class SwarmCommands:
    """Mixin providing swarm slash command implementations."""

    # ------------------------------------------------------------------
    # /swarm
    # ------------------------------------------------------------------

    async def _cmd_swarm_impl(
        self,
        interaction: Any,
        prompt: str,
        preset: str = "code-review",
    ) -> None:
        """Start a multi-agent swarm."""
        import discord as _discord

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "`/swarm` must be run inside a guild.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id

        # RBAC check
        if hasattr(self, "_check_rbac"):
            allowed = await self._check_rbac(interaction, "can_run")
            if not allowed:
                await interaction.followup.send(
                    "You do not have permission to start swarms.", ephemeral=True
                )
                return

        # Check swarm config
        swarm_config = self._config.swarm
        if not swarm_config.enabled:
            await interaction.followup.send(
                "Swarm mode is disabled.", ephemeral=True
            )
            return

        # Resolve workspace from channel binding
        channel = interaction.channel
        if isinstance(channel, _discord.Thread):
            channel_id = channel.parent_id
        else:
            channel_id = channel.id

        channel_key = f"{guild_id}:{channel_id}"
        workspace_path = await self._store.get_channel_binding(channel_key)
        if not workspace_path:
            await interaction.followup.send(
                "No workspace bound to this channel. Use `/bind` first.",
                ephemeral=True,
            )
            return

        # Resolve workspace_id and forum channel
        workspace_id = workspace_path  # Use path as ID fallback
        try:
            rows = await self._store.list_scaffolded_channels(guild_id)
            for _gid, ws_id, ch_type, ch_id, _created_at in rows:
                if ch_type == "tasks":
                    # Check if this workspace matches by looking at binding
                    ckey = f"{guild_id}:{ch_id}"
                    bound = await self._store.get_channel_binding(ckey)
                    if bound == workspace_path:
                        workspace_id = ws_id
                        forum_channel_id = ch_id
                        break
            else:
                await interaction.followup.send(
                    "No tasks forum channel found for this workspace. "
                    "Run `/setup` first.",
                    ephemeral=True,
                )
                return
        except Exception as exc:
            await interaction.followup.send(
                f"Failed to resolve workspace channels: {exc}",
                ephemeral=True,
            )
            return

        # Ensure SwarmManager
        manager = await self._ensure_swarm_manager()

        try:
            swarm_id = await manager.start_swarm(
                guild,
                workspace_id=workspace_id,
                workspace_path=workspace_path,
                forum_channel_id=forum_channel_id,
                preset_name=preset,
                prompt=prompt,
                user_id=interaction.user.id,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            log_event(
                self._logger,
                logging.ERROR,
                "discord.swarm.start_failed",
                guild_id=guild_id,
                preset=preset,
                exc=exc,
            )
            await interaction.followup.send(
                f"Failed to start swarm: {exc}", ephemeral=True
            )
            return

        # Build summary embed
        from ...rendering import build_swarm_summary_embed

        agents = await self._store.list_swarm_agents(swarm_id)
        session = await self._store.get_swarm(swarm_id)
        embed = build_swarm_summary_embed(session or {}, agents)

        lead_thread_id: Optional[int] = None
        lead_agent_name: Optional[str] = None
        for agent in agents:
            if agent.get("is_lead"):
                lead_thread_id = agent.get("discord_thread_id")
                lead_agent_name = agent.get("agent_name")
                break

        lead_link = (
            f"<#{lead_thread_id}>" if isinstance(lead_thread_id, int) else None
        )
        lead_label = (
            f"{lead_link} (`{lead_agent_name}`)" if lead_link and lead_agent_name else lead_link
        )

        await interaction.followup.send(
            (
                f"Swarm `{swarm_id[:8]}` started with preset **{preset}**."
                + (f" Lead: {lead_label}." if lead_label else "")
            ),
            embed=embed,
        )

        log_event(
            self._logger,
            logging.INFO,
            "discord.swarm.started",
            swarm_id=swarm_id,
            guild_id=guild_id,
            preset=preset,
            agent_count=len(agents),
        )

    # ------------------------------------------------------------------
    # /swarm-stop
    # ------------------------------------------------------------------

    async def _cmd_swarm_stop_impl(
        self, interaction: Any, swarm_id: Optional[str] = None
    ) -> None:
        """Stop an active swarm."""
        if hasattr(self, "_check_rbac"):
            allowed = await self._check_rbac(interaction, "can_stop")
            if not allowed:
                await interaction.followup.send(
                    "You do not have permission to stop swarms.", ephemeral=True
                )
                return

        manager = getattr(self, "_swarm_manager", None)
        if manager is None:
            await interaction.followup.send(
                "No active swarms.", ephemeral=True
            )
            return

        if swarm_id:
            if not manager.is_swarm_active(swarm_id):
                await interaction.followup.send(
                    f"Swarm `{swarm_id[:8]}` is not active.", ephemeral=True
                )
                return
            await manager.stop_swarm(swarm_id)
            await interaction.followup.send(
                f"Swarm `{swarm_id[:8]}` stopped.", ephemeral=True
            )
        else:
            active_ids = manager.get_active_swarm_ids()
            if not active_ids:
                await interaction.followup.send(
                    "No active swarms to stop.", ephemeral=True
                )
                return
            await manager.stop_all()
            await interaction.followup.send(
                f"Stopped {len(active_ids)} swarm(s).", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /swarm-status
    # ------------------------------------------------------------------

    async def _cmd_swarm_status_impl(
        self, interaction: Any, swarm_id: Optional[str] = None
    ) -> None:
        """Show swarm status."""
        guild_id = interaction.guild_id

        if swarm_id:
            session = await self._store.get_swarm(swarm_id)
            if session is None:
                await interaction.followup.send(
                    f"Swarm `{swarm_id[:8]}` not found.", ephemeral=True
                )
                return
            agents = await self._store.list_swarm_agents(swarm_id)
            from ...rendering import build_swarm_status_embed

            embed = build_swarm_status_embed(session, agents)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            swarms = await self._store.list_swarms(guild_id)
            if not swarms:
                await interaction.followup.send(
                    "No swarms found for this guild.", ephemeral=True
                )
                return
            # Show the most recent swarm
            latest = swarms[-1] if swarms else None
            if latest:
                sid = latest.get("swarm_id", "")
                agents = await self._store.list_swarm_agents(sid)
                from ...rendering import build_swarm_status_embed

                embed = build_swarm_status_embed(latest, agents)
                await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Lazy manager init
    # ------------------------------------------------------------------

    async def _ensure_swarm_manager(self) -> Any:
        """Lazily initialize the SwarmManager."""
        if getattr(self, "_swarm_manager", None) is None:
            from ...swarm.manager import SwarmManager

            self._swarm_manager = SwarmManager(self)
        return self._swarm_manager
