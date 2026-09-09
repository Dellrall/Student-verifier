import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import pytest
from tarveri.services.verification_service import VerificationService, RoleSyncResult
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.config import hash_student_id


@pytest.mark.asyncio
async def test_format_role_summary():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    result = RoleSyncResult(
        verified_in=[("Server A", "FOCS")],
        already_had_role_in=[("Server B", "FOCS")],
        missing_role_in=["Server C"],
        failed_in=["Server D"],
    )

    summary = service.format_role_summary(result)
    assert "Server A" in summary
    assert "FOCS" in summary
    assert "Server B" in summary
    assert "Server C" in summary
    assert "Server D" in summary


@pytest.mark.asyncio
async def test_perform_verification_invalid_format(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "service_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    user = MagicMock()
    user.id = 12345
    user.__str__.return_value = "TestUser#0001"

    response = await service.perform_verification(user, "invalid_id")
    assert "Invalid student ID format" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_duplicate_id(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "duplicate_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    secret = "secret_123"
    service = VerificationService(bot, db, secret, rate_limiter)

    student_id = "23WMD09867"
    hashed = hash_student_id(student_id, secret)

    # First user is already verified with this ID
    await db.record_verification(11111, hashed, "M")

    # Second user tries to use same student ID
    second_user = MagicMock()
    second_user.id = 22222
    second_user.__str__.return_value = "SecondUser#0002"

    response = await service.perform_verification(second_user, student_id)
    assert "already been used to verify a different Discord account" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_already_verified_resync(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Campus Server"
    bot.guilds = [guild]

    db = Database(str(tmp_path / "resync_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    secret = "secret_123"
    service = VerificationService(bot, db, secret, rate_limiter)

    student_id = "23WMD09867"
    hashed = hash_student_id(student_id, secret)
    user_id = 33333

    # User already verified
    await db.record_verification(user_id, hashed, "M")

    user = MagicMock()
    user.id = user_id
    user.__str__.return_value = "User#3333"

    # Mock member with existing role
    member = MagicMock(spec=discord.Member)
    role = MagicMock(spec=discord.Role)
    role.name = "FOCS"
    member.roles = [role]
    guild.get_member.return_value = member

    response = await service.perform_verification(user, student_id)
    assert "already had a faculty role" in response or "already verified" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_rate_limited(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "ratelimit_test.db"))
    await db.connect()
    rate_limiter = RateLimiter(max_attempts=1, window_seconds=60)
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 44444
    user.__str__.return_value = "RateLimitedUser#0001"

    # First attempt consumes the 1 allowed attempt
    rate_limiter.record_attempt(user.id)
    assert rate_limiter.is_rate_limited(user.id)

    response = await service.perform_verification(user, "23WMD09867")
    assert "too many verification attempts" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_unknown_faculty_code(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "unknown_faculty_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 55555
    user.__str__.return_value = "User#5555"

    # 'Z' is not a valid faculty code
    response = await service.perform_verification(user, "23WZD09867")
    assert "Student ID does not match any known faculty" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_account_already_verified_different_id(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "different_id_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    secret = "secret_123"
    service = VerificationService(bot, db, secret, rate_limiter)

    user_id = 66666
    old_id = "23WMD09867"
    old_hash = hash_student_id(old_id, secret)
    await db.record_verification(user_id, old_hash, "M")

    user = MagicMock()
    user.id = user_id
    user.__str__.return_value = "User#6666"

    # User attempts to verify under a different valid student ID
    response = await service.perform_verification(user, "23WKD11111")
    assert "already verified under a different student ID" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_no_mutual_guilds(tmp_path):
    bot = MagicMock()
    bot.guilds = []
    db = Database(str(tmp_path / "no_guilds_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 77777
    user.__str__.return_value = "User#7777"

    response = await service.perform_verification(user, "23WMD09867")
    assert "couldn't find you in any server" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_role_creation_and_assignment_success(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Campus Alpha"
    guild.roles = []
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = MagicMock()

    created_role = MagicMock(spec=discord.Role)
    created_role.name = "FOCS"
    # Created role is lower than bot top role
    created_role.__ge__.return_value = False
    guild.create_role = AsyncMock(return_value=created_role)

    member = MagicMock(spec=discord.Member)
    member.roles = []
    member.add_roles = AsyncMock()
    guild.get_member.return_value = member

    bot.guilds = [guild]

    db = Database(str(tmp_path / "success_assign_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 88888
    user.__str__.return_value = "User#8888"

    response = await service.perform_verification(user, "23WMD09867")
    assert "You've been given the following role(s)" in response
    assert "Campus Alpha" in response
    assert "FOCS" in response

    guild.create_role.assert_called_once()
    member.add_roles.assert_called_once_with(created_role, reason="TARVeri: Student verification role assignment")

    # Verification recorded in DB
    record = await db.get_verification_by_user(88888)
    assert record is not None
    assert record[1] == "M"

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_role_create_missing_manage_roles_permission(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Locked Server"
    guild.roles = []
    guild.me = MagicMock()
    # Bot does NOT have manage_roles permission
    guild.me.guild_permissions.manage_roles = False

    member = MagicMock(spec=discord.Member)
    member.roles = []
    guild.get_member.return_value = member
    bot.guilds = [guild]

    db = Database(str(tmp_path / "noperms_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 99999
    user.__str__.return_value = "User#9999"

    response = await service.perform_verification(user, "23WMD09867")
    assert "I couldn't create/find the required role" in response
    assert "Locked Server" in response

    # Should NOT record verification in DB because no roles were assigned
    record = await db.get_verification_by_user(99999)
    assert record is None

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_database_collision_rollback(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 555123
    guild.name = "Rollback Server"

    existing_role = MagicMock(spec=discord.Role)
    existing_role.name = "FOCS"
    existing_role.__ge__.return_value = False
    guild.roles = [existing_role]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = MagicMock()

    member = MagicMock(spec=discord.Member)
    member.roles = []

    async def _mock_add_roles(r, **kwargs):
        member.roles.append(r)

    member.add_roles = AsyncMock(side_effect=_mock_add_roles)
    member.remove_roles = AsyncMock()
    guild.get_member.return_value = member
    bot.guilds = [guild]
    bot.get_guild.return_value = guild

    db = Database(str(tmp_path / "collision_test.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        user = MagicMock()
        user.id = 10101
        user.__str__.return_value = "User#10101"

        # Simulate database collision on record_verification by raising IntegrityError
        with patch.object(db, "record_verification", side_effect=sqlite3.IntegrityError("UNIQUE constraint failed")):
            response = await service.perform_verification(user, "23WMD09867")
            assert "Verification failed due to a collision" in response
            # Rollback should remove the assigned role
            member.remove_roles.assert_called_once()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_verified_members_restores_missing_faculty_role(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Reconcile Guild"

    # Setup FOCS role
    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 10
    guild.roles = [focs_role]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock(spec=discord.Role)
    bot_top_role.name = "TARVeri Bot"
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role

    # Student 55555 is verified in DB as FOCS ("M"), but missing role in Discord
    member = MagicMock(spec=discord.Member)
    member.id = 55555
    member.roles = []
    member.add_roles = AsyncMock()

    guild.get_member.side_effect = lambda uid: member if uid == 55555 else None
    bot.guilds = [guild]

    db = Database(str(tmp_path / "reconcile_roles_test.db"))
    await db.connect()
    await db.record_verification(55555, "hash_55555", "M")

    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    # Run reconciliation
    summary = await service.reconcile_verified_members(guild)
    assert summary["checked"] == 1
    assert summary["restored"] == 1
    assert summary["failed"] == 0

    member.add_roles.assert_awaited_once_with(
        focs_role, reason="TARVeri: Self-healing automatic role restoration for verified student"
    )

    await db.close()


def test_diagnose_guild_permissions_hierarchy_and_permissions(tmp_path):
    bot = MagicMock()
    db = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.name = "Diagnosis Guild"

    # 1. Missing Manage Roles permission
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = False
    bot_top_role = MagicMock()
    bot_top_role.name = "TARVeri"
    bot_top_role.position = 10
    guild.me.top_role = bot_top_role
    guild.roles = []

    warnings = service.diagnose_guild_permissions(guild)
    assert any("Manage Roles" in w for w in warnings)

    # 2. Hierarchy conflict: Faculty role higher than bot role
    guild.me.guild_permissions.manage_roles = True
    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 20  # Higher than bot (10)
    guild.roles = [focs_role]

    warnings = service.diagnose_guild_permissions(guild)
    assert any("Role hierarchy conflict" in w and "FOCS" in w for w in warnings)

    # 3. Healthy configuration: Bot role higher than all managed roles
    bot_top_role.position = 100
    warnings = service.diagnose_guild_permissions(guild)
    assert warnings == []

    # 4. Duplicate role detection: multiple roles matching same faculty
    extra_focs = MagicMock(spec=discord.Role)
    extra_focs.name = "focs"
    extra_focs.position = 5
    guild.roles = [focs_role, extra_focs]
    warnings = service.diagnose_guild_permissions(guild)
    assert any("Duplicate faculty roles detected" in w and "FOCS" in w for w in warnings)


@pytest.mark.asyncio
async def test_find_faculty_role_multi_tier_matching():
    bot = MagicMock()
    db = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    guild = MagicMock(spec=discord.Guild)

    # 1. Exact match
    r1 = MagicMock(spec=discord.Role, name="FOCS")
    r1.name = "FOCS"
    r1.position = 10
    guild.roles = [r1]
    assert await service.find_faculty_role(guild, "FOCS") == r1

    # 2. Case-insensitive & trimmed match
    r2 = MagicMock(spec=discord.Role, name="focs ")
    r2.name = "focs "
    r2.position = 10
    guild.roles = [r2]
    assert await service.find_faculty_role(guild, "FOCS") == r2

    # 3. Normalized alphanumeric / bracket / emoji match
    r3 = MagicMock(spec=discord.Role, name="[FOCS]")
    r3.name = "[FOCS]"
    r3.position = 10
    guild.roles = [r3]
    assert await service.find_faculty_role(guild, "FOCS") == r3

    # 4. Prefix & word-boundary match
    r4 = MagicMock(spec=discord.Role, name="FOCS - Faculty of Computing")
    r4.name = "FOCS - Faculty of Computing"
    r4.position = 10
    guild.roles = [r4]
    assert await service.find_faculty_role(guild, "FOCS") == r4

    # 5. Full name expansion without acronym (e.g. "Faculty of Computing and Information Technology")
    r5 = MagicMock(spec=discord.Role, name="Faculty of Computing and Information Technology")
    r5.name = "Faculty of Computing and Information Technology"
    r5.position = 10
    guild.roles = [r5]
    assert await service.find_faculty_role(guild, "FOCS") == r5

    # 6. Pre-University Studies for CPUS
    r6 = MagicMock(spec=discord.Role, name="Centre for Pre-University Studies")
    r6.name = "Centre for Pre-University Studies"
    r6.position = 10
    guild.roles = [r6]
    assert await service.find_faculty_role(guild, "CPUS") == r6

    # 7. Engineering & Technology for FOET
    r7 = MagicMock(spec=discord.Role, name="Faculty of Engineering & Technology")
    r7.name = "Faculty of Engineering & Technology"
    r7.position = 10
    guild.roles = [r7]
    assert await service.find_faculty_role(guild, "FOET") == r7


@pytest.mark.asyncio
async def test_find_faculty_role_live_fetch_roles_fallback():
    bot = MagicMock()
    db = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    # Cache is empty
    guild.roles = []

    live_role = MagicMock(spec=discord.Role, name="FOCS")
    live_role.name = "FOCS"
    live_role.position = 15
    guild.fetch_roles = AsyncMock(return_value=[live_role])

    # Should query fetch_roles and return the live role
    found = await service.find_faculty_role(guild, "FOCS")
    assert found == live_role
    guild.fetch_roles.assert_called_once()


@pytest.mark.asyncio
async def test_perform_verification_never_duplicates_existing_fuzzy_or_cached_role(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 998811
    guild.name = "Banana Hub"

    # Server already has an existing role (e.g. named "FOCS" or "focs")
    existing_role = MagicMock(spec=discord.Role)
    existing_role.name = "FOCS"
    existing_role.position = 5
    existing_role.__ge__.return_value = False
    guild.roles = [existing_role]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock()
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role
    guild.create_role = AsyncMock()

    member = MagicMock(spec=discord.Member)
    member.roles = []
    member.add_roles = AsyncMock()
    guild.get_member.return_value = member
    bot.guilds = [guild]
    bot.get_guild.return_value = guild

    db = Database(str(tmp_path / "never_duplicate.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        user = MagicMock()
        user.id = 12345
        user.__str__.return_value = "Student#1234"

        response = await service.perform_verification(user, "23WMD09867")
        assert "You've been given the following role(s)" in response

        # create_role must NEVER be called because the role already exists
        guild.create_role.assert_not_called()
        # existing role was assigned to member
        member.add_roles.assert_called_once_with(existing_role, reason="TARVeri: Student verification role assignment")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_duplicate_roles_migrates_members_and_deletes_redundant_roles(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 998822
    guild.name = "Dedup Guild"

    # 1. Primary FOCS role (pos: 15, name: "FOCS")
    primary_focs = MagicMock(spec=discord.Role)
    primary_focs.id = 1001
    primary_focs.name = "FOCS"
    primary_focs.position = 15
    primary_focs.managed = False
    primary_focs.is_default.return_value = False
    primary_focs.delete = AsyncMock()

    # 2. Redundant duplicate FOCS role 1 (pos: 2, name: "focs")
    redundant_focs_1 = MagicMock(spec=discord.Role)
    redundant_focs_1.id = 1002
    redundant_focs_1.name = "focs"
    redundant_focs_1.position = 2
    redundant_focs_1.managed = False
    redundant_focs_1.is_default.return_value = False
    redundant_focs_1.delete = AsyncMock()

    # 3. Redundant duplicate FOCS role 2 (pos: 1, name: "[FOCS]")
    redundant_focs_2 = MagicMock(spec=discord.Role)
    redundant_focs_2.id = 1003
    redundant_focs_2.name = "[FOCS]"
    redundant_focs_2.position = 1
    redundant_focs_2.managed = False
    redundant_focs_2.is_default.return_value = False
    redundant_focs_2.delete = AsyncMock()

    # 4. Guest roles
    primary_guest = MagicMock(spec=discord.Role)
    primary_guest.id = 2001
    primary_guest.name = "Guest(Approved)"
    primary_guest.position = 10
    primary_guest.managed = False
    primary_guest.is_default.return_value = False
    primary_guest.delete = AsyncMock()

    redundant_guest = MagicMock(spec=discord.Role)
    redundant_guest.id = 2002
    redundant_guest.name = "Guest"
    redundant_guest.position = 3
    redundant_guest.managed = False
    redundant_guest.is_default.return_value = False
    redundant_guest.delete = AsyncMock()

    # Members setup
    m1 = MagicMock(spec=discord.Member)  # Already has primary FOCS
    m1.roles = [primary_focs]
    m1.add_roles = AsyncMock()
    m1.remove_roles = AsyncMock()

    m2 = MagicMock(spec=discord.Member)  # Has redundant FOCS 1, missing primary
    m2.roles = [redundant_focs_1]
    m2.add_roles = AsyncMock()
    m2.remove_roles = AsyncMock()

    m3 = MagicMock(spec=discord.Member)  # Has redundant FOCS 2 and primary
    m3.roles = [redundant_focs_2, primary_focs]
    m3.add_roles = AsyncMock()
    m3.remove_roles = AsyncMock()

    guest_m = MagicMock(spec=discord.Member)  # Has redundant Guest, missing primary
    guest_m.roles = [redundant_guest]
    guest_m.add_roles = AsyncMock()
    guest_m.remove_roles = AsyncMock()

    primary_focs.members = [m1, m3]
    redundant_focs_1.members = [m2]
    redundant_focs_2.members = [m3]
    primary_guest.members = []
    redundant_guest.members = [guest_m]

    guild.roles = [primary_focs, redundant_focs_1, redundant_focs_2, primary_guest, redundant_guest]

    # Bot perms and top role
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock()
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role

    db = Database(str(tmp_path / "dedup_test.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        stats = await service.reconcile_duplicate_roles(guild)

        assert stats["deleted_roles"] == 3
        assert stats["migrated_members"] == 2  # m2 and guest_m
        assert stats["failed"] == 0

        # Verify m2 was given primary FOCS and had redundant FOCS removed
        m2.add_roles.assert_called_once()
        assert m2.add_roles.call_args[0][0] == primary_focs
        m2.remove_roles.assert_called_once_with(
            redundant_focs_1, reason="TARVeri Self-Healing: Remove duplicate role 'focs'"
        )

        # Verify guest_m was given primary guest
        guest_m.add_roles.assert_called_once()
        assert guest_m.add_roles.call_args[0][0] == primary_guest

        # Verify all 3 redundant roles were deleted
        redundant_focs_1.delete.assert_called_once()
        redundant_focs_2.delete.assert_called_once()
        redundant_guest.delete.assert_called_once()
        primary_focs.delete.assert_not_called()
        primary_guest.delete.assert_not_called()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_duplicate_roles_handles_hierarchy_and_forbidden_gracefully(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 998833
    guild.name = "Hierarchy Guild"

    primary_focs = MagicMock(spec=discord.Role)
    primary_focs.name = "FOCS"
    primary_focs.position = 10
    primary_focs.members = []
    primary_focs.managed = False
    primary_focs.is_default.return_value = False
    primary_focs.delete = AsyncMock()

    # Redundant role is above bot's top role
    high_focs = MagicMock(spec=discord.Role)
    high_focs.name = "focs-high"
    high_focs.position = 60
    high_focs.members = []
    high_focs.managed = False
    high_focs.is_default.return_value = False
    high_focs.delete = AsyncMock()

    guild.roles = [high_focs, primary_focs]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock()
    bot_top_role.position = 20  # Below high_focs (60)
    guild.me.top_role = bot_top_role

    db = Database(str(tmp_path / "hierarchy_dedup.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        stats = await service.reconcile_duplicate_roles(guild)

        # High role should NOT be deleted due to hierarchy
        high_focs.delete.assert_not_called()
        primary_focs.delete.assert_not_called()
        assert stats["deleted_roles"] == 0
        assert stats["failed"] >= 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_src_roles_never_matched_or_deleted_as_duplicates(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 112233
    guild.name = "SRC Protection Guild"

    # Regular FOCS role
    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 15
    focs_role.members = []
    focs_role.delete = AsyncMock()

    # FOCS SRC role (MUST NEVER be matched or deleted)
    focs_src_role = MagicMock(spec=discord.Role)
    focs_src_role.name = "FOCS SRC"
    focs_src_role.position = 12
    focs_src_role.members = []
    focs_src_role.delete = AsyncMock()

    # FOET Council role
    foet_council = MagicMock(spec=discord.Role)
    foet_council.name = "FOET Council"
    foet_council.position = 10
    foet_council.members = []
    foet_council.delete = AsyncMock()

    guild.roles = [focs_role, focs_src_role, foet_council]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock()
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role

    db = Database(str(tmp_path / "src_protection.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        # 1. Matching engine must NOT match FOCS SRC to FOCS
        matched = service._match_faculty_role_in_list([focs_src_role], "FOCS")
        assert matched is None

        # 2. Reconcile duplicate roles must NOT delete FOCS SRC or FOET Council
        stats = await service.reconcile_duplicate_roles(guild)
        assert stats["deleted_roles"] == 0
        focs_src_role.delete.assert_not_called()
        foet_council.delete.assert_not_called()
        focs_role.delete.assert_not_called()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restore_src_roles_creates_all_8_roles(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 554433
    guild.name = "SRC Restore Guild"

    # Server already has FOCS SRC
    existing_focs_src = MagicMock(spec=discord.Role)
    existing_focs_src.name = "FOCS SRC"
    guild.roles = [existing_focs_src]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    created_roles = []

    async def mock_create_role(name, colour, mentionable, reason):
        r = MagicMock(spec=discord.Role)
        r.name = name
        created_roles.append(r)
        return r

    guild.create_role = AsyncMock(side_effect=mock_create_role)

    db = Database(str(tmp_path / "src_restore.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        stats = await service.restore_src_roles(guild)

        assert stats["created"] == 7  # 8 total - 1 already existing
        assert stats["existing"] == 1
        assert stats["failed"] == 0
        assert guild.create_role.call_count == 7
    finally:
        await db.close()





