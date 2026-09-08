"""
Guest verification, referral code management, and private thread ticket orchestration.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import string
from datetime import datetime, timedelta
from typing import Any

import discord

from tarveri.config import get_configured_tz, now_formatted
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter

logger = logging.getLogger("tarveri")


def generate_code_string(length: int = 6) -> str:
    """Generates a clean, readable random alphanumeric code formatted like TAR-8X2K9P."""
    alphabet = string.ascii_uppercase + string.digits
    # Exclude ambiguous characters
    clean_alphabet = "".join(c for c in alphabet if c not in "0O1I")
    rand_part = "".join(secrets.choice(clean_alphabet) for _ in range(length))
    return f"TAR-{rand_part}"


class GuestService:
    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        admin_role_name: str = "TARVeri Admin",
        rate_limiter: RateLimiter | None = None,
    ):
        self.bot = bot
        self.db = db
        self.admin_role_name = admin_role_name
        self.rate_limiter = rate_limiter
        self._lock = asyncio.Lock()

    async def create_referral_code(
        self,
        guild_id: int,
        referrer_user: discord.User | discord.Member,
        ttl_hours: int = 48,
        max_active: int = 3,
    ) -> tuple[bool, str]:
        """
        Generates a new referral code for a verified student if under active limit.
        Returns (success, code_or_error_message).
        """
        async with self._lock:
            active_count = await self.db.count_active_referrals_for_user(guild_id, referrer_user.id)
            if active_count >= max_active:
                return (
                    False,
                    f"⚠️ You already have **{active_count}** active referral code(s) (maximum allowed is {max_active}). "
                    "Please wait for your previous codes to be used or expire before creating more.",
                )

            expires_at = (datetime.now(get_configured_tz()) + timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
            for _ in range(5):
                candidate_code = generate_code_string()
                existing = await self.db.get_referral_code(candidate_code, guild_id)
                if not existing:
                    await self.db.create_referral_code(candidate_code, guild_id, referrer_user.id, expires_at)
                    guild = getattr(referrer_user, "guild", None)
                    await self.db.log(
                        "INFO",
                        "REFERRAL_CREATED",
                        f"Student {referrer_user} (ID: {referrer_user.id}) created referral code '{candidate_code}' (Expires: {expires_at})",
                        guild=guild,
                        user_id=referrer_user.id,
                    )
                    return True, candidate_code

            return False, "❌ Failed to generate unique referral code. Please try again."

    async def validate_referral_code(
        self, guild_id: int, code: str
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Validates whether a referral code is usable in this guild.
        Returns (is_valid, error_reason_if_any, code_record).
        Uses a uniform generic error message to prevent oracle/enumeration attacks.
        """
        generic_error = "❌ Invalid, expired, or already used referral code. Please check with your friend and try again."
        normalized = code.strip().upper().replace(" ", "")
        if not normalized.startswith("TAR-") and len(normalized) == 6:
            normalized = f"TAR-{normalized}"

        record = await self.db.get_referral_code(normalized, guild_id)
        if not record:
            return False, generic_error, None

        if record["status"] != "ACTIVE":
            return False, generic_error, record

        now_str = now_formatted()
        if record["expires_at"] <= now_str:
            await self.db.update_referral_code_status(normalized, guild_id, "EXPIRED")
            return False, generic_error, record

        return True, "", record

    async def find_parent_review_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Finds the best parent text channel in which to spawn private guest review threads."""
        settings = await self.db.get_guild_settings(guild.id)
        # 1. Configured review channel
        if settings and settings[3]:
            ch = guild.get_channel(settings[3])
            if isinstance(ch, discord.TextChannel):
                return ch

        # 2. Configured help channel
        if settings and settings[1]:
            ch = guild.get_channel(settings[1])
            if isinstance(ch, discord.TextChannel):
                return ch

        # 3. Autodetect channel by keywords: approval, review, tickets, verify, help
        keywords = ("approval", "review", "ticket", "mod", "admin", "staff", "verify", "help")
        for kw in keywords:
            for ch in guild.text_channels:
                if kw in ch.name.lower():
                    perms = ch.permissions_for(guild.me)
                    if perms.view_channel and (perms.create_private_threads or perms.manage_threads):
                        return ch

        # 4. First channel where bot can create private threads
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.view_channel and (perms.create_private_threads or perms.manage_threads):
                return ch

        return None

    async def get_or_create_guest_role(self, guild: discord.Guild) -> discord.Role | None:
        """
        Retrieves the guest role for the guild.
        First checks server configuration in DB, then searches for existing roles matching
        'Guest(Approved)', 'Guest (Approved)', 'Guest', etc.
        Only creates a new 'Guest(Approved)' role if no matching guest role exists.
        """
        settings = await self.db.get_guild_settings(guild.id)
        configured_name = settings[2].strip() if settings and settings[2] else None

        # 1. If explicitly configured, search by configured name first
        if configured_name:
            role = discord.utils.get(guild.roles, name=configured_name)
            if role:
                return role
            for r in guild.roles:
                if r.name.lower() == configured_name.lower():
                    return r

        # 2. Search for existing roles in priority order (Guest(Approved), Guest (Approved), Guest, etc.)
        known_aliases = [
            "Guest(Approved)",
            "Guest (Approved)",
            "Guest",
            "Approved Guest",
            "Guest(approved)",
            "Guest (approved)",
        ]
        for alias in known_aliases:
            role = discord.utils.get(guild.roles, name=alias)
            if role:
                return role

        # Fuzzy check across existing server roles
        for r in guild.roles:
            normalized_name = r.name.lower().replace(" ", "").replace("_", "")
            if normalized_name in ("guest(approved)", "guestapproved", "guest"):
                return r

        # 3. If no existing guest role was found, auto-create "Guest(Approved)"
        if not guild.me.guild_permissions.manage_roles:
            return None

        role_name_to_create = configured_name or "Guest(Approved)"
        try:
            permissions = discord.Permissions(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                add_reactions=True,
                use_external_emojis=True,
                connect=True,
                speak=True,
                use_voice_activation=True,
            )
            role = await guild.create_role(
                name=role_name_to_create,
                permissions=permissions,
                reason="TARVeri: Auto-created Guest(Approved) role for verified guests",
            )
            await self.db.log(
                "INFO",
                "ROLE_CREATED",
                f"Created guest role '{role_name_to_create}' in '{guild.name}' (Guild ID: {guild.id})",
                guild=guild,
            )
            return role
        except discord.HTTPException as e:
            logger.warning(f"Could not create guest role '{role_name_to_create}' in '{guild.name}': {e}")
            return None

    async def get_admin_role_or_fallback(self, guild: discord.Guild) -> discord.Role | None:
        """
        Intelligently discovers the server's administrator or staff role in priority order:
        1. Configured per-guild admin role from database (guild_settings.admin_role_name)
        2. Configured global admin role name (self.admin_role_name or 'TARVeri Admin')
        3. Common administrative role names: 'Admin', 'Administrator', 'Staff', 'Moderator', 'Mod'
        4. Server roles with Administrator or Manage Guild permissions.
        """
        if not guild or not hasattr(guild, "roles"):
            return None

        roles = list(guild.roles) if isinstance(guild.roles, (list, tuple, set)) else []
        if not roles:
            return None

        # 1. Guild-specific configured role from database
        try:
            settings = await self.db.get_guild_settings(guild.id)
            if settings and len(settings) > 4 and settings[4]:
                guild_admin_role_name = settings[4].strip()
                for r in roles:
                    if r.name == guild_admin_role_name or r.name.lower() == guild_admin_role_name.lower():
                        return r
        except Exception as e:
            logger.debug(f"Failed to fetch guild settings for admin role check: {e}")

        # 2. Configured global name
        if self.admin_role_name:
            for r in roles:
                if r.name == self.admin_role_name or r.name.lower() == self.admin_role_name.lower():
                    return r

        # 3. Known administrative aliases
        aliases = (
            "admin",
            "administrator",
            "administrators",
            "staff",
            "moderator",
            "moderators",
            "mod",
            "mods",
            "management",
            "server admin",
            "tarveri admin",
        )
        for alias in aliases:
            for r in roles:
                if r.name.lower() == alias:
                    return r

        # 4. Roles with Administrator or Manage Guild permissions (highest role first)
        for r in reversed(roles):
            if getattr(r, "is_default", lambda: False)():
                continue
            perms = getattr(r, "permissions", None)
            if perms and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)):
                return r

        return None


    async def open_guest_review_ticket(
        self,
        guild: discord.Guild,
        applicant: discord.Member,
        referral_code: str | None = None,
        reason: str | None = None,
        referrer_id: int | None = None,
    ) -> tuple[bool, str, discord.Thread | None]:
        """
        Creates a private review thread, invites the applicant & referring student,
        and posts the review embed with action buttons.
        """
        async with self._lock:
            # 1. Rate limiting on guest/referral attempts
            if self.rate_limiter:
                if self.rate_limiter.is_rate_limited(applicant.id):
                    await self.db.log(
                        "WARNING",
                        "RATE_LIMITED",
                        f"{applicant} exceeded guest application attempt limit",
                        user_id=applicant.id,
                        guild=guild,
                    )
                    return (
                        False,
                        "⏳ You've made too many attempts recently. Please wait a few minutes before trying again.",
                        None,
                    )
                self.rate_limiter.record_attempt(applicant.id)

            # 2. Check if applicant is already a verified student
            is_student = bool(await self.db.get_verification_by_user(applicant.id))
            if is_student:
                return (
                    False,
                    "ℹ️ You are already verified as a TARUMT student! You do not need guest access.",
                    None,
                )

            # 3. Check if applicant already has the Guest role
            guest_role = await self.get_or_create_guest_role(guild)
            if guest_role and guest_role in applicant.roles:
                return (
                    False,
                    f"ℹ️ You already have the **{guest_role.name}** role in this server.",
                    None,
                )

            # 4. Prevent duplicate open tickets
            existing_open = await self.db.get_open_guest_ticket_for_applicant(guild.id, applicant.id)
            if existing_open:
                return (
                    False,
                    "⏳ You already have an active guest review ticket in progress! Please check your private threads.",
                    None,
                )

            # 5. If referral code is used, validate and lock code status
            if referral_code:
                is_valid, err_msg, record = await self.validate_referral_code(guild.id, referral_code)
                if not is_valid or not record:
                    return False, err_msg, None

                # Edge case: self-referral prevention
                if record["referrer_discord_id"] == applicant.id:
                    return (
                        False,
                        "❌ You cannot use your own referral code.",
                        None,
                    )

                referrer_id = record["referrer_discord_id"]
                await self.db.update_referral_code_status(
                    referral_code, guild.id, "PENDING_APPROVAL", used_by_discord_id=applicant.id
                )

            # 6. Locate parent channel for thread creation
            parent_ch = await self.find_parent_review_channel(guild)
            if not parent_ch:
                # Revert referral code status if channel creation fails
                if referral_code:
                    await self.db.update_referral_code_status(referral_code, guild.id, "ACTIVE")
                return (
                    False,
                    "❌ Could not find a suitable channel to create the private review thread. Please contact an admin.",
                    None,
                )

            # 7. Create Private Thread with sequential tracking number (e.g. guest-0001-username)
            seq = await self.db.get_next_guild_ticket_seq(guild.id)
            clean_name = "".join(c for c in applicant.display_name if c.isalnum() or c in "-_")[:20].lower() or "guest"
            thread_name = f"guest-{seq:04d}-{clean_name}"
            try:
                thread = await parent_ch.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.private_thread,
                    auto_archive_duration=1440,
                    reason=f"TARVeri Guest Verification Review #{seq:04d} for {applicant}",
                )
            except (discord.HTTPException, discord.Forbidden) as e:
                if referral_code:
                    await self.db.update_referral_code_status(referral_code, guild.id, "ACTIVE")
                logger.error(f"Failed to create private thread '{thread_name}' in #{parent_ch.name} ({guild.name}): {e}")
                return False, f"❌ Failed to create private thread: {e}", None

            # 8. Grant thread chat & participation permissions to applicant on parent channel
            if hasattr(parent_ch, "set_permissions"):
                try:
                    app_overwrite = parent_ch.overwrites_for(applicant)
                    app_overwrite.send_messages_in_threads = True
                    app_overwrite.read_message_history = True
                    app_overwrite.attach_files = True
                    app_overwrite.embed_links = True
                    app_overwrite.add_reactions = True
                    res = parent_ch.set_permissions(
                        applicant,
                        overwrite=app_overwrite,
                        reason=f"TARVeri: Allow guest applicant {applicant} to chat in review thread",
                    )
                    if inspect.isawaitable(res):
                        await res
                except (discord.HTTPException, discord.Forbidden):
                    pass

            # 9. Invite applicant, referrer, and all admin team members
            try:
                await thread.add_user(applicant)
            except (discord.HTTPException, discord.Forbidden):
                pass

            referrer_member: discord.Member | None = None
            if referrer_id:
                referrer_member = guild.get_member(referrer_id)
                if referrer_member:
                    if hasattr(parent_ch, "set_permissions"):
                        try:
                            ref_overwrite = parent_ch.overwrites_for(referrer_member)
                            ref_overwrite.send_messages_in_threads = True
                            ref_overwrite.read_message_history = True
                            ref_overwrite.attach_files = True
                            ref_overwrite.embed_links = True
                            ref_overwrite.add_reactions = True
                            res = parent_ch.set_permissions(
                                referrer_member,
                                overwrite=ref_overwrite,
                                reason=f"TARVeri: Allow referring student {referrer_member} to chat in review thread",
                            )
                            if inspect.isawaitable(res):
                                await res
                        except (discord.HTTPException, discord.Forbidden):
                            pass

                    try:
                        await thread.add_user(referrer_member)
                    except (discord.HTTPException, discord.Forbidden):
                        pass

            # Auto-invite all admin team members & reviewers to private review thread
            if hasattr(guild, "chunk") and not getattr(guild, "chunked", True):
                try:
                    await guild.chunk()
                except Exception as e:
                    logger.debug(f"Guild chunking skipped/failed: {e}")

            admin_role = await self.get_admin_role_or_fallback(guild)

            admin_members: set[discord.Member] = set()

            # A. Members having the discovered admin/reviewer role
            if admin_role:
                if hasattr(admin_role, "members") and admin_role.members:
                    admin_members.update(admin_role.members)
                elif hasattr(guild, "members") and guild.members:
                    for m in guild.members:
                        if admin_role in getattr(m, "roles", []):
                            admin_members.add(m)

            # B. Members with Administrator, Manage Guild, or Manage Threads permissions
            if hasattr(guild, "members") and guild.members:
                for m in guild.members:
                    if getattr(m, "bot", False):
                        continue
                    perms = getattr(m, "guild_permissions", None)
                    if perms and (
                        getattr(perms, "administrator", False)
                        or getattr(perms, "manage_guild", False)
                        or getattr(perms, "manage_threads", False)
                    ):
                        admin_members.add(m)

            # C. Guild Owner
            if getattr(guild, "owner", None):
                admin_members.add(guild.owner)
            elif getattr(guild, "owner_id", None):
                owner_m = guild.get_member(guild.owner_id)
                if owner_m:
                    admin_members.add(owner_m)

            # Exclude applicant, referrer, and bot itself
            exclude_ids = {applicant.id, getattr(getattr(guild, "me", None), "id", None)}
            if referrer_id:
                exclude_ids.add(referrer_id)

            # Invite all discovered admin members (capped at 25 to respect Discord rate limits)
            invited_count = 0
            for adm_m in admin_members:
                if adm_m and adm_m.id not in exclude_ids:
                    try:
                        await thread.add_user(adm_m)
                        invited_count += 1
                        if invited_count >= 25:
                            break
                    except (discord.HTTPException, discord.Forbidden):
                        pass


            # 9. Save ticket to database
            ticket_id = await self.db.create_guest_ticket(
                guild_id=guild.id,
                applicant_id=applicant.id,
                channel_id=thread.id,
                referrer_id=referrer_id,
                referral_code=referral_code,
                reason=reason,
                ticket_seq=seq,
            )

            await self.db.log(
                "INFO",
                "GUEST_TICKET_OPENED",
                f"Opened guest review ticket #{seq:04d} (DB ID: {ticket_id}) for applicant {applicant} (ID: {applicant.id})"
                + (f" with referral code '{referral_code}' (Vouched by ID: {referrer_id})" if referral_code else ""),
                guild=guild,
                user_id=applicant.id,
            )

            return True, f"✅ Guest ticket #{seq:04d} created in private thread {thread.mention}!", thread

    async def approve_guest_application(
        self,
        ticket: dict[str, Any],
        guild: discord.Guild,
        admin_user: discord.User | discord.Member,
        reason: str | None = None,
    ) -> tuple[bool, str]:
        """Approves guest application, assigns guest role, updates DB, and archives thread."""
        ticket_id = ticket["ticket_id"]
        applicant_id = ticket["applicant_id"]
        referral_code = ticket.get("referral_code")
        approval_reason = reason.strip() if reason else "Approved by admin"

        # 1. Assign Guest Role
        guest_role = await self.get_or_create_guest_role(guild)
        if not guest_role:
            return False, "❌ Guest role does not exist and bot lacks permission to create it."

        applicant_member = guild.get_member(applicant_id)
        if not applicant_member:
            try:
                applicant_member = await guild.fetch_member(applicant_id)
            except (discord.NotFound, discord.HTTPException):
                applicant_member = None

        if applicant_member:
            try:
                await applicant_member.add_roles(
                    guest_role, reason=f"TARVeri: Guest approved by {admin_user}"
                )
            except discord.HTTPException as e:
                return False, f"❌ Failed to assign guest role to applicant: {e}"

            try:
                await applicant_member.send(
                    f"🎉 **Congratulations!** Your guest application to **{guild.name}** has been approved by staff.\n"
                    f"You have been granted the **{guest_role.name}** role. Welcome to the server!"
                )
            except discord.Forbidden:
                pass

        # 2. Update DB ticket and referral code
        await self.db.close_guest_ticket(
            ticket_id, "APPROVED", closed_by_admin_id=admin_user.id, close_reason=approval_reason
        )
        if referral_code:
            await self.db.update_referral_code_status(
                referral_code, guild.id, "USED", used_by_discord_id=applicant_id
            )

        await self.db.log(
            "INFO",
            "GUEST_APPROVED",
            f"Admin {admin_user} (ID: {admin_user.id}) approved guest ticket #{ticket_id} for user ID {applicant_id} in '{guild.name}'. Reason: {approval_reason}",
            guild=guild,
            user_id=applicant_id,
        )

        await self._cleanup_channel_overwrites(guild, ticket)

        return True, f"✅ Guest application approved by {admin_user.mention}! Assigned **{guest_role.name}** role."

    async def reject_guest_application(
        self,
        ticket: dict[str, Any],
        guild: discord.Guild,
        admin_user: discord.User | discord.Member,
        reason: str | None = None,
    ) -> tuple[bool, str]:
        """Rejects guest application, sends notification DM, kicks user, and updates DB."""
        ticket_id = ticket["ticket_id"]
        applicant_id = ticket["applicant_id"]
        referral_code = ticket.get("referral_code")
        reject_reason = reason.strip() if reason else "Guest application not approved by server administration."

        applicant_member = guild.get_member(applicant_id)
        if not applicant_member:
            try:
                applicant_member = await guild.fetch_member(applicant_id)
            except (discord.NotFound, discord.HTTPException):
                applicant_member = None

        # 1. Send DM before kicking
        if applicant_member:
            try:
                await applicant_member.send(
                    f"⚠️ Your guest access application to **{guild.name}** was not approved.\n"
                    f"**Reason:** {reject_reason}"
                )
            except discord.Forbidden:
                pass

            # 2. Kick member
            if guild.me.guild_permissions.kick_members and applicant_member.top_role < guild.me.top_role:
                try:
                    await applicant_member.kick(
                        reason=f"TARVeri: Guest application rejected by {admin_user}: {reject_reason}"
                    )
                except discord.HTTPException as e:
                    logger.warning(f"Could not kick rejected guest {applicant_member}: {e}")

        # 3. Update DB ticket and referral code
        await self.db.close_guest_ticket(
            ticket_id, "REJECTED", closed_by_admin_id=admin_user.id, close_reason=reject_reason
        )
        if referral_code:
            await self.db.update_referral_code_status(
                referral_code, guild.id, "REJECTED", used_by_discord_id=applicant_id
            )

        await self.db.log(
            "INFO",
            "GUEST_REJECTED",
            f"Admin {admin_user} (ID: {admin_user.id}) rejected guest ticket #{ticket_id} for user ID {applicant_id} in '{guild.name}'. Reason: {reject_reason}",
            guild=guild,
            user_id=applicant_id,
        )

        await self._cleanup_channel_overwrites(guild, ticket)

        return True, f"🛑 Guest application rejected and applicant removed from the server by {admin_user.mention}."

    async def _cleanup_channel_overwrites(self, guild: discord.Guild, ticket: dict[str, Any]) -> None:
        """Cleans up temporary channel permission overwrites granted to applicant/referrer."""
        channel_id = ticket.get("channel_id")
        if not channel_id or not hasattr(guild, "get_thread"):
            return

        parent_ch = None
        thread = guild.get_thread(channel_id)
        if thread and hasattr(thread, "parent"):
            parent_ch = thread.parent
        elif hasattr(guild, "get_channel"):
            ch = guild.get_channel(channel_id)
            if ch and hasattr(ch, "parent"):
                parent_ch = ch.parent

        if not parent_ch or not hasattr(parent_ch, "set_permissions"):
            return

        applicant_id = ticket.get("applicant_id")
        if applicant_id:
            applicant_m = guild.get_member(applicant_id)
            if applicant_m:
                try:
                    res = parent_ch.set_permissions(applicant_m, overwrite=None, reason="TARVeri: Review ticket closed")
                    if inspect.isawaitable(res):
                        await res
                except (discord.HTTPException, discord.Forbidden):
                    pass

        referrer_id = ticket.get("referrer_id")
        if referrer_id:
            referrer_m = guild.get_member(referrer_id)
            if referrer_m:
                try:
                    res = parent_ch.set_permissions(referrer_m, overwrite=None, reason="TARVeri: Review ticket closed")
                    if inspect.isawaitable(res):
                        await res
                except (discord.HTTPException, discord.Forbidden):
                    pass

    async def handle_member_leave_or_ban(
        self,
        guild: discord.Guild,
        user: discord.User | discord.Member,
        is_ban: bool = False,
    ) -> None:
        """
        Revokes guest access, expires open review tickets, and invalidates active referrals
        when a user leaves, is kicked, or is banned from the server.
        """
        revocation_status = "BANNED" if is_ban else "LEFT_SERVER"
        close_reason = "Member banned from server" if is_ban else "Member left the server"
        referrer_close_reason = "Referring student banned from server" if is_ban else "Referring student left server"

        revoked_tickets = await self.db.revoke_guest_tickets_for_user(
            guild.id, user.id, status=revocation_status, close_reason=close_reason
        )
        revoked_referred_tickets = await self.db.cancel_open_tickets_referred_by_user(
            guild.id, user.id, close_reason=referrer_close_reason
        )
        revoked_referrals = await self.db.revoke_active_referrals_for_user(
            guild.id, user.id, status=revocation_status
        )

        action_type = "GUEST_REVOKED_ON_BAN" if is_ban else "GUEST_REVOKED_ON_LEAVE"
        action_verb = "banned from" if is_ban else "left / was removed from"

        total_affected = revoked_tickets + revoked_referred_tickets + revoked_referrals
        if total_affected > 0:
            await self.db.log(
                "INFO",
                action_type,
                f"Revoked guest status ({revoked_tickets} ticket(s), {revoked_referred_tickets} referred ticket(s), {revoked_referrals} referral(s)) for {user} (ID: {user.id}) who {action_verb} '{guild.name}'",
                guild=guild,
                user_id=user.id,
            )
