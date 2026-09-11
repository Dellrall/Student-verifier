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
