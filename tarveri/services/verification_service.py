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
    FACULTY_COLORS,
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

    @staticmethod
    def _match_faculty_role_in_list(roles: Sequence[discord.Role], target_name: str) -> discord.Role | None:
        """
        Multi-tier matcher to find an existing faculty role in a list/sequence of roles:
        Tier 1: Exact match (name == target_name)
        Tier 2: Case-insensitive & whitespace-trimmed match (name.strip().upper() == target_name.upper())
        Tier 3: Normalized alphanumeric match (e.g. '[FOCS]' or '🎓 FOCS')
        Tier 4: Prefix / word-boundary match (e.g. 'FOCS - Faculty of Computing' or 'Faculty of Computing (FOCS)')
        Prioritizes the role with the highest position if multiple matches exist.
        """
        if not roles:
            return None

        target_upper = target_name.strip().upper()
        target_alnum = re.sub(r"[^A-Za-z0-9]", "", target_upper)

        # Tier 1: Exact match
        exact_matches = [r for r in roles if getattr(r, "name", None) == target_name]
        if exact_matches:
            return max(exact_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 2: Case-insensitive & trimmed match
        ci_matches = [
            r for r in roles if getattr(r, "name", "").strip().upper() == target_upper
        ]
        if ci_matches:
            return max(ci_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 3: Normalized alphanumeric match
        alnum_matches = [
            r
            for r in roles
            if re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "")).upper() == target_alnum
        ]
        if alnum_matches:
            return max(alnum_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 4: Prefix or word boundary or bracket match
        fuzzy_matches = []
        for r in roles:
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

        return None

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
        """
        # 1. Exhaustive search across cache and live API
        existing_role = await self.find_faculty_role(guild, role_name)
        if existing_role is not None:
            return existing_role

        # 2. Check if bot has Manage Roles permission before attempting creation
        can_manage = (
            getattr(guild.me.guild_permissions, "manage_roles", False)
            if hasattr(guild, "me") and hasattr(guild.me, "guild_permissions")
            else False
        )
        if not can_manage:
            return None

        # 3. Create the role only when absolutely not found anywhere
        try:
            color_val = FACULTY_COLORS.get(role_name, 0x3498DB)
            role = await guild.create_role(
                name=role_name,
                colour=discord.Colour(color_val),
                mentionable=True,
                reason="TARVeri: auto-created missing faculty role for verification",
            )
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

    async def _assign_role_in_guild(
        self, guild: discord.Guild, user_id: int, role_name: str, result: RoleSyncResult
    ) -> None:
        """Process role assignment in a single guild."""
        member = await self.get_or_fetch_member(guild, user_id)
        if member is None:
            return

        # Check if member already has any faculty role
        member_roles = getattr(member, "roles", [])
        if isinstance(member_roles, (list, tuple)):
            for r in member_roles:
                for fac in FACULTY_ROLE_NAMES:
                    if self._match_faculty_role_in_list([r], fac) is not None:
                        result.already_had_role_in.append((guild.id, guild.name, getattr(r, "name", fac)))
                        return

        # Exhaustive search or create
        role = await self.get_or_create_faculty_role(guild, role_name)
        if not role:
            result.missing_role_in.append(guild.name)
            return

        # Check hierarchy and permissions
        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top_role = getattr(me, "top_role", None) if me else None
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0
        role_pos = getattr(role, "position", 0)

        if not can_manage or (isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos >= bot_pos):
            result.failed_in.append(guild.name)
            return

        try:
            await member.add_roles(role, reason="TARVeri: Student verification role assignment")
            result.verified_in.append((guild.id, guild.name, getattr(role, "name", role_name)))
        except discord.HTTPException as e:
            result.failed_in.append(guild.name)
            await self.db.log(
                "ERROR",
                "ROLE_ASSIGN_FAILED",
                f"Failed to assign '{role.name}' to user {user_id} in '{guild.name}': {e}",
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
                        f"→ role '{role_name}' in {[entry[1] if len(entry) == 3 else entry[0] for entry in sync_result.verified_in]}",
                        user_id=user.id,
                        guild=guild_ctx,
                    )
                except (sqlite3.IntegrityError, aiosqlite.IntegrityError) as e:
                    # Rollback assigned roles if database collision occurs
                    for entry in sync_result.verified_in:
                        if len(entry) == 3:
                            g_id, _, r_name = entry
                            guild = self.bot.get_guild(g_id)
                        else:
                            g_name, r_name = entry
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

    async def reconcile_verified_members(self, guild: discord.Guild) -> dict[str, int]:
        """
        Self-healing: cross-references current guild members against the verifications table.
        If a student verified in the database is missing their faculty role in this guild
        (e.g., rejoined during maintenance, role was deleted/recreated), automatically restores it.
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

        for discord_user_id, _, faculty_code, _ in all_verifications:
            member = await self.get_or_fetch_member(guild, discord_user_id)
            if not member:
                continue

            summary["checked"] += 1
            target_role_name = FACULTY_ROLES.get(faculty_code)
            if not target_role_name:
                continue

            # Check if member already has any faculty role
            member_roles = getattr(member, "roles", [])
            has_faculty_role = False
            if isinstance(member_roles, (list, tuple)):
                for r in member_roles:
                    for fac in FACULTY_ROLE_NAMES:
                        if self._match_faculty_role_in_list([r], fac) is not None:
                            has_faculty_role = True
                            break
                    if has_faculty_role:
                        break

            if has_faculty_role:
                continue

            # Member is verified in DB but missing faculty role in this guild -> find or create
            target_role = await self.get_or_create_faculty_role(guild, target_role_name)
            if not target_role:
                summary["failed"] += 1
                continue

            # Check role hierarchy and permissions
            me = getattr(guild, "me", None)
            can_manage = (
                getattr(me.guild_permissions, "manage_roles", False)
                if me and hasattr(me, "guild_permissions")
                else False
            )
            bot_top_role = getattr(me, "top_role", None) if me else None

            bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0
            role_pos = getattr(target_role, "position", 0)
            if not can_manage or (isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos >= bot_pos):
                summary["failed"] += 1
                continue

            try:
                await member.add_roles(
                    target_role,
                    reason="TARVeri: Self-healing automatic role restoration for verified student",
                )
                summary["restored"] += 1
                await self.db.log(
                    "INFO",
                    "ROLE_RESTORED",
                    f"Self-healing: Restored missing faculty role '{target_role.name}' to verified student {member} (ID: {discord_user_id})",
                    guild=guild,
                    user_id=discord_user_id,
                )
            except discord.HTTPException as e:
                summary["failed"] += 1
                logger.warning(
                    f"Failed to restore role '{target_role.name}' for {member} in '{guild.name}': {e}"
                )

        if summary["restored"] > 0:
            logger.info(
                f"[{guild.name}] Self-healing verified member reconciliation: "
                f"Checked {summary['checked']}, Restored {summary['restored']}, Failed {summary['failed']}"
            )

        return summary

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

        # Check hierarchy against existing faculty and guest roles
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if r_name in FACULTY_ROLE_NAMES or r_name in ("Guest(Approved)", "Guest (Approved)", "Guest"):
                r_pos = getattr(r, "position", 0)
                if isinstance(bot_pos, int) and isinstance(r_pos, int) and r_pos >= bot_pos:
                    bot_name = getattr(bot_top_role, "name", "TARVeri")
                    warnings.append(
                        f"⚠️ Role hierarchy conflict: Role **{r_name}** (pos {r_pos}) is higher than or equal to bot top role **{bot_name}** (pos {bot_pos}). Please drag the bot's role above **{r_name}** in Server Settings → Roles."
                    )

        return warnings
