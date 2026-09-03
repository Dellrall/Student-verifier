"""
Guest cog providing UI modals, persistent gateway buttons, referral code commands,
and private thread review orchestration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.config import FACULTY_ROLE_NAMES
from tarveri.database import Database
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri")


def is_admin_or_has_role(interaction: discord.Interaction, admin_role_name: str) -> bool:
    """Checks if the user has Administrator permission or the configured admin role."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.name == admin_role_name for r in interaction.user.roles)


class StudentVerificationModal(discord.ui.Modal, title="🎓 TARUMT Student Verification"):
    student_id = discord.ui.TextInput(
        label="Student ID",
        placeholder="e.g. 23WMD09867 or 22PMR12345",
        min_length=7,
        max_length=15,
        required=True,
    )

    def __init__(self, verification_service: VerificationService) -> None:
        super().__init__()
        self.verification_service = verification_service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        resp = await self.verification_service.perform_verification(
            interaction.user, self.student_id.value.strip()
        )
        await interaction.followup.send(resp, ephemeral=True)


class ReferralEntryModal(discord.ui.Modal, title="🎟️ Enter Student Referral Code"):
    code = discord.ui.TextInput(
        label="Referral Code",
        placeholder="e.g. TAR-8X2K9P",
        min_length=5,
        max_length=20,
        required=True,
    )

    def __init__(self, guest_service: GuestService) -> None:
        super().__init__()
        self.guest_service = guest_service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This can only be done in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        raw_code = self.code.value.strip().upper()

        success, msg, thread = await self.guest_service.open_guest_review_ticket(
            guild=interaction.guild,
            applicant=interaction.user,
            referral_code=raw_code,
        )

        if not success or not thread:
            await interaction.followup.send(msg, ephemeral=True)
            return

        # Fetch ticket details to render initial review panel
        ticket = await self.guest_service.db.get_guest_ticket_by_channel(thread.id)
        if ticket:
            embed = build_review_embed(ticket, interaction.guild, interaction.user)
            view = GuestReviewThreadView(self.guest_service)
            admin_mention = get_admin_role_mention(interaction.guild, self.guest_service.admin_role_name)
            vouch_prompt = f"\n👋 {interaction.user.mention} has submitted referral code `{raw_code}`."
            if ticket.get("referrer_id"):
                vouch_prompt += f" <@{ticket['referrer_id']}>, please confirm your vouch for this guest below."

            await thread.send(
                content=f"{admin_mention} {vouch_prompt}",
                embed=embed,
                view=view,
            )

        await interaction.followup.send(
            f"✅ Your referral code was verified! A private review thread has been opened: {thread.mention}. "
            "Please check that thread for staff approval.",
            ephemeral=True,
        )


class GuestApplicationModal(discord.ui.Modal, title="🌐 Guest Access Application"):
    name_affiliation = discord.ui.TextInput(
        label="Your Full Name & Affiliation",
        placeholder="e.g. Alex Tan, Sunway University / Speaker",
        max_length=100,
        required=True,
    )
    reason = discord.ui.TextInput(
        label="Reason for Joining This Server",
        placeholder="Explain why you are requesting guest access...",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )

    def __init__(self, guest_service: GuestService) -> None:
        super().__init__()
        self.guest_service = guest_service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This can only be done in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        reason_text = f"**Affiliation:** {self.name_affiliation.value.strip()}\n**Reason:** {self.reason.value.strip()}"

        success, msg, thread = await self.guest_service.open_guest_review_ticket(
            guild=interaction.guild,
            applicant=interaction.user,
            reason=reason_text,
        )

        if not success or not thread:
            await interaction.followup.send(msg, ephemeral=True)
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(thread.id)
        if ticket:
            embed = build_review_embed(ticket, interaction.guild, interaction.user)
            view = GuestReviewThreadView(self.guest_service)
            admin_mention = get_admin_role_mention(interaction.guild, self.guest_service.admin_role_name)

            await thread.send(
                content=f"{admin_mention} New guest application from {interaction.user.mention}:",
                embed=embed,
                view=view,
            )

        await interaction.followup.send(
            f"✅ Your application was submitted! A private review thread has been opened: {thread.mention}. "
            "Server staff will review your request shortly.",
            ephemeral=True,
        )


class RejectReasonModal(discord.ui.Modal, title="🛑 Rejection Reason"):
    reason = discord.ui.TextInput(
        label="Reason for Rejection",
        placeholder="e.g. Unable to verify affiliation / Invalid vouch",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=False,
    )

    def __init__(self, guest_service: GuestService, ticket: dict[str, Any], message: discord.Message) -> None:
        super().__init__()
        self.guest_service = guest_service
        self.ticket = ticket
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        await interaction.response.defer(ephemeral=True)
        success, reply_msg = await self.guest_service.reject_guest_application(
            ticket=self.ticket,
            guild=interaction.guild,
            admin_user=interaction.user,
            reason=self.reason.value,
        )

        # Update thread embed
        updated_ticket = await self.guest_service.db.get_guest_ticket_by_id(self.ticket["ticket_id"])
        applicant_member = interaction.guild.get_member(self.ticket["applicant_id"])
        if updated_ticket:
            embed = build_review_embed(updated_ticket, interaction.guild, applicant_member, status_override="REJECTED")
            disabled_view = discord.ui.View()
            try:
                await self.message.edit(embed=embed, view=disabled_view)
            except discord.HTTPException:
                pass

        await interaction.followup.send(reply_msg, ephemeral=True)

        if isinstance(interaction.channel, discord.Thread):
            await interaction.channel.send(
                f"🛑 **Application Rejected by {interaction.user.mention}.** This thread will be locked and archived."
            )
            await asyncio.sleep(5)
            try:
                await interaction.channel.edit(locked=True, archived=True)
            except discord.HTTPException:
                pass


class VouchModal(discord.ui.Modal, title="🤝 Confirm Referral Vouch"):
    vouch_note = discord.ui.TextInput(
        label="Vouch Statement / Context for Staff",
        placeholder="e.g. My classmate working on the graduation project with me.",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=True,
    )

    def __init__(self, guest_service: GuestService, ticket: dict[str, Any], message: discord.Message) -> None:
        super().__init__()
        self.guest_service = guest_service
        self.ticket = ticket
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        note = self.vouch_note.value.strip()
        await self.guest_service.db.update_guest_ticket_vouch(self.ticket["ticket_id"], note)

        updated_ticket = await self.guest_service.db.get_guest_ticket_by_id(self.ticket["ticket_id"])
        applicant_member = interaction.guild.get_member(self.ticket["applicant_id"]) if interaction.guild else None
        if updated_ticket and interaction.guild:
            embed = build_review_embed(updated_ticket, interaction.guild, applicant_member)
            view = GuestReviewThreadView(self.guest_service)
            try:
                await self.message.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass

        await interaction.followup.send("✅ Your vouch statement has been recorded for staff review!", ephemeral=True)
        if isinstance(interaction.channel, discord.Thread):
            await interaction.channel.send(f"🤝 **{interaction.user.mention} submitted a vouch:**\n> {note}")


def get_admin_role_mention(guild: discord.Guild, admin_role_name: str) -> str:
    role = discord.utils.get(guild.roles, name=admin_role_name)
    return role.mention if role else "@Staff"


def build_review_embed(
    ticket: dict[str, Any],
    guild: discord.Guild,
    applicant: discord.Member | discord.User | None,
    status_override: str | None = None,
) -> discord.Embed:
    status = status_override or ticket.get("status", "OPEN")
    color = discord.Color.gold()
    if status == "APPROVED":
        color = discord.Color.green()
    elif status in ("REJECTED", "EXPIRED"):
        color = discord.Color.red()

    embed = discord.Embed(
        title=f"📋 Guest Review Ticket #{ticket['ticket_id']}",
        description=f"Status: **{status}**",
        color=color,
    )
    applicant_mention = f"<@{ticket['applicant_id']}>" if not applicant else applicant.mention
    embed.add_field(name="Applicant", value=applicant_mention, inline=True)

    if ticket.get("referrer_id"):
        embed.add_field(name="Referred By", value=f"<@{ticket['referrer_id']}>", inline=True)
    if ticket.get("referral_code"):
        embed.add_field(name="Referral Code", value=f"`{ticket['referral_code']}`", inline=True)

    if ticket.get("vouch_note"):
        embed.add_field(name="Student Vouch Statement", value=f"💬 {ticket['vouch_note']}", inline=False)

    if ticket.get("reason"):
        embed.add_field(name="Application Details", value=ticket["reason"], inline=False)

    embed.set_footer(text=f"Server: {guild.name} • Created at {ticket.get('created_at', 'N/A')} UTC")
    return embed


class VerificationGatewayView(discord.ui.View):
    """Persistent 3-button verification gateway view for server welcome channels."""

    def __init__(self, verification_service: VerificationService, guest_service: GuestService) -> None:
        super().__init__(timeout=None)
        self.verification_service = verification_service
        self.guest_service = guest_service

    @discord.ui.button(
        label="Verify TARUMT Student",
        style=discord.ButtonStyle.primary,
        emoji="🎓",
        custom_id="tarveri:gateway:student",
    )
    async def verify_student(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = StudentVerificationModal(self.verification_service)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Enter Referral Code",
        style=discord.ButtonStyle.success,
        emoji="🎟️",
        custom_id="tarveri:gateway:referral",
    )
    async def enter_referral(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = ReferralEntryModal(self.guest_service)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Apply as Guest",
        style=discord.ButtonStyle.secondary,
        emoji="🌐",
        custom_id="tarveri:gateway:guest",
    )
    async def apply_guest(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = GuestApplicationModal(self.guest_service)
        await interaction.response.send_modal(modal)


class GuestReviewThreadView(discord.ui.View):
    """Interactive review buttons posted inside the private review thread."""

    def __init__(self, guest_service: GuestService) -> None:
        super().__init__(timeout=None)
        self.guest_service = guest_service

    @discord.ui.button(
        label="Approve Guest",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="tarveri:review:approve",
    )
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_admin_or_has_role(interaction, self.guest_service.admin_role_name):
            await interaction.response.send_message("❌ Only server administrators can approve guest requests.", ephemeral=True)
            return

        if not interaction.guild or not interaction.channel:
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket["status"] != "OPEN":
            await interaction.response.send_message("⚠️ This ticket is already resolved or not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        success, msg = await self.guest_service.approve_guest_application(ticket, interaction.guild, interaction.user)

        if success:
            updated_ticket = await self.guest_service.db.get_guest_ticket_by_id(ticket["ticket_id"])
            applicant_member = interaction.guild.get_member(ticket["applicant_id"])
            if updated_ticket:
                embed = build_review_embed(updated_ticket, interaction.guild, applicant_member, status_override="APPROVED")
                disabled_view = discord.ui.View()
                try:
                    await interaction.message.edit(embed=embed, view=disabled_view)
                except discord.HTTPException:
                    pass

            await interaction.followup.send(msg, ephemeral=True)
            if isinstance(interaction.channel, discord.Thread):
                await interaction.channel.send(
                    f"🎉 **Application Approved by {interaction.user.mention}!** This thread will be locked and archived."
                )
                await asyncio.sleep(5)
                try:
                    await interaction.channel.edit(locked=True, archived=True)
                except discord.HTTPException:
                    pass
        else:
            await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(
        label="Reject & Kick",
        style=discord.ButtonStyle.danger,
        emoji="🛑",
        custom_id="tarveri:review:reject",
    )
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_admin_or_has_role(interaction, self.guest_service.admin_role_name):
            await interaction.response.send_message("❌ Only server administrators can reject guest requests.", ephemeral=True)
            return

        if not interaction.guild or not interaction.channel:
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket["status"] != "OPEN":
            await interaction.response.send_message("⚠️ This ticket is already resolved or not found.", ephemeral=True)
            return

        modal = RejectReasonModal(self.guest_service, ticket, interaction.message)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Confirm Vouch",
        style=discord.ButtonStyle.primary,
        emoji="🤝",
        custom_id="tarveri:review:vouch",
    )
    async def vouch_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.channel:
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket["status"] != "OPEN":
            await interaction.response.send_message("⚠️ This ticket is already resolved or not found.", ephemeral=True)
            return

        # Only the referrer or an admin can provide vouch input
        is_referrer = ticket.get("referrer_id") == interaction.user.id
        is_admin = is_admin_or_has_role(interaction, self.guest_service.admin_role_name)
        if not (is_referrer or is_admin):
            await interaction.response.send_message("❌ Only the referring student or staff can submit a vouch statement.", ephemeral=True)
            return

        modal = VouchModal(self.guest_service, ticket, interaction.message)
        await interaction.response.send_modal(modal)


class GuestCog(commands.Cog, name="Guest"):
    """Handles guest verification, referral codes, and approval tickets."""

    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        guest_service: GuestService,
        verification_service: VerificationService,
    ) -> None:
        self.bot = bot
        self.db = db
        self.guest_service = guest_service
        self.verification_service = verification_service

    referral = app_commands.Group(name="referral", description="Commands to generate and manage guest referral codes")

    @referral.command(name="generate", description="Generate a guest referral code for a friend (Verified Students only).")
    @app_commands.describe(ttl_hours="How many hours until the code expires (default: 48, max: 168)")
    async def referral_generate(self, interaction: discord.Interaction, ttl_hours: int = 48) -> None:
        """Generates a guest referral code."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
            return

        # Check if user is a verified student
        is_verified = bool(await self.db.get_verification_by_user(interaction.user.id))
        if not is_verified:
            # Also check if member has any faculty role in Discord
            has_faculty_role = any(r.name in FACULTY_ROLE_NAMES for r in interaction.user.roles)
            is_verified = has_faculty_role

        if not is_verified:
            await interaction.response.send_message(
                "❌ Only verified TARUMT students can generate referral codes. Please verify your student status first with `/verify`.",
                ephemeral=True,
            )
            return

        hours = max(1, min(ttl_hours, 168))
        await interaction.response.defer(ephemeral=True)

        success, code_or_err = await self.guest_service.create_referral_code(
            guild_id=interaction.guild.id,
            referrer_user=interaction.user,
            ttl_hours=hours,
        )

        if success:
            embed = discord.Embed(
                title="🎟️ Guest Referral Code Generated",
                description=(
                    f"Here is your single-use referral code for **{interaction.guild.name}**:\n\n"
                    f"### `{code_or_err}`\n\n"
                    f"⏱️ **Expires In:** {hours} hour(s)\n"
                    f"⚠️ **Note:** Give this code to your friend. When they join and submit the code, a private "
                    f"approval thread will open with staff where you can vouch for them."
                ),
                color=discord.Color.blue(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(code_or_err, ephemeral=True)

    @referral.command(name="list", description="View your active and past referral codes.")
    async def referral_list(self, interaction: discord.Interaction) -> None:
        """Lists referral codes created by the caller in this server."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        codes = await self.db.get_user_referrals(interaction.guild.id, interaction.user.id, limit=10)

        if not codes:
            await interaction.followup.send(
                "ℹ️ You have not generated any referral codes in this server yet. Use `/referral generate` to create one.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🎟️ Your Referral Codes ({interaction.guild.name})",
            color=discord.Color.blue(),
        )
        for item in codes:
            status_emoji = "🟢" if item["status"] == "ACTIVE" else ("🟡" if item["status"] == "PENDING_APPROVAL" else "⚪")
            used_str = f" • Used by <@{item['used_by_discord_id']}>" if item.get("used_by_discord_id") else ""
            embed.add_field(
                name=f"`{item['code']}` {status_emoji} {item['status']}",
                value=f"Expires: `{item['expires_at']} UTC`{used_str}",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="send_gateway_panel",
        description="Send the interactive 3-button verification gateway panel to a channel.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Target channel (defaults to current channel)")
    async def send_gateway_panel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        """Posts the persistent verification gateway panel."""
        if not is_admin_or_has_role(interaction, self.guest_service.admin_role_name):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            return

        target_ch = channel or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.response.send_message("❌ Target must be a text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="🎓 Welcome to the Server!",
            description=(
                "Please choose how you would like to gain access to the server:\n\n"
                "• 🎓 **TARUMT Students:** Click **Verify TARUMT Student** to submit your Student ID and receive your Faculty Role.\n"
                "• 🎟️ **Have a Referral Code:** Click **Enter Referral Code** if a current student gave you an invite code.\n"
                "• 🌐 **Outside Guests / Speakers:** Click **Apply as Guest** to request access from server administration."
            ),
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text="TARVeri Student & Guest Verification System")

        view = VerificationGatewayView(self.verification_service, self.guest_service)
        try:
            await target_ch.send(embed=embed, view=view)
            await interaction.followup.send(
                f"✅ Verification gateway panel posted to {target_ch.mention}!", ephemeral=True
            )
        except (discord.HTTPException, discord.Forbidden) as e:
            await interaction.followup.send(f"❌ Failed to send gateway panel: {e}", ephemeral=True)
