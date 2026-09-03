import pytest
from unittest.mock import AsyncMock, MagicMock
import discord

from tarveri.database import Database
from tarveri.services.guest_service import GuestService, generate_code_string


@pytest.mark.asyncio
async def test_generate_code_format():
    code = generate_code_string()
    assert code.startswith("TAR-")
    assert len(code) == 10  # "TAR-" + 6 chars
    # Ensure no ambiguous characters
    for ch in "0O1I":
        assert ch not in code


@pytest.mark.asyncio
async def test_create_and_validate_referral_code(tmp_path):
    db_path = str(tmp_path / "guest_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    user = MagicMock(spec=discord.Member)
    user.id = 12345
    user.__str__.return_value = "Student#1234"

    guild_id = 999888

    # 1. Create first referral code
    success, code = await service.create_referral_code(guild_id, user, ttl_hours=24, max_active=2)
    assert success is True
    assert code.startswith("TAR-")

    # 2. Validate the referral code
    is_valid, err, record = await service.validate_referral_code(guild_id, code)
    assert is_valid is True
    assert err == ""
    assert record["referrer_discord_id"] == 12345
    assert record["status"] == "ACTIVE"

    # 3. Create second referral code
    success2, code2 = await service.create_referral_code(guild_id, user, ttl_hours=24, max_active=2)
    assert success2 is True

    # 4. Third referral code should exceed rate limit (max_active=2)
    success3, err3 = await service.create_referral_code(guild_id, user, ttl_hours=24, max_active=2)
    assert success3 is False
    assert "maximum allowed" in err3

    # 5. Invalid code validation
    is_valid_fake, err_fake, _ = await service.validate_referral_code(guild_id, "TAR-INVALID")
    assert is_valid_fake is False
    assert "Invalid referral code" in err_fake

    # 6. Wrong guild code validation
    is_valid_wrong_guild, err_wg, _ = await service.validate_referral_code(111222, code)
    assert is_valid_wrong_guild is False

    await db.close()


@pytest.mark.asyncio
async def test_guest_ticket_approval_and_rejection_lifecycle(tmp_path):
    db_path = str(tmp_path / "ticket_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 555666
    guild.name = "Test Server"
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.guild_permissions.kick_members = True
    guild.me.top_role = MagicMock()

    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest"
    guild.roles = [guest_role]

    admin_user = MagicMock(spec=discord.Member)
    admin_user.id = 999
    admin_user.mention = "<@999>"

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 888
    applicant.name = "Friend"
    applicant.display_name = "Friend"
    applicant.top_role = MagicMock()
    applicant.top_role.__lt__.return_value = True  # applicant role < bot top role
    applicant.add_roles = AsyncMock()
    applicant.send = AsyncMock()
    applicant.kick = AsyncMock()

    guild.get_member.side_effect = lambda uid: applicant if uid == 888 else None
    guild.fetch_member = AsyncMock(return_value=applicant)

    # 1. Create a referral code
    referrer = MagicMock(spec=discord.Member)
    referrer.id = 777
    referrer.__str__.return_value = "Referrer#0001"
    _, code = await service.create_referral_code(guild.id, referrer, ttl_hours=48)

    # Mock parent review channel and thread creation
    parent_channel = MagicMock(spec=discord.TextChannel)
    parent_channel.name = "approvals"
    perms = MagicMock()
    perms.view_channel = True
    perms.create_private_threads = True
    perms.send_messages_in_threads = True
    parent_channel.permissions_for.return_value = perms

    thread = MagicMock(spec=discord.Thread)
    thread.id = 444111
    thread.mention = "<#444111>"
    thread.add_user = AsyncMock()
    parent_channel.create_thread = AsyncMock(return_value=thread)
    guild.text_channels = [parent_channel]

    # Open review ticket with referral code
    success, msg, created_thread = await service.open_guest_review_ticket(
        guild=guild,
        applicant=applicant,
        referral_code=code,
    )
    assert success is True
    assert created_thread == thread
    thread.add_user.assert_called()

    # Code should now be PENDING_APPROVAL
    ref_record = await db.get_referral_code(code, guild.id)
    assert ref_record["status"] == "PENDING_APPROVAL"

    # Ticket in DB
    ticket = await db.get_guest_ticket_by_channel(thread.id)
    assert ticket is not None
    assert ticket["status"] == "OPEN"
    assert ticket["applicant_id"] == 888
    assert ticket["referrer_id"] == 777

    # Vouch
    await db.update_guest_ticket_vouch(ticket["ticket_id"], "Verified friend from college")
    ticket_vouched = await db.get_guest_ticket_by_id(ticket["ticket_id"])
    assert ticket_vouched["vouch_note"] == "Verified friend from college"

    # Approve application
    app_success, app_msg = await service.approve_guest_application(ticket, guild, admin_user)
    assert app_success is True
    applicant.add_roles.assert_called_once_with(guest_role, reason=f"TARVeri: Guest approved by {admin_user}")
    applicant.send.assert_called_once()
    assert "approved" in applicant.send.call_args[0][0].lower()

    # Code and ticket should now be marked USED / APPROVED
    ref_record_after = await db.get_referral_code(code, guild.id)
    assert ref_record_after["status"] == "USED"

    ticket_after = await db.get_guest_ticket_by_id(ticket["ticket_id"])
    assert ticket_after["status"] == "APPROVED"
    assert ticket_after["closed_by_admin_id"] == 999

    # Test Rejection flow on a second ticket
    _, code_rej = await service.create_referral_code(guild.id, referrer, ttl_hours=48)
    applicant2 = MagicMock(spec=discord.Member)
    applicant2.id = 666
    applicant2.display_name = "Spammer"
    applicant2.top_role = MagicMock()
    applicant2.top_role.__lt__.return_value = True
    applicant2.add_roles = AsyncMock()
    applicant2.send = AsyncMock()
    applicant2.kick = AsyncMock()
    guild.get_member.side_effect = lambda uid: applicant2 if uid == 666 else None
    guild.fetch_member = AsyncMock(return_value=applicant2)

    thread2 = MagicMock(spec=discord.Thread)
    thread2.id = 444222
    parent_channel.create_thread = AsyncMock(return_value=thread2)

    await service.open_guest_review_ticket(guild=guild, applicant=applicant2, referral_code=code_rej)
    ticket2 = await db.get_guest_ticket_by_channel(thread2.id)

    rej_success, rej_msg = await service.reject_guest_application(
        ticket2, guild, admin_user, reason="Suspicious account"
    )
    assert rej_success is True
    applicant2.send.assert_called_once()
    assert "Suspicious account" in applicant2.send.call_args[0][0]
    applicant2.kick.assert_called_once()

    ticket2_after = await db.get_guest_ticket_by_id(ticket2["ticket_id"])
    assert ticket2_after["status"] == "REJECTED"

    await db.close()


@pytest.mark.asyncio
async def test_get_or_create_guest_role_finds_existing(tmp_path):
    db_path = str(tmp_path / "role_reuse_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 112233
    guild.name = "Role Test Guild"

    # Server already has an existing "Guest(Approved)" role
    existing_guest_role = MagicMock(spec=discord.Role)
    existing_guest_role.name = "Guest(Approved)"
    guild.roles = [existing_guest_role]
    guild.create_role = AsyncMock()

    found_role = await service.get_or_create_guest_role(guild)
    assert found_role == existing_guest_role
    # Should not have called create_role because existing role was found
    guild.create_role.assert_not_called()

    # If role doesn't exist, create_role is called
    guild.roles = []
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    new_role = MagicMock(spec=discord.Role)
    new_role.name = "Guest(Approved)"
    guild.create_role = AsyncMock(return_value=new_role)

    created = await service.get_or_create_guest_role(guild)
    assert created == new_role
    guild.create_role.assert_called_once()
    assert guild.create_role.call_args[1]["name"] == "Guest(Approved)"

    await db.close()

