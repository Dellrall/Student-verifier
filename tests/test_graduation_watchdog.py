"""
Tests for GraduationWatchdogService.
Verifies periodic background monitoring, expiry alerts, cooldowns, and exception handling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from tarveri.config import hash_student_id
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.graduation_watchdog_service import GraduationWatchdogService
from tarveri.services.verification_service import VerificationService


@pytest.fixture
async def setup_env(tmp_path):
    db_path = str(tmp_path / "test_watchdog.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    bot.get_user = MagicMock()
    bot.fetch_user = AsyncMock()

    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_salt", rate_limiter)
    watchdog = GraduationWatchdogService(
        bot=bot,
        db=db,
        verification_service=service,
        interval_hours=24,
        prompt_cooldown_days=7,
    )

    yield bot, db, service, watchdog

    watchdog.stop()
    await db.close()


@pytest.mark.asyncio
async def test_watchdog_lifecycle(setup_env):
    _, _, _, watchdog = setup_env
    assert not watchdog._running
    assert watchdog._task is None

    watchdog.start()
    assert watchdog._running
    assert watchdog._task is not None

    # Redundant start
    task_ref = watchdog._task
    watchdog.start()
    assert watchdog._task is task_ref

    watchdog.stop()
    assert not watchdog._running
    assert watchdog._task is None


@pytest.mark.asyncio
async def test_watchdog_check_expired_students_success(setup_env):
    bot, db, service, watchdog = setup_env

    # Insert an expired student record
    user_id = 112233
    id_hash = hash_student_id("21WMD12345", "secret_salt")
    expired_date = "2024-05-31"

    await db.record_verification(
        discord_user_id=user_id,
        student_id_hash=id_hash,
        faculty_code="M",
        campus_code="W",
        level_code="D",
        card_expiry_date=expired_date,
    )

    mock_user = MagicMock(spec=discord.User)
    mock_user.id = user_id
    mock_user.mention = f"<@{user_id}>"
    mock_user.send = AsyncMock()
    bot.get_user.return_value = mock_user

    stats = await watchdog.check_expired_students_now()
    assert stats["total_expired"] == 1
    assert stats["prompted"] == 1
    assert stats["skipped_cooldown"] == 0
    assert stats["dm_blocked"] == 0

    mock_user.send.assert_called_once()
    embed = mock_user.send.call_args[1]["embed"]
    assert "TARUMT Student Card Expiry" in embed.title
    assert "05/24" in embed.description

    # Verify status in database
    details = await db.get_verification_details(user_id)
    assert details["lifecycle_prompt_status"] == "prompted"
    assert details["last_lifecycle_prompt_at"] is not None


@pytest.mark.asyncio
async def test_watchdog_respects_7day_cooldown(setup_env):
    bot, db, service, watchdog = setup_env

    user_id = 223344
    id_hash = hash_student_id("22WMR99999", "secret_salt")
    expired_date = "2024-10-31"

    recent_prompt = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")

    await db.record_verification(
        discord_user_id=user_id,
        student_id_hash=id_hash,
        faculty_code="M",
        campus_code="W",
        level_code="R",
        card_expiry_date=expired_date,
    )
    await db.update_verification_profile(
        discord_user_id=user_id,
        lifecycle_prompt_status="prompted",
        last_lifecycle_prompt_at=recent_prompt,
    )

    mock_user = MagicMock(spec=discord.User)
    mock_user.id = user_id
    mock_user.send = AsyncMock()
    bot.get_user.return_value = mock_user

    stats = await watchdog.check_expired_students_now()
    assert stats["total_expired"] == 1
    assert stats["skipped_cooldown"] == 1
    assert stats["prompted"] == 0
    mock_user.send.assert_not_called()


@pytest.mark.asyncio
async def test_watchdog_handles_dm_forbidden(setup_env):
    bot, db, service, watchdog = setup_env

    user_id = 334455
    id_hash = hash_student_id("20WMR88888", "secret_salt")
    expired_date = "2023-10-31"

    await db.record_verification(
        discord_user_id=user_id,
        student_id_hash=id_hash,
        faculty_code="F",
        campus_code="W",
        level_code="R",
        card_expiry_date=expired_date,
    )

    mock_user = MagicMock(spec=discord.User)
    mock_user.id = user_id
    mock_user.mention = f"<@{user_id}>"
    mock_user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "Cannot send messages to this user"))
    bot.get_user.return_value = mock_user

    stats = await watchdog.check_expired_students_now()
    assert stats["total_expired"] == 1
    assert stats["dm_blocked"] == 1
    assert stats["prompted"] == 0

    details = await db.get_verification_details(user_id)
    assert details["last_lifecycle_prompt_at"] is not None


@pytest.mark.asyncio
async def test_watchdog_handles_user_not_found(setup_env):
    bot, db, service, watchdog = setup_env

    user_id = 445566
    id_hash = hash_student_id("19WMR77777", "secret_salt")
    expired_date = "2022-10-31"

    await db.record_verification(
        discord_user_id=user_id,
        student_id_hash=id_hash,
        faculty_code="A",
        campus_code="W",
        level_code="R",
        card_expiry_date=expired_date,
    )

    bot.get_user.return_value = None
    bot.fetch_user.side_effect = discord.NotFound(MagicMock(), "Unknown User")

    stats = await watchdog.check_expired_students_now()
    assert stats["total_expired"] == 1
    assert stats["user_not_found"] == 1
    assert stats["prompted"] == 0
