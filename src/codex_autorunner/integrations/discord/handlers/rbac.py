from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from ....core.logging_utils import log_event

if TYPE_CHECKING:
    from ..config import DiscordRoleTier

logger = logging.getLogger("codex_autorunner.integrations.discord.handlers.rbac")

_RBAC_CAPABILITIES = {"can_setup", "can_run", "can_stop", "can_bind"}


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, (list, tuple, set)):
        result: list[int] = []
        for item in value:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, bool):
                result.append(parsed)
        return result
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        result = []
        for part in parts:
            if not part:
                continue
            try:
                parsed = int(part)
            except ValueError:
                continue
            if not isinstance(parsed, bool):
                result.append(parsed)
        return result
    return []


class DiscordRBACMixin:
    """Permission checking mixin that maps Discord roles to capability tiers."""

    async def _check_rbac(self, interaction: Any, capability: str) -> bool:
        """Check whether the caller has a given RBAC capability."""
        rbac = getattr(self._config, "rbac", None)
        if rbac is None or not bool(_get_field(rbac, "enabled", False)):
            return True

        if capability not in _RBAC_CAPABILITIES:
            return False

        user_roles: list[int] = []
        user = getattr(interaction, "user", None)
        try:
            roles = getattr(user, "roles", None)
            if isinstance(roles, (list, tuple)):
                for role in roles:
                    role_id = getattr(role, "id", None)
                    if isinstance(role_id, int) and not isinstance(role_id, bool):
                        user_roles.append(role_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "discord.rbac.role_extract_failed",
                capability=capability,
                exc=exc,
            )

        tier = self._get_user_tier(user_roles)
        if tier is None:
            log_event(
                self._logger,
                logging.INFO,
                "discord.rbac.denied",
                capability=capability,
                reason="no_matching_tier",
                guild_id=getattr(interaction, "guild_id", None),
                user_id=getattr(user, "id", None) if user else None,
            )
            return False

        allowed = bool(_get_field(tier, capability, False))
        if not allowed:
            log_event(
                self._logger,
                logging.INFO,
                "discord.rbac.denied",
                capability=capability,
                tier=_get_field(tier, "name", None),
                guild_id=getattr(interaction, "guild_id", None),
                user_id=getattr(user, "id", None) if user else None,
            )
        return allowed

    def _resolve_approval_mode_for_user(self, interaction: Any) -> Optional[str]:
        """Resolve the effective approval mode for this user based on RBAC tier."""
        rbac = getattr(self._config, "rbac", None)
        if rbac is None or not bool(_get_field(rbac, "enabled", False)):
            return None

        user_roles: list[int] = []
        user = getattr(interaction, "user", None)
        try:
            roles = getattr(user, "roles", None)
            if isinstance(roles, (list, tuple)):
                for role in roles:
                    role_id = getattr(role, "id", None)
                    if isinstance(role_id, int) and not isinstance(role_id, bool):
                        user_roles.append(role_id)
        except Exception:
            return None

        tier = self._get_user_tier(user_roles)
        if tier is None:
            return None
        approval_mode = _get_field(tier, "approval_mode", None)
        if isinstance(approval_mode, str) and approval_mode.strip():
            return approval_mode.strip()
        return None

    def _get_user_tier(self, user_roles: list[int]) -> Optional["DiscordRoleTier"]:
        """Find the best matching tier for a set of role IDs.

        The tier list is treated as ordered by privilege (highest first).
        """
        rbac = getattr(self._config, "rbac", None)
        if rbac is None or not bool(_get_field(rbac, "enabled", False)):
            return None

        tiers_raw = _get_field(rbac, "tiers", None)
        tiers: list[Any] = []
        if isinstance(tiers_raw, (list, tuple)):
            tiers = list(tiers_raw)

        role_set = {rid for rid in user_roles if isinstance(rid, int)}

        # First matching tier wins (tiers are ordered by privilege).
        for tier in tiers:
            tier_role_ids = _parse_int_list(_get_field(tier, "role_ids", None))
            if tier_role_ids and role_set.intersection(tier_role_ids):
                return tier  # type: ignore[return-value]

        default_name = _get_field(rbac, "default_tier", None)
        if isinstance(default_name, str) and default_name.strip():
            name_key = default_name.strip()
            for tier in tiers:
                if _get_field(tier, "name", None) == name_key:
                    return tier  # type: ignore[return-value]

        return None
