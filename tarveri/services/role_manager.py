"""
Unified role manager for idempotent Discord role discovery and atomic creation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from tarveri.database import Database

logger = logging.getLogger("tarveri")


class RoleManager:
    """
    Manages idempotent Discord role discovery and atomic creation with double-checked locking
    and SQLite audit recording.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._guild_role_locks: dict[int, asyncio.Lock] = {}

    def get_guild_role_lock(self, guild_id: int) -> asyncio.Lock:
        """Retrieves or creates an asyncio.Lock for atomic role creation in the specified guild."""
        if guild_id not in self._guild_role_locks:
            self._guild_role_locks[guild_id] = asyncio.Lock()
        return self._guild_role_locks[guild_id]

    async def find_role_in_guild(
        self,
        guild: discord.Guild,
        matcher: Callable[[Sequence[discord.Role]], discord.Role | None],
    ) -> discord.Role | None:
        """
        Searches for an existing role in a guild:
        1. Checks in-memory guild.roles cache.
        2. Falls back to live REST API (guild.fetch_roles) if available.
        """
        # 1. Check in-memory guild.roles cache
        guild_roles = getattr(guild, "roles", [])
        if isinstance(guild_roles, (list, tuple)):
            found = matcher(guild_roles)
            if found is not None:
                return found

        # 2. Check live API
        if hasattr(guild, "fetch_roles") and callable(guild.fetch_roles):
            try:
                live_roles = await guild.fetch_roles()
                if isinstance(live_roles, (list, tuple)):
                    found = matcher(live_roles)
                    if found is not None:
                        return found
            except (discord.HTTPException, discord.Forbidden):
                pass

        return None

    async def get_or_create_role(
        self,
        guild: discord.Guild,
        role_name: str,
        matcher: Callable[[Sequence[discord.Role]], discord.Role | None],
        *,
        colour: discord.Colour | int = 0x3498DB,
        mentionable: bool = False,
        permissions: discord.Permissions | None = None,
        reason: str = "TARVeri: auto-created missing role",
    ) -> discord.Role | None:
        """
        Finds an existing role using the matcher. ONLY creates a new role if not found anywhere.
        Guarantees idempotency via double-checked locking across concurrent tasks.
        """
        existing = await self.find_role_in_guild(guild, matcher)
        if existing is not None:
            return existing

        lock = self.get_guild_role_lock(guild.id)
        async with lock:
            existing = await self.find_role_in_guild(guild, matcher)
            if existing is not None:
                return existing

            can_manage = (
                getattr(guild.me.guild_permissions, "manage_roles", False)
                if hasattr(guild, "me") and hasattr(guild.me, "guild_permissions")
                else False
            )
            if not can_manage:
                return None

            try:
                col = colour if isinstance(colour, discord.Colour) else discord.Colour(colour)
                create_kwargs = {
                    "name": role_name,
                    "colour": col,
                    "mentionable": mentionable,
                    "reason": reason,
                }
                if permissions is not None:
                    create_kwargs["permissions"] = permissions

                role = await guild.create_role(**create_kwargs)
                try:
                    await self.db.record_bot_created_role(guild.id, role.id, role_name)
                except Exception as e:
                    logger.debug("Could not record bot created role: %s", e)

                await self.db.log(
                    "INFO",
                    "ROLE_CREATED",
                    f"Created role '{role_name}' in '{guild.name}' (Guild ID: {guild.id})",
                    guild=guild,
                )
                return role
            except discord.HTTPException as e:
                await self.db.log(
                    "ERROR",
                    "ROLE_CREATE_FAILED",
                    f"Failed to create role '{role_name}' in '{guild.name}': {e}",
                    guild=guild,
                )
                return None
