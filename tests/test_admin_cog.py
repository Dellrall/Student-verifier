import pytest
from unittest.mock import MagicMock
import discord
from tarveri.cogs.admin_cog import is_admin_or_has_role


def test_is_admin_or_has_role():
    admin_role_name = "TARVeri Admin"

    # User without guild
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = None
    assert is_admin_or_has_role(interaction, admin_role_name) is False

    # Member with administrator permission
    interaction.guild = MagicMock(spec=discord.Guild)
    member_admin = MagicMock(spec=discord.Member)
    member_admin.guild_permissions.administrator = True
    member_admin.roles = []
    interaction.user = member_admin
    assert is_admin_or_has_role(interaction, admin_role_name) is True

    # Member with admin role
    member_role = MagicMock(spec=discord.Member)
    member_role.guild_permissions.administrator = False
    role = MagicMock(spec=discord.Role)
    role.name = "TARVeri Admin"
    member_role.roles = [role]
    interaction.user = member_role
    assert is_admin_or_has_role(interaction, admin_role_name) is True

    # Regular member without admin permission or role
    member_regular = MagicMock(spec=discord.Member)
    member_regular.guild_permissions.administrator = False
    other_role = MagicMock(spec=discord.Role)
    other_role.name = "Member"
    member_regular.roles = [other_role]
    interaction.user = member_regular
    assert is_admin_or_has_role(interaction, admin_role_name) is False


@pytest.mark.asyncio
async def test_setwelcomec_and_sethelpc(tmp_path):
    from unittest.mock import AsyncMock
    from tarveri.cogs.admin_cog import AdminCog
    from tarveri.database import Database

    db_path = str(tmp_path / "admin_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    guild.name = "My Server"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.__str__.return_value = "Admin#0001"

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 98765
    channel.name = "welcome"
    channel.mention = "<#98765>"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Set welcome channel
    await cog.setwelcomec.callback(cog, interaction, channel=channel)
    interaction.followup.send.assert_called_once()
    assert "<#98765>" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[0] == 98765

    # 2. Reset welcome channel
    interaction.followup.send.reset_mock()
    await cog.setwelcomec.callback(cog, interaction, channel=None)
    assert "auto-detect" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[0] is None

    # 3. Set help channel
    help_channel = MagicMock(spec=discord.TextChannel)
    help_channel.id = 54321
    help_channel.name = "help"
    help_channel.mention = "<#54321>"

    interaction.followup.send.reset_mock()
    await cog.sethelpc.callback(cog, interaction, channel=help_channel)
    assert "<#54321>" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[1] == 54321

    await db.close()


@pytest.mark.asyncio
async def test_sync_prefix(tmp_path):
    from unittest.mock import AsyncMock
    from tarveri.cogs.admin_cog import AdminCog
    from tarveri.database import Database

    db_path = str(tmp_path / "sync_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    bot.tree = MagicMock()
    bot.tree.sync = AsyncMock(return_value=[MagicMock(), MagicMock()])
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.name = "Test Guild"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    ctx = MagicMock()
    ctx.guild = guild
    ctx.author = admin_user
    msg = MagicMock()
    msg.edit = AsyncMock()
    ctx.send = AsyncMock(return_value=msg)

    await cog.sync_prefix.callback(cog, ctx, scope="guild")
    ctx.send.assert_called_once()
    msg.edit.assert_called_once()
    assert "Instantly synced **2** slash command(s)" in msg.edit.call_args[1]["content"]

    await db.close()


@pytest.mark.asyncio
async def test_setguestrole_and_setreviewchannel(tmp_path):
    from unittest.mock import AsyncMock
    from tarveri.cogs.admin_cog import AdminCog
    from tarveri.database import Database

    db_path = str(tmp_path / "admin_guest_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 998877
    guild.name = "Guest Test Guild"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 776655
    channel.name = "guest-tickets"
    channel.mention = "<#776655>"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Set guest role
    await cog.setguestrole.callback(cog, interaction, role_name="Guest (Approved)")
    interaction.followup.send.assert_called_once()
    assert "Guest (Approved)" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(998877)
    assert settings[2] == "Guest (Approved)"

    # 2. Set review channel
    interaction.followup.send.reset_mock()
    await cog.setreviewchannel.callback(cog, interaction, channel=channel)
    interaction.followup.send.assert_called_once()
    assert "<#776655>" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(998877)
    assert settings[3] == 776655

    await db.close()



