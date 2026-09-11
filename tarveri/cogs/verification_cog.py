"""
Discord UI and Commands for Student Verification.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.config import (
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    GUEST_ROLE_PATTERN,
    ROLE_HELP_KEYWORDS_PATTERN,
    Settings,
    format_card_expiry_display,
    parse_card_expiry_date,
)
from tarveri.cogs.guest_cog import VerificationGatewayView
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService
from tarveri.utils import schedule_ttl_delete

logger = logging.getLogger("tarveri")


class VerificationModal(discord.ui.Modal, title="TARUMT Verification"):
    student_id = discord.ui.TextInput(
        label="Student ID",
        placeholder="e.g. 23WMD09867",
        min_length=7,
        max_length=20,
        required=True,
    )
    card_expiry = discord.ui.TextInput(
        label="Student Card Expiry Date (MM/YY)",
        placeholder="e.g. 10/26 (Optional)",
        min_length=4,
        max_length=10,
        required=False,
    )

    def __init__(self, service: VerificationService):
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_expiry = self.card_expiry.value.strip() if self.card_expiry.value else None
        response_text = await self.service.perform_verification(
            interaction.user,
            self.student_id.value,
            raw_expiry_date=raw_expiry,
        )
        await interaction.followup.send(response_text, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)


class FurtherStudyTransitionModal(discord.ui.Modal, title="TARUMT Level Progression"):
    student_id = discord.ui.TextInput(
        label="New Student ID",
        placeholder="e.g. 24WMR12345",
        min_length=7,
        max_length=20,
        required=True,
    )
    card_expiry = discord.ui.TextInput(
        label="New Student Card Expiry (MM/YY)",
        placeholder="e.g. 10/28 (Optional)",
        min_length=4,
        max_length=10,
        required=False,
    )

    def __init__(self, service: VerificationService):
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_expiry = self.card_expiry.value.strip() if self.card_expiry.value else None
        response_text = await self.service.perform_verification(
            interaction.user,
            self.student_id.value,
            raw_expiry_date=raw_expiry,
        )
        await interaction.followup.send(response_text, ephemeral=True)
        schedule_ttl_delete(interaction, delay=90.0)


class ExtendExpiryModal(discord.ui.Modal, title="Extend Student Card Validity"):
    expiry_date = discord.ui.TextInput(
        label="New Student Card Expiry Date (MM/YY)",
        placeholder="e.g. 05/27",
        min_length=4,
        max_length=10,
        required=True,
    )
    note = discord.ui.TextInput(
        label="Extension Reason (Optional)",
        placeholder="e.g. Final year project extension / Delayed semester",
        max_length=100,
        required=False,
    )

    def __init__(self, db: Database, service: VerificationService):
        super().__init__()
        self.db = db
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_val = self.expiry_date.value.strip()
        iso_date = parse_card_expiry_date(raw_val)
        if not iso_date:
            await interaction.followup.send(
                "❌ Invalid date format. Please use `MM/YY` (e.g. `05/27`) or `YYYY-MM-DD`.",
                ephemeral=True,
            )
            return

        await self.db.update_verification_profile(
            discord_user_id=interaction.user.id,
            card_expiry_date=iso_date,
            lifecycle_prompt_status="extended",
        )
        guild = interaction.guild
        note_str = f" ({self.note.value.strip()})" if self.note.value and self.note.value.strip() else ""
        await self.db.log(
            "INFO",
            "EXPIRY_EXTENDED",
            f"{interaction.user} (ID: {interaction.user.id}) extended card expiry to {iso_date}{note_str}",
            user_id=interaction.user.id,
            guild=guild,
        )
        display_str = format_card_expiry_display(iso_date)
        await interaction.followup.send(
            f"✅ **Student Card Validity Extended!**\n"
            f"Your new card expiry is set to **{display_str}**.\n"
            f"Your Digital Campus Card (`/card`) has been updated.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)


class StudentLifecycleResolutionView(discord.ui.View):
    """Interactive persistent view presented to students whose card expiry is reached."""

    def __init__(
        self,
        service: VerificationService,
        db: Database,
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        self.service = service
        self.db = db

    @discord.ui.button(
        label="I have Graduated",
        style=discord.ButtonStyle.success,
        emoji="🎓",
        custom_id="tarveri_lifecycle_graduated",
        row=0,
    )
    async def on_graduated(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AlumniClaimModal(self.service))

    @discord.ui.button(
        label="Further Studies at TARUMT",
        style=discord.ButtonStyle.primary,
        emoji="📚",
        custom_id="tarveri_lifecycle_further_study",
        row=0,
    )
    async def on_further_study(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(FurtherStudyTransitionModal(self.service))

    @discord.ui.button(
        label="Still Studying / Extension",
        style=discord.ButtonStyle.secondary,
        emoji="⏳",
        custom_id="tarveri_lifecycle_extend",
        row=0,
    )
    async def on_extend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ExtendExpiryModal(self.db, self.service))


class AlumniClaimModal(discord.ui.Modal, title="TARUMT Alumni Transition"):
    grad_year = discord.ui.TextInput(
        label="Graduation Year",
        placeholder="e.g. 2025",
        min_length=4,
        max_length=4,
        required=True,
    )
    programme = discord.ui.TextInput(
        label="Completed Programme (Optional)",
        placeholder="e.g. Bachelor of Software Engineering (Honours)",
        min_length=2,
        max_length=80,
        required=False,
    )

    def __init__(self, service: VerificationService):
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False, thinking=True)
        raw_year = self.grad_year.value.strip()
        try:
            year_int = int(raw_year)
        except ValueError:
            await interaction.followup.send(
                "❌ Please enter a valid 4-digit graduation year (e.g. 2025).",
                ephemeral=True,
            )
            return

        result = await self.service.claim_alumni_status(
            user_id=interaction.user.id,
            user_display_name=interaction.user.display_name,
            graduated_year=year_int,
            programme=self.programme.value if self.programme.value else None,
            current_guild=interaction.guild,
        )

        if not result["success"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎓 Congratulations on Graduating, {interaction.user.display_name}!",
            description=(
                f"🎉 **{interaction.user.mention}** has successfully registered their **TARUMT Alumni** status!\n\n"
                f"• **Class Cohort**: Class of {result['graduated_year']}\n"
                f"• **Faculty**: {result['faculty_name']}\n"
                + (f"• **Programme**: {result['programme']}\n" if result['programme'] else "")
                + f"\n🏷️ **`TARUMT Alumni`** role assigned in {result['guilds_updated']} server(s).\n"
                f"🪪 **`❖ ALUMNI`** badge unlocked on your Digital Campus Card (`/card`)."
            ),
            color=discord.Color.from_rgb(212, 175, 55),
        )
        embed.set_footer(text="TARVeri Alumni Verification • Instant & Tamper-Proof")
        await interaction.followup.send(embed=embed, ephemeral=False)


class VerificationCog(commands.Cog, name="Verification"):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        service: VerificationService,
        rate_limiter: RateLimiter,
        settings: Settings | None = None,
        guest_service: GuestService | None = None,
    ):
        self.bot = bot
        self.db = db
        self.service = service
        self.rate_limiter = rate_limiter
        self.settings = settings or getattr(bot, "settings", None)
        self.guest_service = guest_service or getattr(bot, "guest_service", None)
        self._tip_cooldowns: dict[int, float] = {}
        self._lifecycle_cooldowns: dict[int, float] = {}
        self._guild_channels_cache: dict[int, tuple[int | None, int | None]] = {}

    @app_commands.command(
        name="verify",
        description="Verify your TARUMT student status and receive your faculty role.",
    )
    @app_commands.describe(
        student_id="Your TARUMT student ID (e.g. 23WMD09867). Leave blank to open input window.",
        expiry_date="Student Card Expiry Date (MM/YY, e.g. 10/26, optional)",
    )
    async def verify_slash(
        self,
        interaction: discord.Interaction,
        student_id: str | None = None,
        expiry_date: str | None = None,
    ) -> None:
        """Slash command for verification with optional direct input or modal prompt."""
        if student_id:
            await interaction.response.defer(ephemeral=True, thinking=True)
            response_text = await self.service.perform_verification(
                interaction.user, student_id, raw_expiry_date=expiry_date
            )
            await interaction.followup.send(response_text, ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        # Check if already verified — if so, resync silently without modal
        existing = await self.db.get_verification_by_user(interaction.user.id)
        if existing:
            await interaction.response.defer(ephemeral=True, thinking=True)
            _, stored_faculty, _ = existing
            mutual_guilds = await self.service.get_mutual_guilds_for_user(interaction.user.id)
            result = await self.service.assign_role_across_guilds(
                interaction.user.id, FACULTY_ROLES[stored_faculty], mutual_guilds
            )
            summary = self.service.format_role_summary(result)
            msg = summary or "ℹ️ You're already verified and up to date in every server I share with you."
            msg += "\n💡 *If you are progressing to a new study level (e.g. Diploma -> Degree), run `/verify student_id:<your_new_id>` to transition.*"
            await interaction.followup.send(
                msg,
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        # Open the interactive modal dialog
        await interaction.response.send_modal(VerificationModal(self.service))

    @app_commands.command(
        name="graduate",
        description="Claim your official TARUMT Alumni status and unlock the Alumni role & card badge.",
    )
    @app_commands.describe(
        year="Your graduation year (e.g. 2025). Leave blank to open input form.",
        programme="Your completed programme / degree (optional, e.g. Bachelor of Software Engineering)",
    )
    async def graduate_slash(
        self,
        interaction: discord.Interaction,
        year: int | None = None,
        programme: str | None = None,
    ) -> None:
        """Slash command to transition from student to verified alumni."""
        # 1. Preflight check: user must be verified in database first
        existing = await self.db.get_verification_by_user(interaction.user.id)
        if not existing:
            await interaction.response.send_message(
                "❌ You must be a verified TARUMT student before claiming Alumni status. "
                "Please run `/verify` first to verify your student account.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        # 2. If year not provided, open modal form
        if year is None:
            await interaction.response.send_modal(AlumniClaimModal(self.service))
            return

        # 3. Direct argument submission
        await interaction.response.defer(ephemeral=False, thinking=True)
        result = await self.service.claim_alumni_status(
            user_id=interaction.user.id,
            user_display_name=interaction.user.display_name,
            graduated_year=year,
            programme=programme,
            current_guild=interaction.guild,
        )

        if not result["success"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎓 Congratulations on Graduating, {interaction.user.display_name}!",
            description=(
                f"🎉 **{interaction.user.mention}** has successfully registered their **TARUMT Alumni** status!\n\n"
                f"• **Class Cohort**: Class of {result['graduated_year']}\n"
                f"• **Faculty**: {result['faculty_name']}\n"
                + (f"• **Programme**: {result['programme']}\n" if result['programme'] else "")
                + f"\n🏷️ **`TARUMT Alumni`** role assigned in {result['guilds_updated']} server(s).\n"
                f"🪪 **`❖ ALUMNI`** badge unlocked on your Digital Campus Card (`/card`)."
            ),
            color=discord.Color.from_rgb(212, 175, 55),
        )
        embed.set_footer(text="TARVeri Alumni Verification • Instant & Tamper-Proof")
        await interaction.followup.send(embed=embed, ephemeral=False)

    def invalidate_guild_cache(self, guild_id: int | None = None) -> None:
        """Clears cached channel settings for a guild or all guilds."""
        if guild_id is not None:
            self._guild_channels_cache.pop(guild_id, None)
        else:
            self._guild_channels_cache.clear()

    async def get_guild_channel_ids(self, guild_id: int) -> tuple[int | None, int | None]:
        """Fetches (welcome_channel_id, help_channel_id) for a guild with memory caching."""
        if not isinstance(guild_id, int):
            return (None, None)
        if guild_id in self._guild_channels_cache:
            return self._guild_channels_cache[guild_id]

        row = await self.db.get_guild_settings(guild_id)
        settings = (row[0], row[1]) if row else (None, None)
        self._guild_channels_cache[guild_id] = settings
        return settings

    async def is_help_channel(self, channel: discord.TextChannel) -> bool:
        """Determines if a channel is the designated help channel (per-guild DB, env setting, or keyword)."""
        if not isinstance(channel, discord.TextChannel) or not channel.guild:
            return False

        # 1. Per-server configured help channel in database
        _, guild_help_id = await self.get_guild_channel_ids(channel.guild.id)
        if guild_help_id is not None:
            configured_ch = channel.guild.get_channel(guild_help_id)
            if configured_ch is not None:
                return channel.id == guild_help_id
            else:
                # Channel was deleted on Discord — self-heal database setting
                await self.db.clear_stale_channel_setting(channel.guild.id, "help")
                self.invalidate_guild_cache(channel.guild.id)

        # 2. Global fallback setting from environment
        if self.settings and self.settings.help_channel_id:
            if channel.id == self.settings.help_channel_id:
                return True

        # 3. Autodetect: keywords: help, support, bantuan, faq, verify, verification, ask, question
        keywords = ("help", "support", "bantuan", "faq", "verify", "verification", "ask", "question")
        name_lower = channel.name.lower()
        matches_keyword = any(k in name_lower for k in keywords)

        if not matches_keyword:
            return False

        # Verify default role (@everyone) can view and send messages (unverified users can chat)
        if hasattr(channel, "permissions_for") and hasattr(channel.guild, "default_role"):
            everyone_perms = channel.permissions_for(channel.guild.default_role)
            if hasattr(everyone_perms, "view_channel") and hasattr(everyone_perms, "send_messages"):
                if not (everyone_perms.view_channel and everyone_perms.send_messages):
                    return False

        return True

    async def get_welcome_or_verify_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Finds the best channel to tag newly joined members for verification."""
        def _can_bot_send(c: discord.TextChannel) -> bool:
            if not hasattr(c, "permissions_for") or not hasattr(guild, "me") or not guild.me:
                return True
            perms = c.permissions_for(guild.me)
            return bool(perms.view_channel and perms.send_messages)

        # 1. Per-server configured welcome channel in database
        guild_welcome_id, _ = await self.get_guild_channel_ids(guild.id)
        if guild_welcome_id is not None:
            ch = guild.get_channel(guild_welcome_id)
            if isinstance(ch, discord.TextChannel) and _can_bot_send(ch):
                return ch
            elif ch is None:
                # Channel was deleted on Discord — self-heal database setting
                await self.db.clear_stale_channel_setting(guild.id, "welcome")
                self.invalidate_guild_cache(guild.id)

        # 2. Global configured welcome channel ID from settings (.env)
        if self.settings and self.settings.welcome_channel_id:
            ch = guild.get_channel(self.settings.welcome_channel_id)
            if isinstance(ch, discord.TextChannel) and _can_bot_send(ch):
                return ch

        # 3. Global configured help channel ID from settings (.env)
        if self.settings and self.settings.help_channel_id:
            ch = guild.get_channel(self.settings.help_channel_id)
            if isinstance(ch, discord.TextChannel) and _can_bot_send(ch):
                return ch

        # 4. Autodetect channel by priority keywords: welcome, verify, verification, start-here, help
        keywords = ("welcome", "verify", "verification", "start-here", "gate", "rules", "help")
        for kw in keywords:
            for ch in guild.text_channels:
                if kw in ch.name.lower() and _can_bot_send(ch):
                    return ch

        # 5. Guild system channel (standard Discord welcome channel)
        if guild.system_channel and _can_bot_send(guild.system_channel):
            return guild.system_channel

        # 6. First text channel bot can send to
        for ch in guild.text_channels:
            if _can_bot_send(ch):
                return ch

        return None

    def is_unverified_member(self, member: discord.Member) -> bool:
        """Checks if a member does not hold any TARVeri faculty role or approved guest role."""
        member_roles = getattr(member, "roles", [])
        if isinstance(member_roles, (list, tuple)):
            for r in member_roles:
                r_name = getattr(r, "name", "")
                if not r_name:
                    continue
                # Dynamic faculty role check
                if any(VerificationService._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES):
                    return False
                # Dynamic guest / visitor role check
                if GUEST_ROLE_PATTERN.search(r_name):
                    return False
        return True

    async def handle_help_channel_message(self, message: discord.Message) -> None:
        """Alerts unverified members asking about roles or verification with helpful interactive gateway buttons."""
        if not message.content or not isinstance(message.author, discord.Member) or message.author.bot:
            return

        # Do not respond to commands or prefixes
        if message.content.startswith("/") or message.content.startswith("!"):
            return

        # Only trigger for members who do not hold a faculty role or guest role in this server
        if not self.is_unverified_member(message.author):
            return

        # Check if message contains role or verification inquiry keywords
        if not ROLE_HELP_KEYWORDS_PATTERN.search(message.content):
            return

        # Rate limit tips per user (60-second cooldown) to avoid spamming chat
        now = time.monotonic()
        last_time = self._tip_cooldowns.get(message.author.id, 0.0)
        if now - last_time < 60.0:
            return
        self._tip_cooldowns[message.author.id] = now

        if len(self._tip_cooldowns) > 1000:
            self._tip_cooldowns = {uid: t for uid, t in self._tip_cooldowns.items() if now - t < 60.0}

        tip_embed = discord.Embed(
            title="🎓 TARUMT Student & Guest Verification",
            description=(
                f"👋 Hello {message.author.mention}! Looking to get your student or guest role?\n\n"
                f"Choose an option below to get verified:\n"
                f"• 🎓 **TARUMT Students:** Click **Verify TARUMT Student** or type `/verify` to enter your Student ID.\n"
                f"• 🎟️ **Referral Code:** Click **Enter Referral Code** if you received an invite code.\n"
                f"• 🌐 **Outside Guests:** Click **Apply as Guest** to request staff approval."
            ),
            color=discord.Color.blue(),
        )
        tip_embed.set_footer(text="TARVeri • Click a button below to get verified")

        view = (
            VerificationGatewayView(self.service, self.guest_service)
            if self.guest_service
            else None
        )

        try:
            await message.reply(embed=tip_embed, view=view, mention_author=True)
        except (discord.HTTPException, discord.Forbidden):
            try:
                await message.channel.send(embed=tip_embed, view=view)
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.warning(f"Could not send role help tip in #{message.channel.name}: {e}")
                return

        if message.guild:
            await self.db.log(
                "INFO",
                "ROLE_HELP_TIP",
                f"Alerted user {message.author} (ID: {message.author.id}) with role tips in #{message.channel.name} of '{message.guild.name}' (Guild ID: {message.guild.id})",
                guild=message.guild,
                user_id=message.author.id,
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Automatically assigns faculty roles if member is already verified, else prompts and tags."""
        existing = await self.db.get_verification_by_user(member.id)
        if existing:
            _, stored_faculty, _ = existing
            faculty_role = FACULTY_ROLES.get(stored_faculty)
            if faculty_role:
                result = await self.service.assign_role_across_guilds(member.id, faculty_role, [member.guild])
                if result.verified_in:
                    await self.db.log(
                        "INFO",
                        "AUTO_SYNC_JOIN",
                        f"Auto-assigned '{faculty_role}' to returning verified member {member} in '{member.guild.name}'",
                        guild=member.guild,
                        user_id=member.id,
                    )
                    try:
                        await member.send(
                            f"🎓 Welcome to **{member.guild.name}**! Because you are already verified with TARVeri, "
                            f"you have automatically received your **{faculty_role}** role."
                        )
                    except discord.Forbidden:
                        pass
                    return

        # New unverified member: Tag them in the server welcome/verification channel with embed and buttons
        welcome_channel = await self.get_welcome_or_verify_channel(member.guild)
        if welcome_channel:
            welcome_embed = discord.Embed(
                title="🎓 Welcome to the Server!",
                description=(
                    f"Welcome {member.mention} to **{member.guild.name}**!\n\n"
                    "Please choose an option below to gain access to the server:\n\n"
                    "• 🎓 **TARUMT Students:** Click **Verify TARUMT Student** to enter your Student ID and receive your Faculty Role.\n"
                    "• 🎟️ **Referral Code:** Click **Enter Referral Code** if a current student gave you an invite code.\n"
                    "• 🌐 **Outside Guests / Speakers:** Click **Apply as Guest** to request access from server staff."
                ),
                color=discord.Color.blue(),
            )
            welcome_embed.set_footer(text="TARVeri Student & Guest Verification • Instant & Secure")

            view = (
                VerificationGatewayView(self.service, self.guest_service)
                if self.guest_service
                else None
            )

            try:
                await welcome_channel.send(
                    content=f"👋 Welcome {member.mention}!",
                    embed=welcome_embed,
                    view=view,
                )
                await self.db.log(
                    "INFO",
                    "MEMBER_JOIN_TAGGED",
                    f"Tagged new member {member} (ID: {member.id}) for verification in #{welcome_channel.name} of '{member.guild.name}' (Guild ID: {member.guild.id})",
                    guild=member.guild,
                    user_id=member.id,
                )
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.warning(
                    f"Failed to tag new member {member} in #{welcome_channel.name} ({member.guild.name}): {e}"
                )

        try:
            dm_embed = discord.Embed(
                title=f"🎓 Welcome to {member.guild.name}!",
                description=(
                    "Please choose an option below to verify and unlock your server access, "
                    "or reply directly with your student ID (e.g. `23WMD09867`):"
                ),
                color=discord.Color.blue(),
            )
            dm_view = (
                VerificationGatewayView(self.service, self.guest_service)
                if self.guest_service
                else None
            )
            await member.send(embed=dm_embed, view=dm_view)
        except discord.Forbidden:
            await self.db.log(
                "INFO",
                "DM_BLOCKED_JOIN",
                f"Couldn't send welcome DM to {member} (ID: {member.id}) in '{member.guild.name}' — DMs closed.",
                guild=member.guild,
                user_id=member.id,
            )

    async def handle_expired_student_activity(self, message: discord.Message) -> None:
        """Checks if an active student has an expired student card and needs a lifecycle resolution prompt."""
        if not message.author or message.author.bot or message.guild is None:
            return

        now = time.monotonic()
        last_time = self._lifecycle_cooldowns.get(message.author.id, 0.0)
        # In-memory cooldown: 1 day per user session
        if now - last_time < 86400.0:
            return

        details = await self.db.get_verification_details(message.author.id)
        if not details or details.get("is_alumni") == 1:
            return

        card_expiry = details.get("card_expiry_date")
        if not card_expiry:
            return

        from datetime import datetime, timezone
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if card_expiry >= today_iso:
            return

        # Expired! Record in-memory cooldown
        self._lifecycle_cooldowns[message.author.id] = now
        if len(self._lifecycle_cooldowns) > 1000:
            self._lifecycle_cooldowns = {uid: t for uid, t in self._lifecycle_cooldowns.items() if now - t < 86400.0}

        # Check DB prompt cooldown (7 days)
        last_prompt_str = details.get("last_lifecycle_prompt_at")
        if last_prompt_str:
            try:
                from datetime import timedelta
                last_prompt_dt = datetime.fromisoformat(last_prompt_str)
                if last_prompt_dt.tzinfo is None:
                    last_prompt_dt = last_prompt_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_prompt_dt < timedelta(days=7):
                    return
            except ValueError:
                pass

        expiry_disp = format_card_expiry_display(card_expiry)
        embed = discord.Embed(
            title="🎓 TARUMT Student Card Expiry & Academic Status Confirmation",
            description=(
                f"Hello {message.author.mention}!\n\n"
                f"According to TARVeri records, your TARUMT student card reached its validity date (**{expiry_disp}**).\n\n"
                "Please confirm your current academic status:\n\n"
                "• 🎓 **I have Graduated:** Claim your official **TARUMT Alumni** role & card badge.\n"
                "• 📚 **Further Studies at TARUMT:** Progressing to Degree / Masters? Update your student ID & study level.\n"
                "• ⏳ **Still Studying / Extension:** Extending semester or final year project? Update your card expiry date."
            ),
            color=discord.Color.from_rgb(212, 175, 55),
        )
        embed.set_footer(text="TARVeri Academic Lifecycle Engine • Click an option below to update")
        view = StudentLifecycleResolutionView(self.service, self.db)

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            await message.author.send(embed=embed, view=view)
            await self.db.update_verification_profile(
                discord_user_id=message.author.id,
                lifecycle_prompt_status="prompted",
                last_lifecycle_prompt_at=now_iso,
            )
            await self.db.log(
                "INFO",
                "GRADUATION_PROMPT_SENT",
                f"Sent on-active-chat lifecycle prompt to {message.author} (ID: {message.author.id}, card expired: {expiry_disp})",
                user_id=message.author.id,
                guild=message.guild,
            )
        except discord.Forbidden:
            await self.db.update_verification_profile(
                discord_user_id=message.author.id,
                last_lifecycle_prompt_at=now_iso,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handles student ID messages in direct messages (DMs) and role tips in help channels."""
        if message.author.bot:
            return

        if message.guild is None:
            content = message.content.strip()
            if content:
                response = await self.service.perform_verification(message.author, content)
                if response:
                    await message.author.send(response)
        else:
            if isinstance(message.channel, discord.TextChannel) and await self.is_help_channel(message.channel):
                await self.handle_help_channel_message(message)
            await self.handle_expired_student_activity(message)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.log(
            "INFO", "GUILD_JOIN", f"Joined server '{guild.name}' (members: {guild.member_count})", guild=guild
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.db.log("INFO", "GUILD_REMOVE", f"Left server '{guild.name}'", guild=guild)
