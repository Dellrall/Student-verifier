"""
Verification business logic, concurrency control, and cross-guild role synchronization.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

import aiosqlite
import discord

from tarveri.config import (
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    hash_student_id,
    mask_student_id,
    validate_student_id,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter

logger = logging.getLogger("tarveri")


@dataclass(slots=True)
class RoleSyncResult:
    verified_in: list[tuple[str, str]] = field(default_factory=list)
    already_had_role_in: list[tuple[str, str]] = field(default_factory=list)
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

    async def _assign_role_in_guild(
        self, guild: discord.Guild, user_id: int, role_name: str, result: RoleSyncResult
    ) -> None:
        """Process role assignment in a single guild."""
        member = await self.get_or_fetch_member(guild, user_id)
        if member is None:
            return

        existing_roles = [r for r in member.roles if r.name in FACULTY_ROLE_NAMES]
        if existing_roles:
            result.already_had_role_in.append((guild.name, existing_roles[0].name))
            return

        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            if not guild.me.guild_permissions.manage_roles:
                result.missing_role_in.append(guild.name)
                return
            try:
                role = await guild.create_role(
                    name=role_name,
                    reason="TARVeri: auto-created missing faculty role for verification",
                )
                await self.db.log("INFO", "ROLE_CREATED", f"Created role '{role_name}'", guild=guild)
            except discord.HTTPException as e:
                await self.db.log(
                    "ERROR",
                    "ROLE_CREATE_FAILED",
                    f"Failed to create role '{role_name}' in '{guild.name}': {e}",
                    guild=guild,
                )
                result.missing_role_in.append(guild.name)
                return

        if role >= guild.me.top_role or not guild.me.guild_permissions.manage_roles:
            result.failed_in.append(guild.name)
            return

        try:
            await member.add_roles(role, reason="TARVeri: Student verification role assignment")
            result.verified_in.append((guild.name, role_name))
        except discord.HTTPException as e:
            result.failed_in.append(guild.name)
            await self.db.log(
                "ERROR",
                "ROLE_ASSIGN_FAILED",
                f"Failed to assign '{role_name}' to user {user_id} in '{guild.name}': {e}",
                guild=guild,
                user_id=user_id,
            )

    async def assign_role_across_guilds(
        self, user_id: int, role_name: str, guilds: Sequence[discord.Guild]
    ) -> RoleSyncResult:
        """
        Ensures the given user holds `role_name` in all specified guilds concurrently.
        """
        result = RoleSyncResult()
        if not guilds:
            return result

        tasks = [self._assign_role_in_guild(g, user_id, role_name, result) for g in guilds]
        await asyncio.gather(*tasks, return_exceptions=True)
        return result

    def format_role_summary(self, result: RoleSyncResult) -> str:
        """Formats a human-readable summary of role assignments."""
        lines: list[str] = []
        if result.verified_in:
            lines.append("✅ You've been given the following role(s):")
            lines.extend(f"   • **{g}** → {r}" for g, r in result.verified_in)
        if result.already_had_role_in:
            lines.append("ℹ️ You already had a faculty role in:")
            lines.extend(f"   • **{g}** → {r} (unchanged)" for g, r in result.already_had_role_in)
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
        3. Student ID format and faculty code extraction
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
            is_valid, student_id, faculty_code, role_name = validate_student_id(raw_student_id)
            if not is_valid or not faculty_code or not role_name:
                if not student_id:
                    return "❌ Please provide a valid student ID (e.g., `23WMD09867`)."
                if faculty_code and faculty_code not in FACULTY_ROLES:
                    return "❌ Student ID does not match any known faculty. Please check and try again."
                return "❌ Invalid student ID format. Please use the format like `23WMD09867`."

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
                sync_result = await self.assign_role_across_guilds(user.id, assigned_faculty_role, mutual_guilds)
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

            sync_result = await self.assign_role_across_guilds(user.id, role_name, mutual_guilds)

            # Only persist if role was successfully granted in at least one server
            if sync_result.verified_in:
                try:
                    await self.db.record_verification(user.id, id_hash, faculty_code)
                    await self.db.log(
                        "INFO",
                        "VERIFIED",
                        f"{user} (ID: {user.id}) verified (student ID masked: {mask_student_id(student_id)}) "
                        f"→ role '{role_name}' in {[g for g, _ in sync_result.verified_in]}",
                        user_id=user.id,
                        guild=guild_ctx,
                    )
                except (sqlite3.IntegrityError, aiosqlite.IntegrityError) as e:
                    # Rollback assigned roles if database collision occurs
                    for g_name, r_name in sync_result.verified_in:
                        guild = discord.utils.get(self.bot.guilds, name=g_name)
                        if guild:
                            member = await self.get_or_fetch_member(guild, user.id)
                            if member:
                                r = discord.utils.get(guild.roles, name=r_name)
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
