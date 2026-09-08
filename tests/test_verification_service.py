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

    db = Database(str(tmp_path / "collision_test.db"))
    await db.connect()
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

    await db.close()

