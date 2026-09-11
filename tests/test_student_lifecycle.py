"""
Tests for Student Academic Lifecycle transitions, UI modals, views, and card expiry extensions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from tarveri.cogs.verification_cog import (
    ExtendExpiryModal,
    FurtherStudyTransitionModal,
    StudentLifecycleResolutionView,
    VerificationCog,
    VerificationModal,
)
from tarveri.config import (
    format_card_expiry_display,
    hash_student_id,
    parse_card_expiry_date,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService


def test_date_parsing_and_formatting():
    assert parse_card_expiry_date("10/26") == "2026-10-31"
    assert parse_card_expiry_date("02/24") == "2024-02-29"  # 2024 is leap year
    assert parse_card_expiry_date("02/25") == "2025-02-28"
    assert parse_card_expiry_date("2026-10-15") == "2026-10-15"
    assert parse_card_expiry_date("invalid") is None
    assert parse_card_expiry_date(None) is None

    assert format_card_expiry_display("2026-10-31") == "10/26"
    assert format_card_expiry_display("2024-02-29") == "02/24"
    assert format_card_expiry_display(None) is None


@pytest.mark.asyncio
async def test_extend_expiry_modal(tmp_path):
    db_path = str(tmp_path / "extend_modal.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = VerificationService(bot, db, "secret", RateLimiter())

    user_id = 123456
    id_hash = hash_student_id("23WMD09867", "secret")
    await db.record_verification(user_id, id_hash, "M", campus_code="W", level_code="D", card_expiry_date="2024-10-31")

    modal = ExtendExpiryModal(db, service)
    modal.expiry_date._value = "05/27"
    modal.note._value = "Extended internship semester"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.guild = MagicMock()
    interaction.guild.name = "Test Guild"
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.response.defer.assert_called_once()
    interaction.followup.send.assert_called_once()
    reply_text = interaction.followup.send.call_args[0][0]
    assert "Student Card Validity Extended" in reply_text
    assert "05/27" in reply_text

    # Check database
    details = await db.get_verification_details(user_id)
    assert details["card_expiry_date"] == "2027-05-31"
    assert details["lifecycle_prompt_status"] == "extended"

    await db.close()


@pytest.mark.asyncio
async def test_further_study_transition_modal(tmp_path):
    db_path = str(tmp_path / "further_study_modal.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    bot.guilds = []
    service = VerificationService(bot, db, "secret", RateLimiter())

    user_id = 998877
    old_hash = hash_student_id("22WMD00001", "secret")
    await db.record_verification(user_id, old_hash, "M", campus_code="W", level_code="D")

    modal = FurtherStudyTransitionModal(service)
    modal.student_id._value = "24WMR55555"
    modal.card_expiry._value = "10/28"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.guild = None
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.response.defer.assert_called_once()
    interaction.followup.send.assert_called_once()
    reply_text = interaction.followup.send.call_args[0][0]
    assert "Academic Level Progression Successful" in reply_text

    details = await db.get_verification_details(user_id)
    assert details["level_code"] == "R"
    assert details["card_expiry_date"] == "2028-10-31"

    transitions = await db.get_academic_transitions_for_user(user_id)
    assert len(transitions) == 1
    assert transitions[0]["from_level_code"] == "D"
    assert transitions[0]["to_level_code"] == "R"

    await db.close()


@pytest.mark.asyncio
async def test_student_lifecycle_resolution_view_buttons():
    service = MagicMock(spec=VerificationService)
    db = MagicMock(spec=Database)
    view = StudentLifecycleResolutionView(service, db)

    # Test Graduated Button Callback
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response.send_modal = AsyncMock()
    await view.children[0].callback(interaction)
    interaction.response.send_modal.assert_called_once()

    # Test Further Study Button Callback
    interaction.response.send_modal.reset_mock()
    await view.children[1].callback(interaction)
    interaction.response.send_modal.assert_called_once()

    # Test Extend Button Callback
    interaction.response.send_modal.reset_mock()
    await view.children[2].callback(interaction)
    interaction.response.send_modal.assert_called_once()


@pytest.mark.asyncio
async def test_on_message_expired_student_prompt(tmp_path):
    db_path = str(tmp_path / "on_msg_expired.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = VerificationService(bot, db, "secret", RateLimiter())
    cog = VerificationCog(bot, db, service, RateLimiter())

    user_id = 776655
    id_hash = hash_student_id("21WMR11111", "secret")
    await db.record_verification(
        user_id, id_hash, "M", campus_code="W", level_code="R", card_expiry_date="2024-05-31"
    )

    guild = MagicMock(spec=discord.Guild)
    guild.id = 111222
    guild.name = "Active Guild"

    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "general"
    channel.guild = guild

    author = MagicMock(spec=discord.Member)
    author.id = user_id
    author.bot = False
    author.mention = f"<@{user_id}>"
    author.send = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.channel = channel
    message.author = author
    message.content = "Hello everyone!"

    await cog.on_message(message)

    author.send.assert_called_once()
    call_kwargs = author.send.call_args[1]
    assert "embed" in call_kwargs
    assert "view" in call_kwargs
    embed = call_kwargs["embed"]
    assert "TARUMT Student Card Expiry" in embed.title

    # Second message from the same author should be throttled by in-memory cooldown
    author.send.reset_mock()
    await cog.on_message(message)
    author.send.assert_not_called()

    await db.close()


def test_estimate_student_card_expiry():
    from tarveri.config import estimate_student_card_expiry

    # Foundation: 1 yr -> May 31
    assert estimate_student_card_expiry("24WMF12345") == "2025-05-31"
    # Diploma: 2 yrs -> Oct 31
    assert estimate_student_card_expiry("23WMD09867") == "2025-10-31"
    # Degree: 3 yrs -> Oct 31
    assert estimate_student_card_expiry("24WMR12345") == "2027-10-31"
    # Postgrad: 2 yrs -> Oct 31
    assert estimate_student_card_expiry("23WMP00001") == "2025-10-31"
    # Invalid / short
    assert estimate_student_card_expiry("123") is None
    assert estimate_student_card_expiry(None) is None


@pytest.mark.asyncio
async def test_perform_verification_omitted_expiry_auto_estimates(tmp_path):
    db_path = str(tmp_path / "auto_estimate.db")
    db = Database(db_path)
    await db.connect()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 112233
    guild.name = "Test Campus"
    guild.roles = []
    guild.get_member = MagicMock(return_value=MagicMock())

    role_focs = MagicMock(spec=discord.Role)
    role_focs.name = "FOCS"
    role_kl = MagicMock(spec=discord.Role)
    role_kl.name = "KL Main Campus"
    role_deg = MagicMock(spec=discord.Role)
    role_deg.name = "Degree"

    guild.create_role = AsyncMock(side_effect=[role_focs, role_kl, role_deg])

    bot = MagicMock()
    bot.guilds = [guild]

    member = MagicMock(spec=discord.Member)
    member.id = 445566
    member.guild = guild
    member.roles = []
    member.add_roles = AsyncMock()
    guild.get_member.return_value = member

    service = VerificationService(bot, db, "secret", RateLimiter())

    # Verify student WITHOUT specifying raw_expiry_date (omitted / None)
    result = await service.perform_verification(
        user=member,
        raw_student_id="24WMR12345",
        raw_expiry_date=None,
    )
    assert "You've been given the following role(s)" in result

    # Check database: card_expiry_date should be automatically set to 2027-10-31
    details = await db.get_verification_details(member.id)
    assert details["card_expiry_date"] == "2027-10-31"
    assert details["level_code"] == "R"
    assert details["campus_code"] == "W"

    await db.close()


@pytest.mark.asyncio
async def test_database_startup_backfills_legacy_card_expiry(tmp_path):
    import aiosqlite

    db_path = str(tmp_path / "legacy_backfill.db")
    # Manually create legacy table with no card_expiry_date populated
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE verifications (
                discord_user_id INTEGER PRIMARY KEY,
                student_id_hash TEXT UNIQUE NOT NULL,
                faculty_code TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                campus_code TEXT,
                level_code TEXT,
                is_alumni INTEGER DEFAULT 0
            );
            """
        )
        await conn.execute(
            """
            INSERT INTO verifications (discord_user_id, student_id_hash, faculty_code, verified_at, campus_code, level_code, is_alumni)
            VALUES (101, 'hash1', 'M', '2023-06-15 10:00:00', 'W', 'D', 0),
                   (102, 'hash2', 'A', '2024-03-01 12:00:00', 'W', 'R', 0),
                   (103, 'hash3', 'M', '2022-01-01 00:00:00', 'W', 'R', 1);
            """
        )
        await conn.commit()

    # Now open with Database class, which runs connect() migrations and backfill
    db = Database(db_path)
    await db.connect()

    # User 101 (Diploma verified in 2023): should be backfilled to 2025-10-31
    d1 = await db.get_verification_details(101)
    assert d1["card_expiry_date"] == "2025-10-31"

    # User 102 (Degree verified in 2024): should be backfilled to 2027-10-31
    d2 = await db.get_verification_details(102)
    assert d2["card_expiry_date"] == "2027-10-31"

    # User 103 (Alumni): should remain None since is_alumni = 1
    d3 = await db.get_verification_details(103)
    assert d3["card_expiry_date"] is None

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_invalid_expiry_validation():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    service = VerificationService(bot, db, "secret", RateLimiter())

    member = MagicMock(spec=discord.Member)
    member.id = 12345

    # Passing an invalid expiry format string
    res = await service.perform_verification(
        user=member,
        raw_student_id="24WMR12345",
        raw_expiry_date="invalid_date",
    )
    assert "Invalid student card expiry date format" in res
    assert "MM/YY" in res


@pytest.mark.asyncio
async def test_student_verification_modal_in_guest_cog():
    from tarveri.cogs.guest_cog import StudentVerificationModal

    service = MagicMock(spec=VerificationService)
    service.perform_verification = AsyncMock(return_value="✅ Verified!")

    modal = StudentVerificationModal(service)
    modal.student_id._value = "23WMD09867"
    modal.card_expiry._value = "10/26"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.response.defer.assert_called_once()
    service.perform_verification.assert_called_once_with(
        interaction.user, "23WMD09867", raw_expiry_date="10/26"
    )
    interaction.followup.send.assert_called_once_with("✅ Verified!", ephemeral=True)


