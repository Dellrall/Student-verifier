import time
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.cogs.verification_cog import VerificationCog
from tarveri.config import Settings
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.command_prefix = "!"
    bot.guilds = []
    return bot


@pytest.fixture
def mock_service():
    return MagicMock(spec=VerificationService)


@pytest.fixture
def mock_rate_limiter():
    return RateLimiter()


@pytest.mark.asyncio
async def test_is_help_channel_with_configured_id(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_test.db"))
    settings = Settings(
        bot_token="token",
        id_hash_secret="secret",
        help_channel_id=998877,
    )
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter, settings=settings)

    # Channel matching configured ID
    ch_match = MagicMock(spec=discord.TextChannel)
    ch_match.id = 998877
    assert cog.is_help_channel(ch_match) is True

    # Channel with different ID and non-help name
    ch_other = MagicMock(spec=discord.TextChannel)
    ch_other.id = 12345
    ch_other.name = "general"
    assert cog.is_help_channel(ch_other) is False


@pytest.mark.asyncio
async def test_is_help_channel_autodetect_keyword_and_permissions(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_test.db"))
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    default_role = MagicMock(spec=discord.Role)
    guild.default_role = default_role

    # Channel named "help" where @everyone can send messages
    ch_help = MagicMock(spec=discord.TextChannel)
    ch_help.name = "verification-help"
    ch_help.guild = guild
    perms_allowed = MagicMock()
    perms_allowed.view_channel = True
    perms_allowed.send_messages = True
    ch_help.permissions_for.return_value = perms_allowed

    assert cog.is_help_channel(ch_help) is True

    # Channel named "help" but @everyone cannot send messages (read-only announcements)
    ch_locked = MagicMock(spec=discord.TextChannel)
    ch_locked.name = "help-desk"
    ch_locked.guild = guild
    perms_locked = MagicMock()
    perms_locked.view_channel = True
    perms_locked.send_messages = False
    ch_locked.permissions_for.return_value = perms_locked

    assert cog.is_help_channel(ch_locked) is False


@pytest.mark.asyncio
async def test_get_welcome_or_verify_channel_priority(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_test.db"))
    settings = Settings(
        bot_token="token",
        id_hash_secret="secret",
        welcome_channel_id=111,
        help_channel_id=222,
    )
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter, settings=settings)

    guild = MagicMock(spec=discord.Guild)
    ch_welcome_configured = MagicMock(spec=discord.TextChannel)
    ch_welcome_configured.id = 111
    guild.get_channel.side_effect = lambda cid: ch_welcome_configured if cid == 111 else None

    # Priority 1: Configured welcome channel
    found = cog.get_welcome_or_verify_channel(guild)
    assert found == ch_welcome_configured

    # Priority 2: Autodetect welcome/verify keyword
    cog.settings = Settings(bot_token="token", id_hash_secret="secret")
    ch_verify = MagicMock(spec=discord.TextChannel)
    ch_verify.name = "verify-here"
    perms = MagicMock()
    perms.view_channel = True
    perms.send_messages = True
    ch_verify.permissions_for.return_value = perms

    guild.get_channel.side_effect = None
    guild.get_channel.return_value = None
    guild.text_channels = [ch_verify]
    guild.system_channel = None

    found = cog.get_welcome_or_verify_channel(guild)
    assert found == ch_verify


@pytest.mark.asyncio
async def test_is_unverified_member(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_test.db"))
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    member_unverified = MagicMock(spec=discord.Member)
    role_member = MagicMock(spec=discord.Role)
    role_member.name = "Member"
    member_unverified.roles = [role_member]
    assert cog.is_unverified_member(member_unverified) is True

    member_verified = MagicMock(spec=discord.Member)
    role_faculty = MagicMock(spec=discord.Role)
    role_faculty.name = "FOCS"
    member_verified.roles = [role_member, role_faculty]
    assert cog.is_unverified_member(member_verified) is False


@pytest.mark.asyncio
async def test_on_message_help_channel_keyword_alert(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_msg_test.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.name = "Test Guild"

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 456
    channel.name = "help"
    channel.guild = guild
    perms = MagicMock()
    perms.view_channel = True
    perms.send_messages = True
    channel.permissions_for.return_value = perms

    member = MagicMock(spec=discord.Member)
    member.id = 789
    member.bot = False
    member.roles = []
    member.mention = "<@789>"
    member.__str__.return_value = "Student#1234"

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.channel = channel
    message.author = member
    message.content = "Hello, how to get role?"
    message.reply = AsyncMock()

    # 1. Trigger role tip alert
    await cog.on_message(message)
    message.reply.assert_called_once()
    reply_arg = message.reply.call_args[0][0]
    assert "/verify" in reply_arg
    assert "<@789>" in reply_arg

    # 2. Test cooldown: sending another inquiry within 60s should NOT trigger another reply
    message.reply.reset_mock()
    message.content = "where is my role"
    await cog.on_message(message)
    message.reply.assert_not_called()

    await db.close()


@pytest.mark.asyncio
async def test_on_message_ignores_verified_member(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_verified_test.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "help"
    channel.guild = guild
    perms = MagicMock()
    perms.view_channel = True
    perms.send_messages = True
    channel.permissions_for.return_value = perms

    member = MagicMock(spec=discord.Member)
    member.id = 789
    member.bot = False
    faculty_role = MagicMock(spec=discord.Role)
    faculty_role.name = "FAFB"
    member.roles = [faculty_role]

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.channel = channel
    message.author = member
    message.content = "how to get role?"
    message.reply = AsyncMock()

    await cog.on_message(message)
    message.reply.assert_not_called()

    await db.close()


@pytest.mark.asyncio
async def test_on_member_join_tags_unverified_member(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_join_test.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.name = "TARUMT Campus"
    guild.me = MagicMock()

    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.name = "welcome"
    welcome_channel.send = AsyncMock()
    perms = MagicMock()
    perms.view_channel = True
    perms.send_messages = True
    welcome_channel.permissions_for.return_value = perms

    guild.text_channels = [welcome_channel]
    guild.system_channel = None

    new_member = MagicMock(spec=discord.Member)
    new_member.id = 99999
    new_member.guild = guild
    new_member.mention = "<@99999>"
    new_member.send = AsyncMock()
    new_member.__str__.return_value = "Newbie#0001"

    await cog.on_member_join(new_member)

    # Welcome channel should be sent a tag message
    welcome_channel.send.assert_called_once()
    tag_msg = welcome_channel.send.call_args[0][0]
    assert "<@99999>" in tag_msg
    assert "/verify" in tag_msg

    # DM should also be attempted
    new_member.send.assert_called_once()

    await db.close()


@pytest.mark.asyncio
async def test_on_member_join_auto_sync_already_verified(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_join_sync_test.db"))
    await db.connect()

    # Pre-record verification
    user_id = 88888
    await db.record_verification(user_id, "hash_123", "M")

    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.name = "TARUMT Campus"
    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.name = "welcome"
    welcome_channel.send = AsyncMock()
    guild.text_channels = [welcome_channel]

    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild = guild
    member.send = AsyncMock()
    member.__str__.return_value = "Returning#0002"

    mock_sync_result = MagicMock()
    mock_sync_result.verified_in = [("TARUMT Campus", "FOCS")]
    mock_service.assign_role_across_guilds = AsyncMock(return_value=mock_sync_result)

    await cog.on_member_join(member)

    # Should have called role assignment
    mock_service.assign_role_across_guilds.assert_called_once_with(user_id, "FOCS", [guild])
    # Should NOT have sent the new member tag message in welcome channel
    welcome_channel.send.assert_not_called()
    # Member gets confirmation DM
    member.send.assert_called_once()
    assert "automatically received your **FOCS** role" in member.send.call_args[0][0]

    await db.close()
