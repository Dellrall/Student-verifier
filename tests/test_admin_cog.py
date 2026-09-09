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

    # Test !sync clean / deduplication
    bot.tree.clear_commands = MagicMock()
    msg.edit.reset_mock()
    await cog.sync_prefix.callback(cog, ctx, scope="clean")
    bot.tree.clear_commands.assert_called_once_with(guild=guild)
    assert "Deduplicated & Synced" in msg.edit.call_args[1]["content"]

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

    # 3. Set custom admin role
    admin_custom_role = MagicMock(spec=discord.Role)
    admin_custom_role.name = "Review Moderators"
    admin_custom_role.mention = "<@&334455>"
    interaction.followup.send.reset_mock()
    await cog.setadminrole.callback(cog, interaction, role=admin_custom_role)
    interaction.followup.send.assert_called_once()
    assert "<@&334455>" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(998877)
    assert settings[4] == "Review Moderators"

    # 4. Reset custom admin role
    interaction.followup.send.reset_mock()
    await cog.setadminrole.callback(cog, interaction, role=None)
    interaction.followup.send.assert_called_once()
    assert "auto-detect" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(998877)
    assert settings[4] is None

    await db.close()


@pytest.mark.asyncio
async def test_admin_commands_permission_denied(tmp_path):
    from unittest.mock import AsyncMock
    from tarveri.cogs.admin_cog import AdminCog
    from tarveri.database import Database

    db = Database(str(tmp_path / "perm_test.db"))
    await db.connect()
    cog = AdminCog(MagicMock(), db, MagicMock(), MagicMock(), admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    regular_user = MagicMock(spec=discord.Member)
    regular_user.guild_permissions.administrator = False
    regular_user.roles = []

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = regular_user
    interaction.response.send_message = AsyncMock()

    # stats
    await cog.stats.callback(cog, interaction)
    interaction.response.send_message.assert_called_once()
    assert "do not have permission" in interaction.response.send_message.call_args[0][0]

    # unverify
    interaction.response.send_message.reset_mock()
    await cog.unverify.callback(cog, interaction, user=MagicMock())
    interaction.response.send_message.assert_called_once()
    assert "do not have permission" in interaction.response.send_message.call_args[0][0]

    # audit
    interaction.response.send_message.reset_mock()
    await cog.audit.callback(cog, interaction)
    interaction.response.send_message.assert_called_once()
    assert "do not have permission" in interaction.response.send_message.call_args[0][0]

    # backup
    interaction.response.send_message.reset_mock()
    await cog.backup.callback(cog, interaction)
    interaction.response.send_message.assert_called_once()
    assert "do not have permission" in interaction.response.send_message.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_admin_unverify_lifecycle(tmp_path):
    from unittest.mock import AsyncMock
    from tarveri.cogs.admin_cog import AdminCog
    from tarveri.database import Database

    db = Database(str(tmp_path / "unverify_test.db"))
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.name = "Campus Guild"
    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.__str__.return_value = "Admin#0001"

    target_user = MagicMock(spec=discord.User)
    target_user.id = 777111
    target_user.mention = "<@777111>"
    target_user.__str__.return_value = "Student#7771"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Unverify on non-verified user
    await cog.unverify.callback(cog, interaction, user=target_user)
    interaction.followup.send.assert_called_once()
    assert "is not verified" in interaction.followup.send.call_args[0][0]

    # 2. Record verification then unverify
    await db.record_verification(target_user.id, "hash777", "M")
    member = MagicMock(spec=discord.Member)
    faculty_role = MagicMock(spec=discord.Role)
    faculty_role.name = "FOCS"
    member.roles = [faculty_role]
    member.remove_roles = AsyncMock()

    service.get_mutual_guilds_for_user = AsyncMock(return_value=[guild])
    service.get_or_fetch_member = AsyncMock(return_value=member)

    interaction.followup.send.reset_mock()
    await cog.unverify.callback(cog, interaction, user=target_user, reason="Graduated")
    interaction.followup.send.assert_called_once()
    assert "Successfully unverified" in interaction.followup.send.call_args[0][0]
    assert "Campus Guild (FOCS)" in interaction.followup.send.call_args[0][0]

    # Verification should be deleted from DB
    assert await db.get_verification_by_user(target_user.id) is None
    # Rate limiter was reset
    rate_limiter.reset.assert_called_with(target_user.id)

    await db.close()


@pytest.mark.asyncio
async def test_admin_stats_and_audit(tmp_path):
    from unittest.mock import AsyncMock
    from tarveri.cogs.admin_cog import AdminCog
    from tarveri.database import Database

    db = Database(str(tmp_path / "stats_audit_test.db"))
    await db.connect()

    bot = MagicMock()
    bot.guilds = [MagicMock()]
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1122
    guild.name = "Audit Test Guild"
    guild.get_channel.return_value = None

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Stats with 0 records
    await cog.stats.callback(cog, interaction)
    interaction.followup.send.assert_called_once()
    stats_embed = interaction.followup.send.call_args[1]["embed"]
    assert stats_embed.title == "📊 TARVeri — Verification Statistics"

    # 2. Add verifications & audit logs
    await db.record_verification(101, "hash101", "M")
    await db.record_verification(102, "hash102", "B")
    await db.log("INFO", "TEST_EVENT", "Audit test 1", guild=guild, user_id=101)

    # 3. Query audit
    interaction.followup.send.reset_mock()
    await cog.audit.callback(cog, interaction, limit=5, event_type="TEST_EVENT")
    interaction.followup.send.assert_called_once()
    audit_embed = interaction.followup.send.call_args[1]["embed"]
    assert "Audit Log Entries" in audit_embed.title
    assert len(audit_embed.fields) == 1

    # 4. Query audit with non-matching filter
    interaction.followup.send.reset_mock()
    await cog.audit.callback(cog, interaction, limit=5, event_type="NON_EXISTENT")
    interaction.followup.send.assert_called_once()
    assert "No audit log records found" in interaction.followup.send.call_args[0][0]

    # 5. Query guest tickets
    await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=3001,
        channel_id=4001,
        reason="Attending workshop",
    )
    interaction.followup.send.reset_mock()
    await cog.guest_tickets.callback(cog, interaction, status=None, limit=5)
    interaction.followup.send.assert_called_once()
    gt_embed = interaction.followup.send.call_args[1]["embed"]
    assert "Guest Review Tickets" in gt_embed.title
    assert len(gt_embed.fields) == 1
    assert "Ticket #A0001" in gt_embed.fields[0].name

    await db.close()


@pytest.mark.asyncio
async def test_admin_diagnose_command(tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    from tarveri.cogs.admin_cog import AdminCog
    from tarveri.database import Database

    db = Database(str(tmp_path / "diagnose_test.db"))
    await db.connect()

    service = MagicMock()
    service.reconcile_duplicate_roles = AsyncMock(
        return_value={"checked_categories": 1, "migrated_members": 2, "deleted_roles": 1, "failed": 0, "details": []}
    )
    service.diagnose_guild_permissions.return_value = ["⚠️ Role hierarchy conflict: Role FOCS is higher than bot role."]
    service.reconcile_verified_members = AsyncMock(return_value={"checked": 5, "restored": 2, "failed": 0})

    cog = AdminCog(MagicMock(), db, service, MagicMock(), admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 8877
    guild.name = "Diagnose Guild"

    # Set up channels
    welcome_ch = MagicMock(spec=discord.TextChannel)
    welcome_ch.id = 1111
    welcome_ch.mention = "<#1111>"

    # Set up settings with valid welcome, but stale help and review channels
    await db.set_guild_welcome_channel(guild.id, 1111)
    await db.set_guild_help_channel(guild.id, 2222)  # Stale (get_channel returns None)
    await db.set_guild_review_channel(guild.id, 3333)  # Stale

    guild.get_channel.side_effect = lambda cid: welcome_ch if cid == 1111 else None

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.diagnose.callback(cog, interaction)

    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "Server Health & Diagnostics" in embed.title
    assert len(embed.fields) >= 4
    assert any("Duplicate Role Cleanup" in f.name for f in embed.fields)

    # Verify stale channels were cleaned in DB
    settings = await db.get_guild_settings(guild.id)
    assert settings[0] == 1111  # Kept
    assert settings[1] is None  # Stale cleared
    assert settings[3] is None  # Stale cleared

    await db.close()
