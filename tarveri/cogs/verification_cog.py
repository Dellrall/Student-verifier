"""
Discord UI and Commands for Student Verification.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.config import FACULTY_ROLES
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService


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
    def __init__(self, bot: commands.Bot, db: Database, service: VerificationService, rate_limiter: RateLimiter):
        self.bot = bot
        self.db = db
        self.service = service
        self.rate_limiter = rate_limiter

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

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Automatically assigns faculty roles if member is already verified, else prompts."""
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
        """Handles student ID messages in direct messages (DMs)."""
        if message.guild is None and not message.author.bot:
            if not message.content.startswith(self.bot.command_prefix):  # type: ignore
                response = await self.service.perform_verification(message.author, message.content)
                if response:
                    await message.author.send(response)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.log(
            "INFO", "GUILD_JOIN", f"Joined server '{guild.name}' (members: {guild.member_count})", guild=guild
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.db.log("INFO", "GUILD_REMOVE", f"Left server '{guild.name}'", guild=guild)
