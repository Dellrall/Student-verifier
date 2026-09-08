import pytest
from unittest.mock import AsyncMock, MagicMock
import discord

from tarveri.cogs.guest_cog import GuestCog, VerificationGatewayView, GuestReviewThreadView
from tarveri.database import Database
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService


@pytest.mark.asyncio
async def test_guest_cog_referral_commands(tmp_path):
    db_path = str(tmp_path / "cog_guest.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guest_service = GuestService(bot, db, admin_role_name="TARVeri Admin")
    verification_service = MagicMock(spec=VerificationService)

    cog = GuestCog(bot, db, guest_service, verification_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    guild.name = "My Guild"

    # 1. Unverified student tries to generate referral code
    unverified_user = MagicMock(spec=discord.Member)
    unverified_user.id = 1001
    unverified_user.roles = []

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = unverified_user
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.referral_generate.callback(cog, interaction, ttl_hours=48)
    interaction.response.send_message.assert_called_once()
    assert "Only verified TARUMT students" in interaction.response.send_message.call_args[0][0]

    # 2. Verified student generates referral code
    await db.record_verification(1001, "hash1001", "M")
    interaction.response.send_message.reset_mock()

    await cog.referral_generate.callback(cog, interaction, ttl_hours=48)
    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "TAR-" in embed.description

    # 3. Referral list
    interaction.followup.send.reset_mock()
    await cog.referral_list.callback(cog, interaction)
    interaction.followup.send.assert_called_once()
    list_embed = interaction.followup.send.call_args[1]["embed"]
    assert len(list_embed.fields) == 1
    assert "TAR-" in list_embed.fields[0].name

    await db.close()


@pytest.mark.asyncio
async def test_send_gateway_panel(tmp_path):
    db_path = str(tmp_path / "gateway_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guest_service = GuestService(bot, db, admin_role_name="TARVeri Admin")
    verification_service = MagicMock(spec=VerificationService)
    cog = GuestCog(bot, db, guest_service, verification_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 111222

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    channel = MagicMock(spec=discord.TextChannel)
    channel.mention = "<#333444>"
    channel.send = AsyncMock()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.channel = channel
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.send_gateway_panel.callback(cog, interaction, channel=channel)
    channel.send.assert_called_once()
    interaction.followup.send.assert_called_once()
    assert "Verification gateway panel posted" in interaction.followup.send.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_guest_review_thread_double_verification(tmp_path):
    db_path = str(tmp_path / "double_verif_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guest_service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    view = GuestReviewThreadView(guest_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 555
    guild.name = "Double Guild"
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = MagicMock()

    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest(Approved)"
    guild.roles = [guest_role]

    admin_user = MagicMock(spec=discord.Member)
    admin_user.id = 9999
    admin_user.guild_permissions.administrator = True
    admin_user.roles = []
    admin_user.mention = "<@9999>"

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 8888
    applicant.top_role = MagicMock()
    applicant.top_role.__lt__.return_value = True
    applicant.add_roles = AsyncMock()
    applicant.send = AsyncMock()
    guild.get_member.return_value = applicant

    # Create referral ticket without vouch_note yet
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=applicant.id,
        referrer_id=7777,
        channel_id=4444,
        referral_code="TAR-DOUBLE",
    )

    channel = MagicMock(spec=discord.Thread)
    channel.id = 4444
    channel.send = AsyncMock()
    channel.edit = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.channel = channel
    interaction.user = admin_user
    interaction.message = message
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Admin tries to approve BEFORE voucher has confirmed
    await view.approve_btn.callback(interaction)
    interaction.response.send_message.assert_called_once()
    assert "Double Verification Required" in interaction.response.send_message.call_args[0][0]

    # 2. Voucher submits vouch
    await db.update_guest_ticket_vouch(ticket_id, "Confirmed classmate")
    interaction.response.send_message.reset_mock()

    # 3. Admin approves AFTER voucher has confirmed -> Success!
    await view.approve_btn.callback(interaction)
    interaction.followup.send.assert_called_once()
    applicant.add_roles.assert_called_once()

    ticket_after = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_after["status"] == "APPROVED"

    await db.close()

