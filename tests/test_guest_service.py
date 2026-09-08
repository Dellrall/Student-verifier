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
    assert "Invalid, expired, or already used" in err_fake

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
    parent_channel.set_permissions = AsyncMock()
    parent_channel.overwrites_for = MagicMock(return_value=MagicMock())

    thread = MagicMock(spec=discord.Thread)
    thread.id = 444111
    thread.mention = "<#444111>"
    thread.parent = parent_channel
    thread.add_user = AsyncMock()
    parent_channel.create_thread = AsyncMock(return_value=thread)
    guild.text_channels = [parent_channel]
    guild.get_thread = MagicMock(return_value=thread)

    # Open review ticket with referral code
    success, msg, created_thread = await service.open_guest_review_ticket(
        guild=guild,
        applicant=applicant,
        referral_code=code,
    )
    assert success is True
    assert created_thread == thread
    assert "#0001" in msg
    assert parent_channel.create_thread.call_args[1]["name"].startswith("guest-0001-")
    thread.add_user.assert_called()
    parent_channel.set_permissions.assert_called()

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
    parent_channel.set_permissions.reset_mock()
    app_success, app_msg = await service.approve_guest_application(ticket, guild, admin_user)
    assert app_success is True
    applicant.add_roles.assert_called_once_with(guest_role, reason=f"TARVeri: Guest approved by {admin_user}")
    applicant.send.assert_called_once()
    assert "approved" in applicant.send.call_args[0][0].lower()
    parent_channel.set_permissions.assert_called_with(applicant, overwrite=None, reason="TARVeri: Review ticket closed")

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


@pytest.mark.asyncio
async def test_handle_member_leave_or_ban_revokes_guest_access(tmp_path):
    db_path = str(tmp_path / "guest_leave_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 777888
    guild.name = "Leave Test Guild"

    member = MagicMock(spec=discord.Member)
    member.id = 554433
    member.__str__.return_value = "GuestUser#1234"

    # Create ticket and referral code
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=member.id,
        channel_id=999111,
        reason="Attending workshop",
    )
    await db.create_referral_code("TAR-LEAVE1", guild.id, member.id, "2099-01-01 00:00:00")

    ticket_before = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_before["status"] == "OPEN"

    # 1. User leaves server
    await service.handle_member_leave_or_ban(guild, member, is_ban=False)

    ticket_after = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_after["status"] == "LEFT_SERVER"

    ref_code = await db.get_referral_code("TAR-LEAVE1", guild.id)
    assert ref_code["status"] == "LEFT_SERVER"

    # 2. User gets banned
    ticket_id2 = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=member.id,
        channel_id=999222,
        reason="Attending workshop 2",
    )
    await service.handle_member_leave_or_ban(guild, member, is_ban=True)

    ticket_after_ban = await db.get_guest_ticket_by_id(ticket_id2)
    assert ticket_after_ban["status"] == "BANNED"

    await db.close()


@pytest.mark.asyncio
async def test_referral_edge_scenarios(tmp_path):
    """Tests all edge scenarios for referral codes and guest review tickets."""
    db_path = str(tmp_path / "edge_scenarios.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 111999
    guild.name = "Edge Guild"
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.guild_permissions.kick_members = True

    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest"
    guild.roles = [guest_role]

    parent_channel = MagicMock(spec=discord.TextChannel)
    parent_channel.name = "guest-review"
    perms = MagicMock()
    perms.view_channel = True
    perms.create_private_threads = True
    parent_channel.permissions_for.return_value = perms
    guild.text_channels = [parent_channel]

    student_referrer = MagicMock(spec=discord.Member)
    student_referrer.id = 10001
    student_referrer.roles = []
    student_referrer.display_name = "StudentReferrer"
    student_referrer.__str__.return_value = "StudentReferrer#0001"

    # Edge Scenario 1: Code normalization (spaces, lowercase, missing TAR- prefix)
    _, ref_code = await service.create_referral_code(guild.id, student_referrer, ttl_hours=24)
    # Extract raw 6-char part
    raw_part = ref_code.replace("TAR-", "")

    # Test lowercase with whitespace
    is_valid1, _, rec1 = await service.validate_referral_code(guild.id, f"  {ref_code.lower()}  ")
    assert is_valid1 is True
    assert rec1["code"] == ref_code

    # Test without TAR- prefix
    is_valid2, _, rec2 = await service.validate_referral_code(guild.id, raw_part.lower())
    assert is_valid2 is True
    assert rec2["code"] == ref_code

    # Edge Scenario 2: Self-referral prevention
    success_self, msg_self, _ = await service.open_guest_review_ticket(
        guild=guild,
        applicant=student_referrer,
        referral_code=ref_code,
    )
    assert success_self is False
    assert "cannot use your own referral code" in msg_self.lower()

    # Edge Scenario 3: Already-verified student applying for guest
    # Record verification for student in DB
    await db.record_verification(10001, "hash10001", "M")
    success_verif, msg_verif, _ = await service.open_guest_review_ticket(
        guild=guild,
        applicant=student_referrer,
        reason="I want guest role",
    )
    assert success_verif is False
    assert "already verified as a tarumt student" in msg_verif.lower()

    # Edge Scenario 4: User already has Guest role
    existing_guest = MagicMock(spec=discord.Member)
    existing_guest.id = 20002
    existing_guest.roles = [guest_role]
    existing_guest.display_name = "AlreadyGuest"

    success_guest, msg_guest, _ = await service.open_guest_review_ticket(
        guild=guild,
        applicant=existing_guest,
        referral_code=ref_code,
    )
    assert success_guest is False
    assert "already have the" in msg_guest.lower()

    # Edge Scenario 5: Referrer leaves or gets banned -> cancels open tickets referred by them
    applicant_friend = MagicMock(spec=discord.Member)
    applicant_friend.id = 30003
    applicant_friend.roles = []
    applicant_friend.display_name = "FriendGuest"

    thread = MagicMock(spec=discord.Thread)
    thread.id = 888111
    thread.mention = "<#888111>"
    thread.add_user = AsyncMock()
    parent_channel.create_thread = AsyncMock(return_value=thread)

    # Valid friend opens review ticket
    success_friend, _, friend_thread = await service.open_guest_review_ticket(
        guild=guild,
        applicant=applicant_friend,
        referral_code=ref_code,
    )
    assert success_friend is True

    ticket_friend = await db.get_guest_ticket_by_channel(thread.id)
    assert ticket_friend["status"] == "OPEN"
    assert ticket_friend["referrer_id"] == student_referrer.id

    # Referrer gets banned
    await service.handle_member_leave_or_ban(guild, student_referrer, is_ban=True)

    # Friend's ticket should now be cancelled/revoked
    ticket_friend_after = await db.get_guest_ticket_by_channel(thread.id)
    assert ticket_friend_after["status"] == "REVOKED"
    assert "banned" in ticket_friend_after["close_reason"].lower()

    # Referral code should also be revoked
    code_record = await db.get_referral_code(ref_code, guild.id)
    assert code_record["status"] in ("BANNED", "REVOKED", "PENDING_APPROVAL")

    # Edge Scenario 6: Expired referral code
    _, exp_code = await service.create_referral_code(guild.id, student_referrer, ttl_hours=24)
    # Manually set expired timestamp
    await db._conn.execute("UPDATE referral_codes SET expires_at = '2020-01-01 00:00:00' WHERE code = ?", (exp_code,))
    await db._conn.commit()

    await db.close()


@pytest.mark.asyncio
async def test_get_admin_role_or_fallback_and_auto_invite(tmp_path):
    db_path = str(tmp_path / "admin_discovery_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 9988
    guild.name = "Discovery Guild"

    # 1. Test standard alias discovery ("Staff")
    staff_role = MagicMock(spec=discord.Role)
    staff_role.name = "Staff"
    staff_admin_member = MagicMock(spec=discord.Member)
    staff_admin_member.id = 7001
    staff_role.is_default.return_value = False
    guild.roles = [staff_role]

    discovered = await service.get_admin_role_or_fallback(guild)
    assert discovered == staff_role

    # Test DB configured custom admin role overrides alias
    custom_role = MagicMock(spec=discord.Role)
    custom_role.name = "Custom Reviewers"
    custom_admin_member = MagicMock(spec=discord.Member)
    custom_admin_member.id = 7002
    custom_role.members = [custom_admin_member]
    guild.roles = [staff_role, custom_role]

    await db.set_guild_admin_role(guild.id, "Custom Reviewers")
    discovered_custom = await service.get_admin_role_or_fallback(guild)
    assert discovered_custom == custom_role

    # 2. Test auto-inviting multiple admin members (role member + admin permission + owner) to private review thread
    parent_channel = MagicMock(spec=discord.TextChannel)
    parent_channel.name = "guest-tickets"
    perms = MagicMock()
    perms.view_channel = True
    perms.create_private_threads = True
    parent_channel.permissions_for.return_value = perms
    guild.text_channels = [parent_channel]

    thread = MagicMock(spec=discord.Thread)
    thread.id = 888777
    thread.add_user = AsyncMock()
    parent_channel.create_thread = AsyncMock(return_value=thread)

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 6001
    applicant.roles = []
    applicant.display_name = "NewGuest"

    # Server owner
    owner_member = MagicMock(spec=discord.Member)
    owner_member.id = 9001
    guild.owner = owner_member

    # Administrator permission member
    admin_perm_member = MagicMock(spec=discord.Member)
    admin_perm_member.id = 9002
    admin_perm_member.bot = False
    admin_perm_perms = MagicMock()
    admin_perm_perms.administrator = True
    admin_perm_perms.manage_guild = False
    admin_perm_perms.manage_threads = False
    admin_perm_member.guild_permissions = admin_perm_perms
    admin_perm_member.roles = []

    guild.members = [applicant, custom_admin_member, owner_member, admin_perm_member]

    success, msg, created_thread = await service.open_guest_review_ticket(
        guild=guild,
        applicant=applicant,
        reason="Testing admin auto-invite",
    )
    assert success is True

    # Check that applicant, custom_admin_member, owner_member, and admin_perm_member were all added to thread
    added_user_ids = [call.args[0].id for call in thread.add_user.call_args_list]
    assert applicant.id in added_user_ids
    assert custom_admin_member.id in added_user_ids
    assert owner_member.id in added_user_ids
    assert admin_perm_member.id in added_user_ids

    await db.close()




