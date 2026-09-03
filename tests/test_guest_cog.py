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
