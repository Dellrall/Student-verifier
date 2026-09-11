"""
Verification business logic, concurrency control, and cross-guild role synchronization.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

import aiosqlite
import discord

from tarveri.config import (
    ALUMNI_ROLE_COLOR,
    ALUMNI_ROLE_NAME,
    CAMPUS_ALIASES,
    CAMPUS_COLORS,
    CAMPUS_ROLE_NAMES,
    CAMPUS_ROLES,
    FACULTY_ALIASES,
    FACULTY_COLORS,
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    GUEST_ROLE_PATTERN,
    ROLE_QUALIFIER_PATTERN,
    SRC_ROLE_NAMES,
    SRC_ROLES,
    STUDY_LEVEL_ALIASES,
    STUDY_LEVEL_COLORS,
    STUDY_LEVEL_ROLE_NAMES,
    STUDY_LEVEL_ROLES,
    StudentIdInfo,
    hash_student_id,
    mask_student_id,
    parse_student_id,
    validate_student_id,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter

logger = logging.getLogger("tarveri")


@dataclass(slots=True)
class RoleSyncResult:
    verified_in: list[tuple[int, str, str]] = field(default_factory=list)  # (guild_id, guild_name, role_name)
    already_had_role_in: list[tuple[int, str, str]] = field(default_factory=list)  # (guild_id, guild_name, role_name)
    missing_role_in: list[str] = field(default_factory=list)
    failed_in: list[str] = field(default_factory=list)


class VerificationService:
    def __init__(self, bot: discord.Client, db: Database, secret: str, rate_limiter: RateLimiter):
        self.bot = bot
        self.db = db
        self.secret = secret
        self.rate_limiter = rate_limiter
        self._in_flight_users: set[int] = set()
        self._lock = asyncio.Lock()
        self._role_locks: dict[int, asyncio.Lock] = {}

    def _get_guild_role_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._role_locks:
            self._role_locks[guild_id] = asyncio.Lock()
        return self._role_locks[guild_id]

    async def get_or_fetch_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        """Retrieves a member from cache (O(1)), or fetches from Discord API on cache miss."""
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    async def get_mutual_guilds_for_user(self, user_id: int) -> list[discord.Guild]:
        """
        Finds all mutual guilds where the user is a member.
        Optimized with fast cache-check first and concurrent API fetch on cache misses.
        """
        mutual: list[discord.Guild] = []
        missing_in_cache: list[discord.Guild] = []

        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member is not None:
                mutual.append(guild)
            else:
                missing_in_cache.append(guild)

        if missing_in_cache:
            async def _check_guild(g: discord.Guild) -> discord.Guild | None:
                try:
                    m = await g.fetch_member(user_id)
                    return g if m is not None else None
                except (discord.NotFound, discord.HTTPException):
                    return None

            results = await asyncio.gather(*[_check_guild(g) for g in missing_in_cache], return_exceptions=True)
            for res in results:
                if isinstance(res, discord.Guild):
                    mutual.append(res)

        return mutual

    @classmethod
    def _match_faculty_role_in_list(cls, roles: Sequence[discord.Role], target_name: str) -> discord.Role | None:
        """
        Multi-tier dynamic matcher to find an existing faculty role in a list/sequence of roles:
        Tier 1: Exact match (name == target_name)
        Tier 2: Case-insensitive & whitespace-trimmed match (name.strip().upper() == target_name.upper())
        Tier 3: Normalized alphanumeric match (e.g. '[FOCS]' or '🎓 FOCS')
        Tier 4: Prefix / word-boundary / bracket match (e.g. 'FOCS - Computing' or 'Faculty of Computing (FOCS)')
        Tier 5: Full faculty name & aliases match from FACULTY_ALIASES (e.g. 'Faculty of Computing and Information Technology')
        Tier 6: Normalized alphanumeric alias match (stripping punctuation/brackets from aliases)

        CRITICAL GUARD: Excludes roles containing committee/council/staff qualifiers (e.g. 'FOCS SRC', 'FOCS Council', 'FOCS Exco').
        Prioritizes the role with the highest position if multiple matches exist.
        """
        if not roles:
            return None

        target_upper = target_name.strip().upper()
        target_alnum = re.sub(r"[^A-Za-z0-9]", "", target_upper)
        target_has_qualifier = bool(ROLE_QUALIFIER_PATTERN.search(target_name))
        aliases = FACULTY_ALIASES.get(target_name, [target_name])

        def _is_safe_role(r_name: str) -> bool:
            if not target_has_qualifier and ROLE_QUALIFIER_PATTERN.search(r_name):
                return False
            return True

        # Tier 1: Exact match
        exact_matches = [r for r in roles if getattr(r, "name", None) == target_name]
        if exact_matches:
            return max(exact_matches, key=lambda r: getattr(r, "position", 0))

        # Filter candidates for Tiers 2-6 to avoid matching SRC / Council / Exco roles
        safe_roles = [r for r in roles if _is_safe_role(getattr(r, "name", ""))]

        # Tier 2: Case-insensitive & trimmed match
        ci_matches = [
            r for r in safe_roles if getattr(r, "name", "").strip().upper() == target_upper
        ]
        if ci_matches:
            return max(ci_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 3: Normalized alphanumeric match
        alnum_matches = [
            r
            for r in safe_roles
            if re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "")).upper() == target_alnum
        ]
        if alnum_matches:
            return max(alnum_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 4: Prefix or word boundary or bracket match of acronym
        fuzzy_matches = []
        for r in safe_roles:
            r_name = getattr(r, "name", "").strip().upper()
            if not r_name:
                continue
            if (
                r_name.startswith(f"{target_upper} ")
                or r_name.startswith(f"{target_upper}-")
                or r_name.startswith(f"{target_upper}:")
                or f"({target_upper})" in r_name
                or f"[{target_upper}]" in r_name
                or bool(re.search(rf"\b{re.escape(target_upper)}\b", r_name))
            ):
                fuzzy_matches.append(r)

        if fuzzy_matches:
            return max(fuzzy_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 5 & 6: Full faculty name & dynamic aliases match
        alias_matches = []
        for r in safe_roles:
            r_name = getattr(r, "name", "").strip()
            if not r_name:
                continue
            r_lower = r_name.lower()
            r_clean = re.sub(r"[^A-Za-z0-9]", "", r_lower)
            for alias in aliases:
                a_lower = alias.lower()
                a_clean = re.sub(r"[^A-Za-z0-9]", "", a_lower)
                # Exact alias match or stripped alphanumeric match
                if r_lower == a_lower or (a_clean and r_clean == a_clean):
                    alias_matches.append(r)
                    break
                # Word boundary match for full alias (if length > 3 to avoid acronym collisions)
                if len(alias) > 3 and re.search(rf"\b{re.escape(alias)}\b", r_name, re.IGNORECASE):
                    alias_matches.append(r)
                    break

        if alias_matches:
            return max(alias_matches, key=lambda r: getattr(r, "position", 0))

        return None

    async def restore_src_roles(self, guild: discord.Guild) -> dict[str, int]:
        """
        Restores / creates the faculty SRC (Student Representative Council) roles if missing.
        FAFB SRC, CPUS SRC, FOCS SRC, FCCI SRC, FOAS SRC, FOBE SRC, FSSH SRC, FOET SRC.
        Guarantees idempotency via per-guild role lock.
        """
        stats = {"created": 0, "existing": 0, "failed": 0}
        if not guild or not hasattr(guild, "roles"):
            return stats

        can_manage = (
            getattr(guild.me.guild_permissions, "manage_roles", False)
            if hasattr(guild, "me") and hasattr(guild.me, "guild_permissions")
            else False
        )
        if not can_manage:
            stats["failed"] = len(SRC_ROLES)
            return stats

        lock = self._get_guild_role_lock(guild.id)
        async with lock:
            guild_roles = list(getattr(guild, "roles", []))
            if hasattr(guild, "fetch_roles") and callable(guild.fetch_roles):
                try:
                    live_roles = await guild.fetch_roles()
                    if isinstance(live_roles, (list, tuple)):
                        guild_roles = list(live_roles)
                except (discord.HTTPException, discord.Forbidden):
                    pass

            for fac_code, src_name in SRC_ROLES.items():
                # Check if role already exists (exact or case-insensitive)
                exists = any(getattr(r, "name", "").strip().lower() == src_name.lower() for r in guild_roles)
                if exists:
                    stats["existing"] += 1
                    continue

                # Role missing, create it
                color_val = FACULTY_COLORS.get(fac_code, 0x3498DB)
                try:
                    role = await guild.create_role(
                        name=src_name,
                        colour=discord.Colour(color_val),
                        mentionable=True,
                        reason="TARVeri: restore missing faculty SRC role",
                    )
                    guild_roles.append(role)
                    try:
                        await self.db.record_bot_created_role(guild.id, role.id, src_name)
                    except Exception as e:
                        logger.debug(f"Could not record bot created SRC role: {e}")
                    stats["created"] += 1
                    await self.db.log(
                        "INFO",
                        "SRC_ROLE_RESTORED",
                        f"Restored SRC role '{src_name}' in '{guild.name}' (Guild ID: {guild.id})",
                        guild=guild,
                    )
                except discord.HTTPException as e:
                    stats["failed"] += 1
                    logger.warning(f"Failed to restore SRC role '{src_name}' in '{guild.name}': {e}")

        if stats["created"] > 0:
            logger.info(f"[{guild.name}] Restored {stats['created']} missing SRC role(s).")

        return stats

    async def find_faculty_role(self, guild: discord.Guild, role_name: str) -> discord.Role | None:
        """
        Finds an existing faculty role in a guild by searching in-memory cache first,
        and querying Discord REST API (fetch_roles) as a fallback to guarantee no duplicates.
        """
        # 1. Check in-memory guild.roles cache
        guild_roles = getattr(guild, "roles", [])
        if isinstance(guild_roles, (list, tuple)):
            found = self._match_faculty_role_in_list(guild_roles, role_name)
            if found is not None:
                return found

        # 2. If not found in cache, fetch live roles from Discord API
        if hasattr(guild, "fetch_roles") and callable(guild.fetch_roles):
            try:
                live_roles = await guild.fetch_roles()
                if isinstance(live_roles, (list, tuple)):
                    found = self._match_faculty_role_in_list(live_roles, role_name)
                    if found is not None:
                        return found
            except (discord.HTTPException, discord.Forbidden):
                pass

        return None

    async def get_or_create_faculty_role(self, guild: discord.Guild, role_name: str) -> discord.Role | None:
        """
        Finds an existing faculty role. ONLY creates a new role if the role absolutely does not exist.
        Guarantees idempotency via double-checked locking across concurrent tasks.
        """
        # 1. Exhaustive search across cache and live API
        existing_role = await self.find_faculty_role(guild, role_name)
        if existing_role is not None:
            return existing_role

        # 2. Acquire per-guild lock for atomic role creation
        lock = self._get_guild_role_lock(guild.id)
        async with lock:
            # Re-check under lock (double-checked locking)
            existing_role = await self.find_faculty_role(guild, role_name)
            if existing_role is not None:
                return existing_role

            # 3. Check if bot has Manage Roles permission before attempting creation
            can_manage = (
                getattr(guild.me.guild_permissions, "manage_roles", False)
                if hasattr(guild, "me") and hasattr(guild.me, "guild_permissions")
                else False
            )
            if not can_manage:
                return None

            # 4. Create the role only when absolutely not found anywhere
            try:
                color_val = FACULTY_COLORS.get(role_name, 0x3498DB)
                role = await guild.create_role(
                    name=role_name,
                    colour=discord.Colour(color_val),
                    mentionable=True,
                    reason="TARVeri: auto-created missing faculty role for verification",
                )
                try:
                    await self.db.record_bot_created_role(guild.id, role.id, role_name)
                except Exception as e:
                    logger.debug(f"Could not record bot created faculty role: {e}")
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

    async def find_alumni_role(self, guild: discord.Guild) -> discord.Role | None:
        """Finds existing alumni role in guild cache or live API."""
        guild_roles = getattr(guild, "roles", [])
        if isinstance(guild_roles, (list, tuple)):
            for r in guild_roles:
                name = getattr(r, "name", "").strip().lower()
                if name in ("tarumt alumni", "alumni"):
                    return r
        if hasattr(guild, "fetch_roles") and callable(guild.fetch_roles):
            try:
                live_roles = await guild.fetch_roles()
                if isinstance(live_roles, (list, tuple)):
                    for r in live_roles:
                        name = getattr(r, "name", "").strip().lower()
                        if name in ("tarumt alumni", "alumni"):
                            return r
            except (discord.HTTPException, discord.Forbidden):
                pass
        return None

    async def get_or_create_alumni_role(self, guild: discord.Guild) -> discord.Role | None:
        """Finds or atomically creates the TARUMT Alumni role."""
        existing = await self.find_alumni_role(guild)
        if existing is not None:
            return existing

        lock = self._get_guild_role_lock(guild.id)
        async with lock:
            existing = await self.find_alumni_role(guild)
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
                role = await guild.create_role(
                    name=ALUMNI_ROLE_NAME,
                    colour=discord.Colour(ALUMNI_ROLE_COLOR),
                    mentionable=True,
                    reason="TARVeri: auto-created missing TARUMT Alumni role",
                )
                try:
                    await self.db.record_bot_created_role(guild.id, role.id, ALUMNI_ROLE_NAME)
                except Exception as e:
                    logger.debug(f"Could not record bot created alumni role: {e}")
                await self.db.log(
                    "INFO",
                    "ROLE_CREATED",
                    f"Created alumni role '{ALUMNI_ROLE_NAME}' in '{guild.name}' (Guild ID: {guild.id})",
                    guild=guild,
                )
                return role
            except discord.HTTPException as e:
                await self.db.log(
                    "ERROR",
                    "ROLE_CREATE_FAILED",
                    f"Failed to create role '{ALUMNI_ROLE_NAME}' in '{guild.name}': {e}",
                    guild=guild,
                )
                return None

    @classmethod
    def _match_campus_role_in_list(cls, roles: Sequence[discord.Role], target_name: str) -> discord.Role | None:
        """Dynamic matcher to find an existing branch campus role in a list of roles."""
        if not roles or not target_name:
            return None

        target_upper = target_name.strip().upper()
        target_alnum = re.sub(r"[^A-Za-z0-9]", "", target_upper)
        aliases = CAMPUS_ALIASES.get(target_name, [target_name])

        for r in roles:
            if getattr(r, "name", None) == target_name:
                return r

        for r in roles:
            if getattr(r, "name", "").strip().upper() == target_upper:
                return r

        for alias in aliases:
            alias_upper = alias.strip().upper()
            for r in roles:
                if getattr(r, "name", "").strip().upper() == alias_upper:
                    return r

        for r in roles:
            r_alnum = re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "").upper())
            if r_alnum == target_alnum and r_alnum:
                return r

        return None

    async def find_campus_role(self, guild: discord.Guild, campus_name: str) -> discord.Role | None:
        """Finds existing campus role in guild cache or live API."""
        if not guild or not campus_name:
            return None
        guild_roles = getattr(guild, "roles", [])
        if isinstance(guild_roles, (list, tuple)):
            found = self._match_campus_role_in_list(guild_roles, campus_name)
            if found is not None:
                return found
        if hasattr(guild, "fetch_roles") and callable(guild.fetch_roles):
            try:
                live_roles = await guild.fetch_roles()
                if isinstance(live_roles, (list, tuple)):
                    found = self._match_campus_role_in_list(live_roles, campus_name)
                    if found is not None:
                        return found
            except (discord.HTTPException, discord.Forbidden):
                pass
        return None

    async def get_or_create_campus_role(self, guild: discord.Guild, campus_name: str) -> discord.Role | None:
        """Finds or atomically creates a branch campus role."""
        if not guild or not campus_name:
            return None
        existing = await self.find_campus_role(guild, campus_name)
        if existing is not None:
            return existing

        lock = self._get_guild_role_lock(guild.id)
        async with lock:
            existing = await self.find_campus_role(guild, campus_name)
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
                color_val = CAMPUS_COLORS.get(campus_name, 0x3498DB)
                role = await guild.create_role(
                    name=campus_name,
                    colour=discord.Colour(color_val),
                    mentionable=True,
                    reason="TARVeri: auto-created campus branch role for student verification",
                )
                try:
                    await self.db.record_bot_created_role(guild.id, role.id, campus_name)
                except Exception as e:
                    logger.debug(f"Could not record bot created campus role: {e}")
                await self.db.log(
                    "INFO",
                    "ROLE_CREATED",
                    f"Created campus role '{campus_name}' in '{guild.name}' (Guild ID: {guild.id})",
                    guild=guild,
                )
                return role
            except Exception as e:
                await self.db.log(
                    "ERROR",
                    "ROLE_CREATE_FAILED",
                    f"Failed to create campus role '{campus_name}' in '{guild.name}': {e}",
                    guild=guild,
                )
                return None

    @classmethod
    def _match_study_level_role_in_list(cls, roles: Sequence[discord.Role], target_name: str) -> discord.Role | None:
        """Dynamic matcher to find an existing study level role in a list of roles."""
        if not roles or not target_name:
            return None

        target_upper = target_name.strip().upper()
        target_alnum = re.sub(r"[^A-Za-z0-9]", "", target_upper)
        aliases = STUDY_LEVEL_ALIASES.get(target_name, [target_name])

        for r in roles:
            if getattr(r, "name", None) == target_name:
                return r

        for r in roles:
            if getattr(r, "name", "").strip().upper() == target_upper:
                return r

        for alias in aliases:
            alias_upper = alias.strip().upper()
            for r in roles:
                if getattr(r, "name", "").strip().upper() == alias_upper:
                    return r

        for r in roles:
            r_alnum = re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "").upper())
            if r_alnum == target_alnum and r_alnum:
                return r

        return None

    async def find_study_level_role(self, guild: discord.Guild, level_name: str) -> discord.Role | None:
        """Finds existing study level role in guild cache or live API."""
        if not guild or not level_name:
            return None
        guild_roles = getattr(guild, "roles", [])
        if isinstance(guild_roles, (list, tuple)):
            found = self._match_study_level_role_in_list(guild_roles, level_name)
            if found is not None:
                return found
        if hasattr(guild, "fetch_roles") and callable(guild.fetch_roles):
            try:
                live_roles = await guild.fetch_roles()
                if isinstance(live_roles, (list, tuple)):
                    found = self._match_study_level_role_in_list(live_roles, level_name)
                    if found is not None:
                        return found
            except (discord.HTTPException, discord.Forbidden):
                pass
        return None

    async def get_or_create_study_level_role(self, guild: discord.Guild, level_name: str) -> discord.Role | None:
        """Finds or atomically creates a study level role."""
        if not guild or not level_name:
            return None
        existing = await self.find_study_level_role(guild, level_name)
        if existing is not None:
            return existing

        lock = self._get_guild_role_lock(guild.id)
        async with lock:
            existing = await self.find_study_level_role(guild, level_name)
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
                color_val = STUDY_LEVEL_COLORS.get(level_name, 0x2980B9)
                role = await guild.create_role(
                    name=level_name,
                    colour=discord.Colour(color_val),
                    mentionable=True,
                    reason="TARVeri: auto-created study level role for student verification",
                )
                try:
                    await self.db.record_bot_created_role(guild.id, role.id, level_name)
                except Exception as e:
                    logger.debug(f"Could not record bot created study level role: {e}")
                await self.db.log(
                    "INFO",
                    "ROLE_CREATED",
                    f"Created study level role '{level_name}' in '{guild.name}' (Guild ID: {guild.id})",
                    guild=guild,
                )
                return role
            except Exception as e:
                await self.db.log(
                    "ERROR",
                    "ROLE_CREATE_FAILED",
                    f"Failed to create study level role '{level_name}' in '{guild.name}': {e}",
                    guild=guild,
                )
                return None

    async def _assign_role_in_guild(
        self,
        guild: discord.Guild,
        user_id: int,
        role_name: str,
        result: RoleSyncResult,
        campus_role_name: str | None = None,
        level_role_name: str | None = None,
    ) -> None:
        """Process role assignment in a single guild (faculty role + campus role + study level role)."""
        member = await self.get_or_fetch_member(guild, user_id)
        if member is None:
            return

        member_roles = getattr(member, "roles", [])
        if not isinstance(member_roles, (list, tuple)):
            member_roles = []

        # Check if member already holds the target faculty role
        has_target_faculty = self._match_faculty_role_in_list(member_roles, role_name) is not None

        # Check for any conflicting faculty roles (e.g. manually selected a different faculty role prior)
        conflicting_faculty_roles = [
            r for r in member_roles
            if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES if fac != role_name)
        ]

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top_role = getattr(me, "top_role", None) if me else None
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0

        roles_to_add: list[discord.Role] = []
        roles_to_remove: list[discord.Role] = []
        assigned_names: list[str] = []

        # 1. Primary faculty role
        if not has_target_faculty:
            fac_role = await self.get_or_create_faculty_role(guild, role_name)
            if not fac_role:
                result.missing_role_in.append(guild.name)
                return
            fac_pos = getattr(fac_role, "position", 0)
            if not can_manage or (isinstance(bot_pos, int) and isinstance(fac_pos, int) and fac_pos >= bot_pos):
                result.failed_in.append(guild.name)
                return
            roles_to_add.append(fac_role)
            fac_name = getattr(fac_role, "name", None)
            assigned_names.append(fac_name if isinstance(fac_name, str) and fac_name else role_name)
        else:
            assigned_names.append(role_name)

        # Queue removal of conflicting faculty roles if bot has permission
        for conf_r in conflicting_faculty_roles:
            conf_pos = getattr(conf_r, "position", 0)
            if can_manage and not (isinstance(bot_pos, int) and isinstance(conf_pos, int) and conf_pos >= bot_pos):
                roles_to_remove.append(conf_r)

        # 2. Branch Campus role (if provided)
        if campus_role_name:
            has_campus = any(self._match_campus_role_in_list([r], campus_role_name) is not None for r in member_roles)
            if not has_campus:
                camp_role = await self.get_or_create_campus_role(guild, campus_role_name)
                if camp_role:
                    camp_pos = getattr(camp_role, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(camp_pos, int) and camp_pos >= bot_pos):
                        roles_to_add.append(camp_role)
                        camp_name = getattr(camp_role, "name", None)
                        assigned_names.append(camp_name if isinstance(camp_name, str) and camp_name else campus_role_name)
            else:
                assigned_names.append(campus_role_name)

        # 3. Study Level role (if provided)
        if level_role_name:
            has_level = any(self._match_study_level_role_in_list([r], level_role_name) is not None for r in member_roles)
            if not has_level:
                lvl_role = await self.get_or_create_study_level_role(guild, level_role_name)
                if lvl_role:
                    lvl_pos = getattr(lvl_role, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(lvl_pos, int) and lvl_pos >= bot_pos):
                        roles_to_add.append(lvl_role)
                        lvl_name = getattr(lvl_role, "name", None)
                        assigned_names.append(lvl_name if isinstance(lvl_name, str) and lvl_name else level_role_name)
            else:
                assigned_names.append(level_role_name)

        # Perform role modifications
        roles_modified = False
        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason="TARVeri: Reconcile faculty role mismatch")
                roles_modified = True
            except discord.HTTPException as e:
                logger.warning(f"Failed to remove conflicting roles for user {user_id} in '{guild.name}': {e}")

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="TARVeri: Student verification role assignment")
                roles_modified = True
            except discord.HTTPException as e:
                result.failed_in.append(guild.name)
                await self.db.log(
                    "ERROR",
                    "ROLE_ASSIGN_FAILED",
                    f"Failed to assign roles to user {user_id} in '{guild.name}': {e}",
                    guild=guild,
                    user_id=user_id,
                )
                return

        summary_label = ", ".join(dict.fromkeys(assigned_names))
        if roles_modified:
            result.verified_in.append((guild.id, guild.name, summary_label))
        elif has_target_faculty:
            result.already_had_role_in.append((guild.id, guild.name, summary_label))

    async def assign_role_across_guilds(
        self,
        user_id: int,
        role_name: str,
        guilds: Sequence[discord.Guild],
        campus_role_name: str | None = None,
        level_role_name: str | None = None,
    ) -> RoleSyncResult:
        """
        Ensures the given user holds `role_name` (and optional campus & level roles) in all specified guilds concurrently.
        """
        result = RoleSyncResult()
        if not guilds:
            return result

        tasks = [
            self._assign_role_in_guild(
                g,
                user_id,
                role_name,
                result,
                campus_role_name=campus_role_name,
                level_role_name=level_role_name,
            )
            for g in guilds
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        return result

    def format_role_summary(self, result: RoleSyncResult) -> str:
        """Formats a human-readable summary of role assignments."""
        lines: list[str] = []
        if result.verified_in:
            lines.append("✅ You've been given the following role(s):")
            lines.extend(
                f"   • **{item[1] if len(item) == 3 else item[0]}** → {item[2] if len(item) == 3 else item[1]}"
                for item in result.verified_in
            )
        if result.already_had_role_in:
            lines.append("ℹ️ You already had a faculty role in:")
            lines.extend(
                f"   • **{item[1] if len(item) == 3 else item[0]}** → {item[2] if len(item) == 3 else item[1]} (unchanged)"
                for item in result.already_had_role_in
            )
            if not result.verified_in:
                lines.append("✅ Your student status is now officially verified in our database.")
        if result.missing_role_in:
            lines.append("⚠️ I couldn't create/find the required role (contact an admin) in:")
            lines.extend(f"   • **{g}** (I likely need 'Manage Roles' permission there)" for g in result.missing_role_in)
        if result.failed_in:
            lines.append("⚠️ I don't have permission to assign roles in:")
            lines.extend(f"   • **{g}** (my role needs to be moved above the faculty roles)" for g in result.failed_in)
        return "\n".join(lines)

    async def perform_verification(self, user: discord.User | discord.Member, raw_student_id: str) -> str:
        """
        Core verification pipeline:
        1. Rate limit validation
        2. In-flight race condition check
        3. Student ID format and faculty/campus/level code extraction
        4. Account / duplicate ID verification checks
        5. Role assignment across mutual guilds
        6. Atomic database recording with rollback on collision
        """
        guild_ctx = getattr(user, "guild", None)
        if self.rate_limiter.is_rate_limited(user.id):
            await self.db.log(
                "WARNING",
                "RATE_LIMITED",
                f"{user} hit the attempt limit",
                user_id=user.id,
                guild=guild_ctx,
            )
            return (
                "⏳ You've made too many verification attempts. Please wait a few minutes "
                "and try again, or contact an admin if this is a mistake."
            )

        async with self._lock:
            if user.id in self._in_flight_users:
                return "⏳ Your verification is already being processed. Please wait a moment."
            self._in_flight_users.add(user.id)

        self.rate_limiter.record_attempt(user.id)

        try:
            info = parse_student_id(raw_student_id)
            if not info.is_valid or not info.faculty_code or not info.faculty_role:
                if not info.student_id:
                    return "❌ Please provide a valid student ID (e.g., `23WMD09867`)."
                if info.faculty_code and info.faculty_code not in FACULTY_ROLES:
                    return "❌ Student ID does not match any known faculty. Please check and try again."
                return "❌ Invalid student ID format. Please use the format like `23WMD09867`."

            student_id = info.student_id
            faculty_code = info.faculty_code
            role_name = info.faculty_role
            campus_code = info.campus_code
            campus_role_name = info.campus_role
            level_code = info.level_code
            level_role_name = info.level_role

            id_hash = hash_student_id(student_id, self.secret)

            # Check if user is already verified
            existing_for_user = await self.db.get_verification_by_user(user.id)
            if existing_for_user:
                stored_hash, stored_faculty, _ = existing_for_user
                if stored_hash != id_hash:
                    return (
                        "ℹ️ This Discord account is already verified under a different student ID. "
                        "If you need to change the ID on file (e.g. account transfer), contact an admin."
                    )

                mutual_guilds = await self.get_mutual_guilds_for_user(user.id)
                assigned_faculty_role = FACULTY_ROLES.get(stored_faculty, role_name)
                sync_result = await self.assign_role_across_guilds(
                    user.id,
                    assigned_faculty_role,
                    mutual_guilds,
                    campus_role_name=campus_role_name,
                    level_role_name=level_role_name,
                )
                try:
                    await self.db.update_verification_details(
                        user.id,
                        campus_code=campus_code,
                        level_code=level_code,
                    )
                except Exception:
                    pass
                summary = self.format_role_summary(sync_result)
                return summary or "ℹ️ You're already verified and up to date in every server I share with you."

            # Check if student ID is already bound to another Discord account
            existing_for_id = await self.db.get_verification_by_id_hash(id_hash)
            if existing_for_id:
                await self.db.log(
                    "WARNING",
                    "DUPLICATE_ID_ATTEMPT",
                    f"{user} (ID: {user.id}) tried to reuse a student ID "
                    f"(masked: {mask_student_id(student_id)}) already bound to account ID {existing_for_id[0]}",
                    user_id=user.id,
                    guild=guild_ctx,
                )
                return (
                    "❌ This student ID has already been used to verify a different Discord "
                    "account. If that wasn't you, contact an admin immediately."
                )

            mutual_guilds = await self.get_mutual_guilds_for_user(user.id)
            if not mutual_guilds:
                return "⚠️ I couldn't find you in any server I'm in. Please join the server first, then try again."

            sync_result = await self.assign_role_across_guilds(
                user.id,
                role_name,
                mutual_guilds,
                campus_role_name=campus_role_name,
                level_role_name=level_role_name,
            )

            # Persist if role was granted or user already held the role in at least one server
            if sync_result.verified_in or sync_result.already_had_role_in:
                try:
                    await self.db.record_verification(
                        user.id,
                        id_hash,
                        faculty_code,
                        campus_code=campus_code,
                        level_code=level_code,
                    )
                    active_servers = [
                        entry[1] if len(entry) == 3 else entry[0]
                        for entry in (sync_result.verified_in + sync_result.already_had_role_in)
                    ]
                    await self.db.log(
                        "INFO",
                        "VERIFIED",
                        f"{user} (ID: {user.id}) verified (student ID masked: {mask_student_id(student_id)}) "
                        f"→ active in {active_servers}",
                        user_id=user.id,
                        guild=guild_ctx,
                    )
                except (sqlite3.IntegrityError, aiosqlite.IntegrityError) as e:
                    # Rollback assigned roles if database collision occurs
                    for entry in sync_result.verified_in:
                        if len(entry) == 3:
                            g_id, _, r_names = entry
                            guild = self.bot.get_guild(g_id)
                        else:
                            g_name, r_names = entry
                            guild = discord.utils.get(self.bot.guilds, name=g_name)

                        if guild:
                            member = await self.get_or_fetch_member(guild, user.id)
                            if member:
                                for single_r in r_names.split(", "):
                                    r = discord.utils.get(guild.roles, name=single_r.strip())
                                    if r and r in member.roles:
                                        try:
                                            await member.remove_roles(
                                                r, reason="TARVeri: Rollback due to database collision"
                                            )
                                        except discord.HTTPException:
                                            pass
                    await self.db.log(
                        "ERROR",
                        "INTEGRITY_CONFLICT",
                        f"Verification collision for {user} (ID: {user.id}): {e}",
                        user_id=user.id,
                        guild=guild_ctx,
                    )
                    return (
                        "❌ Verification failed due to a collision (the student ID or your account was just verified elsewhere). "
                        "Please contact an admin if this persists."
                    )

            summary = self.format_role_summary(sync_result)
            return summary or "⚠️ Verification completed, but no roles could be assigned."
        finally:
            async with self._lock:
                self._in_flight_users.discard(user.id)

    async def reconcile_verified_members(
        self,
        guild: discord.Guild,
        default_campus: str = "W",
        default_level: str | None = None,
    ) -> dict[str, int]:
        """
        Self-healing: cross-references current guild members against the verifications table.
        If a student verified in the database is missing their faculty, campus, or study level
        roles in this guild, automatically restores and synchronizes them.
        """
        summary = {"checked": 0, "restored": 0, "failed": 0}
        if not guild:
            return summary

        # Ensure guild member cache is populated if chunk method exists
        if hasattr(guild, "chunk") and not getattr(guild, "chunked", True):
            try:
                await guild.chunk()
            except Exception:
                pass

        all_verifications = await self.db.get_all_verifications()
        if not all_verifications:
            return summary

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top_role = getattr(me, "top_role", None) if me else None
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0

        for discord_user_id, _, faculty_code, _ in all_verifications:
            member = await self.get_or_fetch_member(guild, discord_user_id)
            if not member:
                continue

            summary["checked"] += 1
            member_roles = getattr(member, "roles", [])
            roles_to_add: list[discord.Role] = []

            # 1. Primary faculty role
            target_role_name = FACULTY_ROLES.get(faculty_code)
            has_faculty_role = False
            if target_role_name and isinstance(member_roles, (list, tuple)):
                has_faculty_role = self._match_faculty_role_in_list(member_roles, target_role_name) is not None

                # Clean up any obsolete/conflicting other faculty roles
                conflicting_fac_roles = [
                    r for r in member_roles
                    if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES if fac != target_role_name)
                ]
                for conf_r in conflicting_fac_roles:
                    conf_pos = getattr(conf_r, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(conf_pos, int) and conf_pos >= bot_pos):
                        try:
                            await member.remove_roles(conf_r, reason="TARVeri: Self-healing faculty role mismatch")
                        except discord.HTTPException:
                            pass

            if not has_faculty_role and target_role_name:
                target_role = await self.get_or_create_faculty_role(guild, target_role_name)
                if target_role:
                    role_pos = getattr(target_role, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos >= bot_pos):
                        roles_to_add.append(target_role)
                    else:
                        summary["failed"] += 1
                else:
                    summary["failed"] += 1

            # 2. Campus branch role
            details = await self.db.get_verification_details(discord_user_id)
            c_code = details.get("campus_code") if details else None

            # Detect if member already holds a campus role in Discord
            existing_campus_code = None
            has_campus_role = False
            if isinstance(member_roles, (list, tuple)):
                for r in member_roles:
                    for code_k, name_v in CAMPUS_ROLES.items():
                        if self._match_campus_role_in_list([r], name_v) is not None:
                            has_campus_role = True
                            existing_campus_code = code_k
                            break
                    if has_campus_role:
                        break

            if has_campus_role and existing_campus_code and (not c_code or c_code != existing_campus_code):
                try:
                    await self.db.update_verification_details(discord_user_id, campus_code=existing_campus_code)
                    c_code = existing_campus_code
                except Exception:
                    pass

            if not c_code:
                c_code = default_campus

            target_campus_name = CAMPUS_ROLES.get(c_code, "KL Main Campus")
            if not has_campus_role and target_campus_name:
                target_camp = await self.get_or_create_campus_role(guild, target_campus_name)
                if target_camp:
                    camp_pos = getattr(target_camp, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(camp_pos, int) and camp_pos >= bot_pos):
                        roles_to_add.append(target_camp)

            # 3. Study level role
            l_code = details.get("level_code") if details else None

            # Detect if member already holds a study level role in Discord
            existing_level_code = None
            has_level_role = False
            if isinstance(member_roles, (list, tuple)):
                for r in member_roles:
                    for code_k, name_v in STUDY_LEVEL_ROLES.items():
                        if self._match_study_level_role_in_list([r], name_v) is not None:
                            has_level_role = True
                            existing_level_code = code_k
                            break
                    if has_level_role:
                        break

            if has_level_role and existing_level_code and (not l_code or l_code != existing_level_code):
                try:
                    await self.db.update_verification_details(discord_user_id, level_code=existing_level_code)
                    l_code = existing_level_code
                except Exception:
                    pass

            if not l_code and default_level:
                l_code = default_level

            if l_code:
                target_level_name = STUDY_LEVEL_ROLES.get(l_code)
                if target_level_name and not has_level_role:
                    target_lvl = await self.get_or_create_study_level_role(guild, target_level_name)
                    if target_lvl:
                        lvl_pos = getattr(target_lvl, "position", 0)
                        if can_manage and not (isinstance(bot_pos, int) and isinstance(lvl_pos, int) and lvl_pos >= bot_pos):
                            roles_to_add.append(target_lvl)

            if roles_to_add:
                try:
                    await member.add_roles(
                        *roles_to_add,
                        reason="TARVeri: Self-healing automatic role restoration for verified student",
                    )
                    summary["restored"] += len(roles_to_add)
                    assigned_labels = ", ".join(
                        [getattr(r, "name", "Role") for r in roles_to_add if isinstance(getattr(r, "name", None), str)]
                    ) or "roles"
                    await self.db.log(
                        "INFO",
                        "ROLE_RESTORED",
                        f"Self-healing: Restored missing role(s) [{assigned_labels}] to verified student {member} (ID: {discord_user_id})",
                        guild=guild,
                        user_id=discord_user_id,
                    )
                except discord.HTTPException as e:
                    summary["failed"] += len(roles_to_add)
                    logger.warning(
                        f"Failed to restore roles for {member} in '{guild.name}': {e}"
                    )

        if summary["restored"] > 0:
            logger.info(
                f"[{guild.name}] Self-healing verified member reconciliation: "
                f"Checked {summary['checked']}, Restored {summary['restored']}, Failed {summary['failed']}"
            )

        return summary

    async def backfill_branch_roles(
        self,
        guild: discord.Guild | None = None,
        default_campus_code: str = "W",
        default_level_code: str | None = None,
    ) -> dict[str, int]:
        """
        One-time migration helper:
        1. Backfills legacy DB records where campus_code is NULL to default_campus_code.
        2. If default_level_code is given, backfills DB records where level_code is NULL.
        3. Iterates over specified guild (or all shared guilds) and assigns missing branch campus
           and study level roles to verified students.
        """
        stats = {
            "guilds_scanned": 0,
            "members_checked": 0,
            "roles_assigned": 0,
            "db_migrated": 0,
            "failed": 0,
        }
        stats["db_migrated"] = await self.db.backfill_legacy_verifications(default_campus=default_campus_code)
        if default_level_code and self.db._conn:
            cursor = await self.db._conn.execute(
                "UPDATE verifications SET level_code = ? WHERE level_code IS NULL",
                (default_level_code,),
            )
            await self.db._conn.commit()
            stats["db_migrated"] += cursor.rowcount

        target_guilds = [guild] if guild else list(self.bot.guilds)
        for g in target_guilds:
            if not g:
                continue
            stats["guilds_scanned"] += 1
            g_summary = await self.reconcile_verified_members(
                g,
                default_campus=default_campus_code,
                default_level=default_level_code,
            )
            stats["members_checked"] += g_summary.get("checked", 0)
            stats["roles_assigned"] += g_summary.get("restored", 0)
            stats["failed"] += g_summary.get("failed", 0)

        return stats

    async def claim_alumni_status(
        self,
        user_id: int,
        user_display_name: str,
        graduated_year: int,
        programme: str | None = None,
        current_guild: discord.Guild | None = None,
    ) -> dict[str, Any]:
        """Processes instant alumni transition for an already-verified student."""
        verif = await self.db.get_verification_by_user(user_id)
        if not verif:
            return {
                "success": False,
                "error": "NOT_VERIFIED",
                "message": "You must be a verified TARUMT student before claiming Alumni status. Please run `/verify` first.",
            }

        # Validate year (from 1969 TAR College founding to realistic graduation window)
        current_year = 2026
        if graduated_year < 1969 or graduated_year > current_year + 5:
            return {
                "success": False,
                "error": "INVALID_YEAR",
                "message": f"Please provide a valid graduation year (1969–{current_year + 5}).",
            }

        # Record in database
        clean_prog = programme.strip() if programme and programme.strip() else None
        await self.db.record_alumni_claim(user_id, graduated_year, clean_prog)

        # Assign role across mutual guilds
        mutual_guilds = await self.get_mutual_guilds_for_user(user_id)
        roles_assigned: list[str] = []

        for guild in mutual_guilds:
            member = await self.get_or_fetch_member(guild, user_id)
            if not member:
                continue

            alumni_role = await self.get_or_create_alumni_role(guild)
            if not alumni_role:
                continue

            if alumni_role not in getattr(member, "roles", []):
                me = getattr(guild, "me", None)
                can_manage = (
                    getattr(me.guild_permissions, "manage_roles", False)
                    if me and hasattr(me, "guild_permissions")
                    else False
                )
                bot_top = getattr(me, "top_role", None)
                bot_pos = getattr(bot_top, "position", 0) if bot_top else 0
                role_pos = getattr(alumni_role, "position", 0)
                if can_manage and role_pos < bot_pos:
                    try:
                        await member.add_roles(
                            alumni_role,
                            reason=f"TARVeri: Claimed Alumni status (Class of {graduated_year})",
                        )
                        roles_assigned.append(guild.name)
                    except discord.HTTPException as e:
                        logger.warning(f"Could not assign alumni role to {member} in {guild.name}: {e}")

        stored_faculty = verif[1]
        faculty_name = FACULTY_ROLES.get(stored_faculty, stored_faculty)

        await self.db.log(
            "INFO",
            "ALUMNI_CLAIMED",
            f"Student {user_display_name} (ID: {user_id}) claimed Alumni status: Class of {graduated_year} • {clean_prog or 'N/A'} (Assigned in {len(roles_assigned)} servers)",
            guild=current_guild,
            user_id=user_id,
        )

        return {
            "success": True,
            "graduated_year": graduated_year,
            "programme": clean_prog,
            "faculty_code": stored_faculty,
            "faculty_name": faculty_name,
            "guilds_updated": len(roles_assigned),
        }

    async def revoke_alumni_status(
        self,
        target_user: discord.User | discord.Member,
        admin: discord.User | discord.Member,
        current_guild: discord.Guild | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Revokes alumni status from a user and removes alumni roles."""
        alumni_info = await self.db.get_alumni_info_by_user(target_user.id)
        if not alumni_info or not alumni_info.get("is_alumni"):
            return {
                "success": False,
                "error": "NOT_ALUMNI",
                "message": f"{target_user.mention} is not currently registered as an Alumni.",
            }

        # Remove role in mutual guilds
        mutual_guilds = await self.get_mutual_guilds_for_user(target_user.id)
        roles_removed: list[str] = []

        for guild in mutual_guilds:
            member = await self.get_or_fetch_member(guild, target_user.id)
            if not member:
                continue

            alumni_role = await self.find_alumni_role(guild)
            if alumni_role and alumni_role in getattr(member, "roles", []):
                me = getattr(guild, "me", None)
                can_manage = (
                    getattr(me.guild_permissions, "manage_roles", False)
                    if me and hasattr(me, "guild_permissions")
                    else False
                )
                bot_top = getattr(me, "top_role", None)
                bot_pos = getattr(bot_top, "position", 0) if bot_top else 0
                role_pos = getattr(alumni_role, "position", 0)
                if can_manage and role_pos < bot_pos:
                    try:
                        await member.remove_roles(
                            alumni_role,
                            reason=f"TARVeri: Alumni status revoked by {admin}. Reason: {reason or 'None'}",
                        )
                        roles_removed.append(guild.name)
                    except discord.HTTPException as e:
                        logger.warning(f"Could not remove alumni role from {member} in {guild.name}: {e}")

        await self.db.revoke_alumni_status(target_user.id)
        await self.db.log(
            "WARNING",
            "ALUMNI_REVOKED",
            f"Alumni status for {target_user} (ID: {target_user.id}) revoked by {admin}. Reason: {reason or 'No reason provided'}",
            guild=current_guild,
            user_id=target_user.id,
        )

        return {
            "success": True,
            "target_id": target_user.id,
            "roles_removed_count": len(roles_removed),
        }

    async def reconcile_alumni_members(self, guild: discord.Guild) -> dict[str, int]:
        """Self-healing: Ensures all registered alumni in the guild have the TARUMT Alumni role."""
        summary = {"checked": 0, "restored": 0, "failed": 0}
        if not guild:
            return summary

        alumni_ids = await self.db.get_all_alumni_user_ids()
        if not alumni_ids:
            return summary

        alumni_role = await self.get_or_create_alumni_role(guild)
        if not alumni_role:
            summary["failed"] = len(alumni_ids)
            return summary

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top = getattr(me, "top_role", None)
        bot_pos = getattr(bot_top, "position", 0) if bot_top else 0
        role_pos = getattr(alumni_role, "position", 0)
        if not can_manage or (isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos >= bot_pos):
            summary["failed"] = len(alumni_ids)
            return summary

        for user_id in alumni_ids:
            member = await self.get_or_fetch_member(guild, user_id)
            if not member:
                continue

            summary["checked"] += 1
            if alumni_role not in getattr(member, "roles", []):
                try:
                    await member.add_roles(
                        alumni_role,
                        reason="TARVeri: Self-healing automatic Alumni role restoration",
                    )
                    summary["restored"] += 1
                    await self.db.log(
                        "INFO",
                        "ROLE_RESTORED",
                        f"Self-healing: Restored missing Alumni role to {member} (ID: {user_id}) in '{guild.name}'",
                        guild=guild,
                        user_id=user_id,
                    )
                except discord.HTTPException as e:
                    summary["failed"] += 1
                    logger.warning(f"Could not restore alumni role for {member} in {guild.name}: {e}")

        if summary["restored"] > 0:
            logger.info(
                f"[{guild.name}] Self-healing alumni member reconciliation: "
                f"Checked {summary['checked']}, Restored {summary['restored']}, Failed {summary['failed']}"
            )

        return summary

    async def is_role_created_by_bot(
        self,
        guild: discord.Guild,
        role: discord.Role,
        bot_created_ids: set[int] | None = None,
    ) -> bool:
        """
        Checks whether a role was created by TARVeri (tracked in SQLite bot_created_roles
        or verified via Discord audit logs). Admin-created roles return False.
        """
        if not role or not guild:
            return False

        # 1. Fast in-memory / pre-fetched set check
        if bot_created_ids is not None and getattr(role, "id", None) in bot_created_ids:
            return True

        # 2. SQLite database lookup
        try:
            db_ids = await self.db.get_bot_created_role_ids(guild.id)
            if getattr(role, "id", None) in db_ids:
                return True
        except Exception as e:
            logger.debug(f"Could not query bot_created_role_ids for guild {guild.id}: {e}")

        # 3. Discord Audit Logs fallback if bot has View Audit Log permission
        me = getattr(guild, "me", None)
        can_view_audit = getattr(getattr(me, "guild_permissions", None), "view_audit_log", False)
        if can_view_audit and hasattr(guild, "audit_logs") and callable(guild.audit_logs):
            try:
                async for entry in guild.audit_logs(action=discord.AuditLogAction.role_create, limit=100):
                    if entry.target and entry.target.id == getattr(role, "id", None):
                        if entry.user and me and entry.user.id == me.id:
                            await self.db.record_bot_created_role(guild.id, role.id, getattr(role, "name", "unknown"))
                            return True
                        else:
                            return False
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass

        return False

    async def reconcile_duplicate_roles(self, guild: discord.Guild) -> dict[str, Any]:
        """
        Self-healing: scans guild for duplicate faculty and guest roles matching the same category.
        Identifies the primary role (highest position in hierarchy / highest member count),
        migrates all members on redundant duplicate role(s) to the primary role, and ONLY deletes
        redundant duplicate role(s) that were created by the bot (admin-created roles are preserved).
        """
        stats: dict[str, Any] = {
            "checked_categories": 0,
            "migrated_members": 0,
            "deleted_roles": 0,
            "failed": 0,
            "details": [],
        }
        if not guild:
            return stats

        # Ensure guild member cache is populated if chunk method exists
        if hasattr(guild, "chunk") and not getattr(guild, "chunked", True):
            try:
                await guild.chunk()
            except Exception:
                pass

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top_role = getattr(me, "top_role", None) if me else None
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0

        # Pre-fetch bot-created role IDs from DB for this guild
        try:
            bot_created_ids = await self.db.get_bot_created_role_ids(guild.id)
        except Exception:
            bot_created_ids = set()

        guild_roles = list(getattr(guild, "roles", []))

        def _role_rank(role: discord.Role, target_name: str) -> tuple[int, int, int]:
            pos = getattr(role, "position", 0) if isinstance(getattr(role, "position", 0), int) else 0
            member_count = len(getattr(role, "members", []))
            exact_match = 1 if getattr(role, "name", "").strip().lower() == target_name.strip().lower() else 0
            return (exact_match, pos, member_count)

        # 1. Group by faculty (strictly ignoring SRC, Council, Committee, and staff roles)
        category_roles: dict[str, list[discord.Role]] = {}
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name or ROLE_QUALIFIER_PATTERN.search(r_name):
                continue
            for fac in FACULTY_ROLE_NAMES:
                if self._match_faculty_role_in_list([r], fac) is not None:
                    category_roles.setdefault(fac, []).append(r)
                    break

        # 2. Group guest roles
        settings = await self.db.get_guild_settings(guild.id)
        configured_guest_name = settings[2].strip() if settings and len(settings) > 2 and settings[2] else None
        guest_roles: list[discord.Role] = []
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name or ROLE_QUALIFIER_PATTERN.search(r_name):
                continue
            if configured_guest_name and r_name.strip().lower() == configured_guest_name.lower():
                guest_roles.append(r)
            elif GUEST_ROLE_PATTERN.search(r_name):
                guest_roles.append(r)

        if guest_roles:
            category_roles["Guest"] = guest_roles

        # 3. Process categories with duplicates
        for cat_name, roles_found in category_roles.items():
            stats["checked_categories"] += 1
            if len(roles_found) <= 1:
                continue

            target_name = cat_name if cat_name != "Guest" else (configured_guest_name or "Guest(Approved)")
            sorted_roles = sorted(roles_found, key=lambda r: _role_rank(r, target_name), reverse=True)
            primary_role = sorted_roles[0]
            redundant_roles = sorted_roles[1:]

            for red_role in redundant_roles:
                is_bot_created = await self.is_role_created_by_bot(guild, red_role, bot_created_ids)

                # Migrate members
                red_members = list(getattr(red_role, "members", []))
                for member in red_members:
                    member_roles = getattr(member, "roles", [])
                    if primary_role not in member_roles:
                        try:
                            await member.add_roles(
                                primary_role,
                                reason=f"TARVeri Self-Healing: Migrate from duplicate role '{red_role.name}' to primary '{primary_role.name}'",
                            )
                            stats["migrated_members"] += 1
                        except (discord.HTTPException, discord.Forbidden) as e:
                            logger.warning(f"Failed to migrate member {member} to {primary_role.name}: {e}")
                            stats["failed"] += 1

                    if is_bot_created and red_role in getattr(member, "roles", []):
                        try:
                            await member.remove_roles(
                                red_role,
                                reason=f"TARVeri Self-Healing: Remove duplicate role '{red_role.name}'",
                            )
                        except (discord.HTTPException, discord.Forbidden):
                            pass

                # If NOT created by bot, preserve the admin-created role (do not delete!)
                if not is_bot_created:
                    detail_msg = f"Preserved admin-created role '{red_role.name}' (ID: {getattr(red_role, 'id', 'N/A')}) — only bot-created roles are deleted"
                    stats["details"].append(detail_msg)
                    logger.info(f"[{guild.name}] {detail_msg}")
                    continue

                # Delete bot-created redundant role
                red_pos = getattr(red_role, "position", 0)
                is_manageable = (
                    can_manage
                    and isinstance(bot_pos, int)
                    and isinstance(red_pos, int)
                    and red_pos < bot_pos
                    and not getattr(red_role, "managed", False)
                    and not (hasattr(red_role, "is_default") and red_role.is_default())
                )

                if is_manageable:
                    try:
                        await red_role.delete(
                            reason=f"TARVeri Self-Healing: Removed bot-created duplicate role '{red_role.name}' (migrated to '{primary_role.name}')"
                        )
                        try:
                            await self.db.delete_bot_created_role(red_role.id)
                        except Exception:
                            pass
                        stats["deleted_roles"] += 1
                        detail_msg = f"Deleted bot-created duplicate role '{red_role.name}' (migrated {len(red_members)} member(s) to '{primary_role.name}')"
                        stats["details"].append(detail_msg)
                        await self.db.log(
                            "INFO",
                            "DUPLICATE_ROLE_DELETED",
                            f"Self-Healing: [{guild.name}] {detail_msg}",
                            guild=guild,
                        )
                    except (discord.HTTPException, discord.Forbidden) as e:
                        stats["failed"] += 1
                        logger.warning(f"Failed to delete duplicate role {red_role.name} in {guild.name}: {e}")
                else:
                    stats["failed"] += 1
                    detail_msg = f"Cannot delete bot-created duplicate role '{red_role.name}' due to hierarchy/permissions (pos {red_pos} >= bot {bot_pos})"
                    stats["details"].append(detail_msg)
                    logger.warning(f"[{guild.name}] {detail_msg}")

        if stats["deleted_roles"] > 0 or stats["migrated_members"] > 0:
            logger.info(
                f"[{guild.name}] Self-healing duplicate role reconciliation complete: "
                f"Deleted {stats['deleted_roles']} role(s), Migrated {stats['migrated_members']} member(s), Failed {stats['failed']}"
            )

        return stats

    def diagnose_guild_permissions(self, guild: discord.Guild) -> list[str]:
        """
        Diagnoses permission, hierarchy, and duplicate role issues in a guild.
        Returns a list of warning descriptions (empty if guild setup is fully healthy).
        """
        warnings: list[str] = []
        if not guild or not hasattr(guild, "me") or not guild.me:
            return warnings

        me = guild.me
        bot_perms = getattr(me, "guild_permissions", None)
        bot_top_role = getattr(me, "top_role", None)

        if not bot_perms or not getattr(bot_perms, "manage_roles", False):
            warnings.append("❌ Missing `Manage Roles` permission — cannot create or assign faculty/guest roles.")

        guild_roles = getattr(guild, "roles", [])
        if not isinstance(guild_roles, (list, tuple)):
            guild_roles = []

        # Check for duplicate faculty roles
        seen_faculties: dict[str, list[discord.Role]] = {}
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            for fac in FACULTY_ROLE_NAMES:
                if self._match_faculty_role_in_list([r], fac) is not None:
                    seen_faculties.setdefault(fac, []).append(r)
                    break

        for fac, matched_roles in seen_faculties.items():
            if len(matched_roles) > 1:
                role_descs = ", ".join(f"`{r.name}` (pos: {getattr(r, 'position', 0)})" for r in matched_roles)
                warnings.append(
                    f"⚠️ Duplicate faculty roles detected for **{fac}**: {role_descs}. Please delete redundant roles in Server Settings → Roles."
                )

        # Check for duplicate guest roles
        matched_guest_roles: list[discord.Role] = []
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name:
                continue
            if GUEST_ROLE_PATTERN.search(r_name):
                matched_guest_roles.append(r)

        if len(matched_guest_roles) > 1:
            role_descs = ", ".join(f"`{r.name}` (pos: {getattr(r, 'position', 0)})" for r in matched_guest_roles)
            warnings.append(
                f"⚠️ Duplicate guest roles detected: {role_descs}. Please delete redundant roles in Server Settings → Roles."
            )

        # Check hierarchy against existing faculty and guest roles
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name:
                continue
            is_managed = (
                any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
                or bool(GUEST_ROLE_PATTERN.search(r_name))
            )
            if is_managed:
                r_pos = getattr(r, "position", 0)
                if isinstance(bot_pos, int) and isinstance(r_pos, int) and r_pos >= bot_pos:
                    bot_name = getattr(bot_top_role, "name", "TARVeri")
                    warnings.append(
                        f"⚠️ Role hierarchy conflict: Role **{r_name}** (pos {r_pos}) is higher than or equal to bot top role **{bot_name}** (pos {bot_pos}). Please drag the bot's role above **{r_name}** in Server Settings → Roles."
                    )

        return warnings
