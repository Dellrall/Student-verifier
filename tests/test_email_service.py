"""
Unit and integration tests for EmailService, AES-256 email encryption, OTP lifecycle, and database persistence.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from cryptography.fernet import Fernet

from tarveri.config import (
    Settings,
    decrypt_email,
    encrypt_email,
    hash_email,
    is_valid_student_email,
    mask_email,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.email_service import EmailService
from tarveri.services.verification_service import VerificationService
from tarveri.cogs.verification_cog import (
    OtpVerificationPromptView,
    StudentOtpModal,
    VerificationModal,
)


def test_email_crypto_helpers():
    key = Fernet.generate_key().decode()
    email = "23WMD09867@student.tarc.edu.my"

    encrypted = encrypt_email(email, key)
    assert encrypted != email
    decrypted = decrypt_email(encrypted, key)
    assert decrypted == email.lower()

    # Masking test
    masked = mask_email(email)
    assert masked.endswith("@student.tarc.edu.my")
    assert "***" in masked

    # Blind index hash
    secret = "test-secret-pepper"
    h1 = hash_email(email, secret)
    h2 = hash_email("  23WMD09867@STUDENT.TARC.EDU.MY  ", secret)
    assert h1 == h2

    # Validation
    assert is_valid_student_email("23wmd09867@student.tarc.edu.my") is True
    assert is_valid_student_email("staff@tarc.edu.my") is True
    assert is_valid_student_email("imposter@gmail.com") is False
    assert is_valid_student_email("invalid-email") is False


@pytest.mark.asyncio
async def test_email_service_otp_lifecycle():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        email_otp_ttl_seconds=300,
        email_otp_max_attempts=3,
        email_otp_resend_cooldown_seconds=5,
    )
    svc = EmailService(settings, mock_smtp=True)

    user_id = 998877
    student_id = "24WMD01234"
    email = "24wmd01234@student.tarc.edu.my"

    # 1. Generate & send OTP
    res = await svc.generate_and_send_otp(user_id, student_id, email, server_name="TARUMT Main")
    assert res["success"] is True
    assert len(svc.sent_emails) == 1
    sent_otp = svc.sent_emails[0]["otp"]
    assert len(sent_otp) == 6

    # 2. Resend cooldown test
    res_cooldown = await svc.generate_and_send_otp(user_id, student_id, email, server_name="TARUMT Main")
    assert res_cooldown["success"] is False
    assert "Please wait" in res_cooldown["error"]

    # 3. Invalid OTP attempt
    bad_res = await svc.verify_otp(user_id, "000000")
    assert bad_res["success"] is False
    assert "Incorrect verification code" in bad_res["error"]

    # 4. Valid OTP attempt
    good_res = await svc.verify_otp(user_id, sent_otp)
    assert good_res["success"] is True
    assert good_res["pending"].student_id == student_id
    assert good_res["pending"].email == email

    # 5. Verify cannot reuse used OTP
    reuse_res = await svc.verify_otp(user_id, sent_otp)
    assert reuse_res["success"] is False


@pytest.mark.asyncio
async def test_email_service_otp_max_attempts():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        email_otp_max_attempts=2,
    )
    svc = EmailService(settings, mock_smtp=True)
    user_id = 112233

    await svc.generate_and_send_otp(user_id, "24WMD11111", "24wmd11111@student.tarc.edu.my", server_name="Server")

    # Attempt 1 failed
    res1 = await svc.verify_otp(user_id, "999999")
    assert res1["success"] is False
    assert "1 attempt remaining" in res1["error"]

    # Attempt 2 failed (should invalidate)
    res2 = await svc.verify_otp(user_id, "999999")
    assert res2["success"] is False
    assert "invalidated" in res2["error"].lower()

    # Attempt 3: No active code
    res3 = await svc.verify_otp(user_id, "999999")
    assert res3["success"] is False
    assert "No pending verification code" in res3["error"]


@pytest.mark.asyncio
async def test_database_email_storage_and_duplicate_detection(tmp_path):
    db_path = str(tmp_path / "email_test.db")
    db = Database(db_path)
    await db.connect()

    key = Fernet.generate_key().decode()
    secret = "secret-pepper"
    email = "24wmd00001@student.tarc.edu.my"
    encrypted = encrypt_email(email, key)
    email_hash = hash_email(email, secret)

    await db.record_verification(
        discord_user_id=1001,
        student_id_hash="hash-001",
        faculty_code="FOCS",
        campus_code="W",
        level_code="R",
        card_expiry_date="2026-10-31",
        student_email_encrypted=encrypted,
        student_email_hash=email_hash,
    )

    details = await db.get_verification_details(1001)
    assert details is not None
    assert details["student_email_encrypted"] == encrypted
    assert details["student_email_hash"] == email_hash
    assert decrypt_email(details["student_email_encrypted"], key) == email

    # Lookup by email hash
    match = await db.get_verification_by_email_hash(email_hash)
    assert match is not None
    assert match[0] == 1001

    await db.close()


@pytest.mark.asyncio
async def test_verification_service_with_email_duplicate_guard(tmp_path):
    db_path = str(tmp_path / "veri_email.db")
    db = Database(db_path)
    await db.connect()

    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    email_svc = EmailService(settings, mock_smtp=True)
    rate_limiter = RateLimiter()
    bot = MagicMock()
    service = VerificationService(
        bot=bot,
        db=db,
        secret="fake_secret",
        rate_limiter=rate_limiter,
        settings=settings,
        email_service=email_svc,
    )

    user1 = MagicMock(spec=discord.Member)
    user1.id = 5001
    user1.name = "Student1"
    user1.mention = "<@5001>"
    user1.roles = []
    guild1 = MagicMock(spec=discord.Guild)
    guild1.id = 100
    guild1.name = "TARUMT Hub"
    guild1.roles = []
    user1.guild = guild1

    # First user verifies with email
    with patch.object(service, "get_mutual_guilds_for_user", AsyncMock(return_value=[guild1])):
        with patch.object(service, "assign_role_across_guilds") as mock_assign:
            mock_assign.return_value = MagicMock(verified_in=[(100, "TARUMT Hub", "FOCS")], already_had_role_in=[], missing_role_in=[], failed_in=[])
            res1 = await service.perform_verification(
                user1,
                "23WMD01111",
                raw_email="23wmd01111@student.tarc.edu.my",
            )
            assert "FOCS" in res1

    # Second user tries to verify with the same student email
    user2 = MagicMock(spec=discord.Member)
    user2.id = 5002
    user2.name = "Student2"
    user2.mention = "<@5002>"
    user2.roles = []
    user2.guild = guild1

    res2 = await service.perform_verification(
        user2,
        "23WMD02222",
        raw_email="23wmd01111@student.tarc.edu.my",
    )
    assert "already been used" in res2

    await db.close()


@pytest.mark.asyncio
async def test_student_otp_modal_submission(tmp_path):
    db_path = str(tmp_path / "modal_email.db")
    db = Database(db_path)
    await db.connect()

    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    email_svc = EmailService(settings, mock_smtp=True)
    rate_limiter = RateLimiter()
    bot = MagicMock()
    service = VerificationService(
        bot=bot,
        db=db,
        secret="fake_secret",
        rate_limiter=rate_limiter,
        settings=settings,
        email_service=email_svc,
    )

    user_id = 7001
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.mention = "<@7001>"
    user.roles = []
    guild = MagicMock(spec=discord.Guild)
    guild.id = 200
    guild.name = "TARUMT Campus"
    guild.roles = []
    user.guild = guild

    # Generate OTP
    res_otp = await email_svc.generate_and_send_otp(user_id, "24WMD08888", "24wmd08888@student.tarc.edu.my", server_name="TARUMT Campus")
    assert res_otp["success"] is True
    otp = email_svc.sent_emails[0]["otp"]

    modal = StudentOtpModal(service=service, email_service=email_svc)
    modal.otp_code._value = otp

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user
    interaction.guild = guild
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    with patch.object(service, "get_mutual_guilds_for_user", AsyncMock(return_value=[guild])):
        with patch.object(service, "assign_role_across_guilds") as mock_assign:
            mock_assign.return_value = MagicMock(verified_in=[(200, "TARUMT Campus", "FOCS")], already_had_role_in=[], missing_role_in=[], failed_in=[])
            await modal.on_submit(interaction)

    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "Institutional Email & Student Verified" in embed.title

    # Verify DB record has encrypted email
    details = await db.get_verification_details(user_id)
    assert details is not None
    assert decrypt_email(details["student_email_encrypted"], key) == "24wmd08888@student.tarc.edu.my"

    await db.close()


def test_email_service_smtp_fallback_on_primary_failure():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        smtp_host="mail.smtp2go.com",
        smtp_port=587,
        smtp_user="smtp2go_user",
        smtp_password="smtp2go_password",
        smtp_fallback_host="mail.direct-domain.com",
        smtp_fallback_port=587,
        smtp_fallback_user="verify@direct-domain.com",
        smtp_fallback_password="mailbox_password",
    )
    svc = EmailService(settings, mock_smtp=False)

    def mock_endpoint(to_email, otp_code, server_name, ttl_minutes, host, port, user, password, from_email, from_name, use_tls, relay_label="SMTP"):
        if host == "mail.smtp2go.com":
            # Simulate SMTP2GO limit / quota exhausted error
            return False, "550 5.7.1 Daily message sending limit exceeded on SMTP2GO relay"
        if host == "mail.direct-domain.com":
            # Fallback direct SMTP succeeds
            return True, None
        return False, "Unknown host"

    with patch.object(svc, "_send_to_smtp_endpoint", side_effect=mock_endpoint) as mock_send:
        success = svc._send_smtp_sync(
            to_email="24wmd12345@student.tarc.edu.my",
            otp_code="123456",
            server_name="Test Server",
            ttl_minutes=10,
        )
        assert success is True
        assert mock_send.call_count == 2
        # First call was primary
        assert mock_send.call_args_list[0].kwargs["host"] == "mail.smtp2go.com"
        # Second call was fallback
        assert mock_send.call_args_list[1].kwargs["host"] == "mail.direct-domain.com"


def test_email_service_smtp_both_fail():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        smtp_host="mail.smtp2go.com",
        smtp_fallback_host="mail.direct-domain.com",
    )
    svc = EmailService(settings, mock_smtp=False)

    with patch.object(svc, "_send_to_smtp_endpoint", return_value=(False, "Connection timeout")):
        success = svc._send_smtp_sync(
            to_email="24wmd12345@student.tarc.edu.my",
            otp_code="123456",
            server_name="Test Server",
            ttl_minutes=10,
        )
        assert success is False


def test_email_service_primary_fails_without_fallback():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        smtp_host="mail.smtp2go.com",
        smtp_fallback_host="",  # No fallback configured
    )
    svc = EmailService(settings, mock_smtp=False)

    with patch.object(svc, "_send_to_smtp_endpoint", return_value=(False, "550 Limit Exceeded")):
        success = svc._send_smtp_sync(
            to_email="24wmd12345@student.tarc.edu.my",
            otp_code="123456",
            server_name="Test Server",
            ttl_minutes=10,
        )
        assert success is False


@pytest.mark.asyncio
async def test_verification_modal_guild_opt_in_and_opt_out(tmp_path):
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    db = Database(str(tmp_path / "guild_opt_in.db"))
    await db.connect()

    bot = MagicMock()
    service = VerificationService(bot, db, "secret", RateLimiter(), email_service=EmailService(settings, mock_smtp=True))
    email_service = service.email_service

    guild_id = 999888
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "Opt In Guild"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 777111
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Case 1: Guild is OPTED-OUT (default) -> email is optional, omitting email succeeds directly
    modal_opt_out = VerificationModal(service, email_service, require_email=False)
    assert modal_opt_out.student_email.required is False
    modal_opt_out.student_id._value = "24WMD05555"
    modal_opt_out.student_email._value = ""

    with patch.object(service, "perform_verification", return_value="✅ Verified!"):
        await modal_opt_out.on_submit(interaction)
        interaction.followup.send.assert_called_once_with("✅ Verified!", ephemeral=True)

    # Case 2: Guild OPTS-IN -> require_email = True, omitting email is blocked
    await db.set_guild_email_verification(guild_id, True)
    assert await db.is_guild_email_verification_enabled(guild_id) is True

    modal_opt_in = VerificationModal(service, email_service, require_email=True)
    assert modal_opt_in.student_email.required is True
    modal_opt_in.student_id._value = "24WMD05555"
    modal_opt_in.student_email._value = ""

    interaction.followup.send.reset_mock()
    await modal_opt_in.on_submit(interaction)
    interaction.followup.send.assert_called_once()
    assert "Institutional student email is required" in interaction.followup.send.call_args[0][0]

    # Case 3: Guild is OPTED-IN and valid email is provided -> OTP dispatched
    modal_opt_in.student_email._value = "24wmd05555@student.tarc.edu.my"
    interaction.followup.send.reset_mock()
    await modal_opt_in.on_submit(interaction)
    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "Verification Code Sent!" in embed.title

    await db.close()


