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
    ROLE_HELP_KEYWORDS_PATTERN,
    Settings,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri")


class VerificationModal(discord.ui.Modal, title="TARUMT Verification"):
    student_id = discord.ui.TextInput(
        label="Student ID",
        placeholder="e.g. 23WMD09867",
        min_length=10,
        max_length=10,
        required=True,
    )

    def __init__(self, service: VerificationService):
        super().__init__()
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        response_text = await self.service.perform_verification(interaction.user, self.student_id.value)
        await interaction.followup.send(response_text, ephemeral=True)


class VerificationCog(commands.Cog, name="Verification"):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        service: VerificationService,
        rate_limiter: RateLimiter,
        settings: Settings | None = None,
    ):
        self.bot = bot
        self.db = db
        self.service = service
        self.rate_limiter = rate_limiter
        self.settings = settings or getattr(bot, "settings", None)
        self._tip_cooldowns: dict[int, float] = {}
        self._guild_channels_cache: dict[int, tuple[int | None, int | None]] = {}

    @app_commands.command(
        name="verify",
        description="Verify your TARUMT student status and receive your faculty role.",
    )
    @app_commands.describe(
        student_id="Your TARUMT student ID (e.g. 23WMD09867). Leave blank to open input window."
    )
    async def verify_slash(self, interaction: discord.Interaction, student_id: str | None = None) -> None:
        """Slash command for verification with optional direct input or modal prompt."""
        if student_id:
            await interaction.response.defer(ephemeral=True, thinking=True)
            response_text = await self.service.perform_verification(interaction.user, student_id)
            await interaction.followup.send(response_text, ephemeral=True)
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
            await interaction.followup.send(
                summary or "ℹ️ You're already verified and up to date in every server I share with you.",
                ephemeral=True,
            )
            return

        # Open the interactive modal dialog
        await interaction.response.send_modal(VerificationModal(self.service))

    @commands.command(name="verify")
    async def verify_prefix(self, ctx: commands.Context, *args: str) -> None:
        """Fallback text command for members; guides them to privacy-safe verification."""
        if ctx.guild is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        if self.rate_limiter.is_rate_limited(ctx.author.id):
            await self.db.log(
                "WARNING", "RATE_LIMITED", f"{ctx.author} hit attempt limit on !verify", user_id=ctx.author.id
            )
            msg = "⏳ You've made too many verification attempts. Please wait a few minutes before trying again."
            if ctx.guild is not None:
                await ctx.send(f"{ctx.author.mention} {msg}", delete_after=15)
            else:
                await ctx.author.send(msg)
            return

        # In direct message (DM) with student ID argument
        if ctx.guild is None:
            if args:
                response_text = await self.service.perform_verification(ctx.author, args[0])
                await ctx.author.send(response_text)
            else:
                self.rate_limiter.record_attempt(ctx.author.id)
                await ctx.author.send("🎓 Please send your student ID (e.g., `23WMD09867`) directly here in DMs.")
            return

        self.rate_limiter.record_attempt(ctx.author.id)

        # Check existing verification
        existing = await self.db.get_verification_by_user(ctx.author.id)
        if existing:
            _, stored_faculty, _ = existing
            faculty_role = FACULTY_ROLES.get(stored_faculty)
            if faculty_role:
                mutual_guilds = await self.service.get_mutual_guilds_for_user(ctx.author.id)
                result = await self.service.assign_role_across_guilds(ctx.author.id, faculty_role, mutual_guilds)
                summary = self.service.format_role_summary(result)
                try:
                    await ctx.author.send(
                        summary
                        or "🪿 You're already verified! Your roles are up to date across all servers! 🚀"
                    )
                except discord.Forbidden:
                    pass
                await ctx.send(
                    f"{ctx.author.mention} Bro, you're already verified, you silly goose! 🪿🎓 Roles resynced! ✨",
                    delete_after=15,
                )
                return

        try:
            await ctx.author.send(
                "🎓 Please enter your student ID (e.g., `23WMD09867`) here to get verified, "
                "or use the `/verify` slash command directly in the server."
            )
            await ctx.send(
                f"{ctx.author.mention} I've sent you a DM to continue verification. "
                "You can also use `/verify` directly in this server!",
                delete_after=15,
            )
        except discord.Forbidden:
            await ctx.send(
                f"{ctx.author.mention} Your DMs are closed! Please use the `/verify` slash command "
                "directly in this server (only you will see the response).",
                delete_after=20,
            )

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
            return channel.id == guild_help_id

        # 2. Global fallback setting from environment
        if self.settings and self.settings.help_channel_id:
            if channel.id == self.settings.help_channel_id:
                return True

        # 3. Autodetect: keywords: help, support, bantuan, faq, verify, verification
        keywords = ("help", "support", "bantuan", "faq", "verify", "verification")
        name_lower = channel.name.lower()
        matches_keyword = any(k in name_lower for k in keywords)

        if not matches_keyword:
            return False

        # Verify default role (@everyone) can view and send messages (unverified users can chat)
        if hasattr(channel, "permissions_for") and hasattr(channel.guild, "default_role"):
            everyone_perms = channel.permissions_for(channel.guild.default_role)
            return bool(everyone_perms.view_channel and everyone_perms.send_messages)

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
        if any(r.name in FACULTY_ROLE_NAMES for r in member.roles):
            return False
        # Treat members with Guest roles as verified
        for r in member.roles:
            norm = r.name.lower().replace(" ", "")
            if "guest" in norm:
                return False
        return True

    async def handle_help_channel_message(self, message: discord.Message) -> None:
        """Alerts unverified members asking about roles with helpful verification tips."""
        if not isinstance(message.author, discord.Member) or message.author.bot:
            return

        # Do not respond to commands or prefixes
        prefix = self.bot.command_prefix
        if isinstance(prefix, str) and message.content.startswith(prefix):
            return
        if message.content.startswith("/"):
            return

        # Only trigger for members who do not have any faculty role
        if not self.is_unverified_member(message.author):
            return

        # Check if already verified in database
        existing = await self.db.get_verification_by_user(message.author.id)
        if existing:
            return

        # Check if message contains role inquiry keywords
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

        tip_text = (
            f"👋 Hello {message.author.mention}! Looking to get your student/faculty role?\n\n"
            f"Here is how to get verified:\n"
            f"1️⃣ Type `/verify` in any channel to open the verification form and enter your TARUMT Student ID (e.g. `23WMD09867`).\n"
            f"2️⃣ Or send your Student ID directly to me in a private DM!\n\n"
            f"*(Once verified, your faculty role will be assigned automatically.)* 🎓"
        )

        try:
            await message.reply(tip_text, mention_author=True)
        except (discord.HTTPException, discord.Forbidden):
            try:
                await message.channel.send(tip_text)
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.warning(f"Could not send role help tip in #{message.channel.name}: {e}")
                return

        if message.guild:
            await self.db.log(
                "INFO",
                "ROLE_HELP_TIP",
                f"Alerted unverified user {message.author} (ID: {message.author.id}) with role tips in #{message.channel.name} of '{message.guild.name}' (Guild ID: {message.guild.id})",
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

        # New unverified member: Tag them in the server welcome/verification channel
        welcome_channel = await self.get_welcome_or_verify_channel(member.guild)
        if welcome_channel:
            welcome_tag_msg = (
                f"👋 Welcome {member.mention} to **{member.guild.name}**! 🎓\n"
                f"Please verify your TARUMT student status to receive your faculty role and unlock server access.\n"
                f"• Type `/verify` in the server to submit your Student ID, or\n"
                f"• Send your Student ID (e.g., `23WMD09867`) directly to me in a private DM!"
            )
            try:
                await welcome_channel.send(welcome_tag_msg)
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
            await member.send(
                f"🎓 Welcome to **{member.guild.name}**! Please verify your student status by typing "
                f"`/verify` in the server or entering your student ID (e.g., `23WMD09867`) here in DMs:"
            )
        except discord.Forbidden:
            await self.db.log(
                "INFO",
                "DM_BLOCKED_JOIN",
                f"Couldn't send welcome DM to {member} (ID: {member.id}) in '{member.guild.name}' — DMs closed.",
                guild=member.guild,
                user_id=member.id,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handles student ID messages in direct messages (DMs) and role tips in help channels."""
        if message.author.bot:
            return

        if message.guild is None:
            if not message.content.startswith(self.bot.command_prefix):  # type: ignore
                response = await self.service.perform_verification(message.author, message.content)
                if response:
                    await message.author.send(response)
        else:
            if isinstance(message.channel, discord.TextChannel) and await self.is_help_channel(message.channel):
                await self.handle_help_channel_message(message)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.log(
            "INFO", "GUILD_JOIN", f"Joined server '{guild.name}' (members: {guild.member_count})", guild=guild
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.db.log("INFO", "GUILD_REMOVE", f"Left server '{guild.name}'", guild=guild)
