import pytest
from unittest.mock import AsyncMock, MagicMock
import discord
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
