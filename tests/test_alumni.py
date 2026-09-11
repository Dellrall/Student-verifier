from __future__ import annotations

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import discord
from PIL import Image
import pytest

from tarveri.cogs.admin_cog import AdminCog
from tarveri.cogs.verification_cog import AlumniClaimModal, VerificationCog
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.card_service import CardService
from tarveri.services.verification_service import VerificationService


@pytest.mark.asyncio
async def test_database_alumni_crud_and_counts(tmp_path):
    db = Database(str(tmp_path / "alumni_test.db"))
    await db.connect()
    try:
        user_id = 987654321
        await db.record_verification(user_id, "student_hash_abc", "M")

        # 1. Initially not alumni
        assert await db.count_alumni() == 0
        assert await db.get_alumni_info_by_user(user_id) is None
        assert await db.get_all_alumni_user_ids() == []

        # 2. Record alumni claim
        claimed = await db.record_alumni_claim(user_id, 2025, "Bachelor of Software Engineering (Honours)")
        assert claimed is True
        assert await db.count_alumni() == 1

        info = await db.get_alumni_info_by_user(user_id)
        assert info is not None
        assert info["is_alumni"] is True
        assert info["graduated_year"] == 2025
        assert info["programme"] == "Bachelor of Software Engineering (Honours)"
        assert info["faculty_code"] == "M"

        all_ids = await db.get_all_alumni_user_ids()
        assert all_ids == [user_id]

        # 3. Revoke alumni status
        revoked = await db.revoke_alumni_status(user_id)
        assert revoked is True
        assert await db.count_alumni() == 0
        assert await db.get_alumni_info_by_user(user_id) is None
        assert await db.get_all_alumni_user_ids() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_verification_service_claim_and_revoke_alumni(tmp_path):
    db = Database(str(tmp_path / "alumni_service_test.db"))
    await db.connect()
    try:
        bot = MagicMock(spec=discord.Client)
        bot.guilds = []
        rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
        service = VerificationService(bot=bot, db=db, secret="test_secret", rate_limiter=rate_limiter)

        user_id = 1122334455
        await db.record_verification(user_id, "hash_1122", "M")

        guild = MagicMock(spec=discord.Guild)
        guild.id = 778899
        guild.name = "TARUMT Hub"
        guild.roles = []
        guild.chunked = True

        alumni_role = MagicMock(spec=discord.Role)
        alumni_role.id = 998811
        alumni_role.name = "TARUMT Alumni"
        alumni_role.position = 5

        bot_top_role = MagicMock(spec=discord.Role)
        bot_top_role.position = 10

        bot_member = MagicMock(spec=discord.Member)
        bot_member.guild_permissions.manage_roles = True
        bot_member.top_role = bot_top_role
        guild.me = bot_member

        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.display_name = "Alex Graduate"
        member.roles = []
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()

        guild.get_member = MagicMock(return_value=member)
        guild.fetch_member = AsyncMock(return_value=member)
        guild.create_role = AsyncMock(return_value=alumni_role)
        bot.guilds = [guild]

        # 1. Claim alumni status
        res = await service.claim_alumni_status(
            user_id=user_id,
            user_display_name="Alex Graduate",
            graduated_year=2025,
            programme="Bachelor of Software Engineering",
            current_guild=guild,
        )

        assert res["success"] is True
        assert res["graduated_year"] == 2025
        assert res["faculty_name"] == "FOCS"
        member.add_roles.assert_called_once()

        # Verify DB reflects alumni status
        info = await db.get_alumni_info_by_user(user_id)
        assert info is not None
        assert info["is_alumni"] is True

        # 2. Revoke alumni status
        guild.roles = [alumni_role]
        guild.fetch_roles = AsyncMock(return_value=[alumni_role])
        admin = MagicMock(spec=discord.Member)
        admin.name = "AdminUser"
        member.roles = [alumni_role]

        rev_res = await service.revoke_alumni_status(
            target_user=member,
            admin=admin,
            current_guild=guild,
            reason="Incorrect year",
        )
        assert rev_res["success"] is True
        member.remove_roles.assert_called_once()

        assert await db.get_alumni_info_by_user(user_id) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_alumni_members(tmp_path):
    db = Database(str(tmp_path / "reconcile_alumni_test.db"))
    await db.connect()
    try:
        bot = MagicMock(spec=discord.Client)
        bot.guilds = []
        rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
        service = VerificationService(bot=bot, db=db, secret="test_secret", rate_limiter=rate_limiter)

        user_id = 55667788
        await db.record_verification(user_id, "hash_5566", "B")
        await db.record_alumni_claim(user_id, 2024, "Bachelor of Finance")

        guild = MagicMock(spec=discord.Guild)
        guild.id = 665544
        guild.name = "FAFB Campus"
        guild.roles = []
        guild.chunked = True

        alumni_role = MagicMock(spec=discord.Role)
        alumni_role.id = 110022
        alumni_role.name = "TARUMT Alumni"
        alumni_role.position = 4
        guild.roles = [alumni_role]

        bot_top_role = MagicMock(spec=discord.Role)
        bot_top_role.position = 10

        bot_member = MagicMock(spec=discord.Member)
        bot_member.guild_permissions.manage_roles = True
        bot_member.top_role = bot_top_role
        guild.me = bot_member

        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.roles = []  # Missing alumni role
        member.add_roles = AsyncMock()

        guild.get_member = MagicMock(return_value=member)
        guild.fetch_member = AsyncMock(return_value=member)
        bot.guilds = [guild]

        stats = await service.reconcile_alumni_members(guild)
        assert stats["checked"] == 1
        assert stats["restored"] == 1
        assert stats["failed"] == 0
        member.add_roles.assert_called_once_with(
            alumni_role,
            reason="TARVeri: Self-healing automatic Alumni role restoration",
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_card_service_alumni_badge_and_cohort(tmp_path):
    db = Database(str(tmp_path / "card_alumni_test.db"))
    await db.connect()
    try:
        service = CardService(db=db, admin_role_name="TARVeri Admin")
        user_id = 99887766
        await db.record_verification(user_id, "alumni_hash_9988", "M")
        await db.record_alumni_claim(user_id, 2025, "Bachelor of Computer Science")

        guild = MagicMock(spec=discord.Guild)
        guild.id = 334455
        guild.name = "TARUMT Main Server"

        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.display_name = "Graduate Student"
        member.name = "grad_student"
        member.guild_permissions.administrator = False
        member.roles = []
        member.premium_since = None
        member.joined_at = datetime(2023, 1, 1, tzinfo=timezone.utc)

        card_data = await service.get_user_card_data(guild, member)

        assert card_data["is_alumni"] is True
        assert card_data["graduated_year"] == 2025
        assert card_data["cohort_str"] == "Class of 2025 • FOCS Alumni"
        assert "❖ ALUMNI" in card_data["badges"]
        assert "✦ FOCS" in card_data["badges"]
        assert "✓ VERIFIED" in card_data["badges"]

        # Render card image
        buf = await service.render_card_from_data(card_data, None)
        assert isinstance(buf, io.BytesIO)
        img = Image.open(buf)
        assert img.size == (920, 530)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_graduate_slash_command_and_modal(tmp_path):
    db = Database(str(tmp_path / "graduate_slash_test.db"))
    await db.connect()
    try:
        bot = MagicMock()
        bot.settings = None
        bot.guest_service = None
        rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
        service = VerificationService(bot=bot, db=db, secret="test_secret", rate_limiter=rate_limiter)
        cog = VerificationCog(bot=bot, db=db, service=service, rate_limiter=rate_limiter)

        user_id = 88776655
        user = MagicMock(spec=discord.Member)
        user.id = user_id
        user.display_name = "Unverified Candidate"
        user.roles = []

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = user
        interaction.guild = MagicMock(spec=discord.Guild)
        interaction.guild.id = 112233
        interaction.guild.roles = []
        interaction.response.send_message = AsyncMock()
        interaction.response.send_modal = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        # 1. Unverified student runs /graduate -> blocked
        await cog.graduate_slash.callback(cog, interaction, year=2025, programme=None)
        interaction.response.send_message.assert_called_once()
        assert "You must be a verified TARUMT student" in interaction.response.send_message.call_args[0][0]

        # 2. Verified student runs /graduate without args -> opens modal
        await db.record_verification(user_id, "test_hash_8877", "M")
        interaction.response.send_message.reset_mock()
        await cog.graduate_slash.callback(cog, interaction, year=None, programme=None)
        interaction.response.send_modal.assert_called_once()
        modal = interaction.response.send_modal.call_args[0][0]
        assert isinstance(modal, AlumniClaimModal)

        # 3. Verified student runs /graduate with direct args -> succeeds
        interaction.response.send_modal.reset_mock()
        await cog.graduate_slash.callback(cog, interaction, year=2025, programme="Bachelor of IT")
        interaction.response.defer.assert_called_once_with(ephemeral=False, thinking=True)
        interaction.followup.send.assert_called_once()
        call_kwargs = interaction.followup.send.call_args[1]
        assert "embed" in call_kwargs
        assert call_kwargs["ephemeral"] is False

        # Verify DB is updated
        info = await db.get_alumni_info_by_user(user_id)
        assert info is not None
        assert info["is_alumni"] is True
        assert info["graduated_year"] == 2025
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_admin_alumni_revoke_command(tmp_path):
    db = Database(str(tmp_path / "admin_alumni_revoke_test.db"))
    await db.connect()
    try:
        bot = MagicMock()
        rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
        service = VerificationService(bot=bot, db=db, secret="test_secret", rate_limiter=rate_limiter)
        cog = AdminCog(
            bot=bot,
            db=db,
            service=service,
            rate_limiter=rate_limiter,
            admin_role_name="TARVeri Admin",
        )

        user_id = 44332211
        await db.record_verification(user_id, "hash_4433", "M")
        await db.record_alumni_claim(user_id, 2025, "Software Engineering")

        target_user = MagicMock(spec=discord.Member)
        target_user.id = user_id
        target_user.mention = "<@44332211>"
        target_user.roles = []

        admin_user = MagicMock(spec=discord.Member)
        admin_user.id = 990099
        admin_user.guild_permissions.administrator = True
        admin_user.roles = []

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = admin_user
        interaction.guild = MagicMock(spec=discord.Guild)
        interaction.guild.id = 556677
        interaction.guild.roles = []
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        # Run /alumni_revoke
        await cog.alumni_revoke.callback(cog, interaction, user=target_user, reason="Revocation test")
        interaction.response.defer.assert_called_once_with(ephemeral=True)
        interaction.followup.send.assert_called_once()
        assert "Successfully revoked Alumni status" in interaction.followup.send.call_args[0][0]

        # Verify DB is updated
        assert await db.get_alumni_info_by_user(user_id) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_existing_alumni_role_discovery_and_reuse(tmp_path):
    """
    Verifies that if a guild already has an existing alumni role named 'Alumni', '[TARUMT] Alumni',
    or 'Graduated', TARVeri finds and uses the existing role without calling guild.create_role.
    """
    db = Database(str(tmp_path / "existing_alumni_role_test.db"))
    await db.connect()
    try:
        bot = MagicMock(spec=discord.Client)
        service = VerificationService(bot=bot, db=db, secret="test_secret", rate_limiter=RateLimiter())

        user_id = 987654321
        await db.record_verification(user_id, "hash_9876", "M")

        # Guild with existing custom alumni role "Alumni"
        existing_role = MagicMock(spec=discord.Role)
        existing_role.id = 554433
        existing_role.name = "Alumni"
        existing_role.position = 5

        guild = MagicMock(spec=discord.Guild)
        guild.id = 12345
        guild.name = "Existing Role Guild"
        guild.roles = [existing_role]
        guild.create_role = AsyncMock()

        bot_top_role = MagicMock(spec=discord.Role)
        bot_top_role.position = 10
        bot_member = MagicMock(spec=discord.Member)
        bot_member.guild_permissions.manage_roles = True
        bot_member.top_role = bot_top_role
        guild.me = bot_member

        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.display_name = "Jane Graduate"
        member.roles = []
        member.add_roles = AsyncMock()
        guild.get_member = MagicMock(return_value=member)
        guild.fetch_member = AsyncMock(return_value=member)
        bot.guilds = [guild]

        # Claim alumni status
        res = await service.claim_alumni_status(
            user_id=user_id,
            user_display_name="Jane Graduate",
            graduated_year=2024,
            programme="Information Systems",
            current_guild=guild,
        )

        assert res["success"] is True
        # Verify the existing role was assigned and NO new role was created
        guild.create_role.assert_not_called()
        member.add_roles.assert_called_once_with(
            existing_role,
            reason="TARVeri: Claimed Alumni status (Class of 2024)",
        )

        # Reconcile also uses existing role without creating
        reconcile_res = await service.reconcile_alumni_members(guild)
        assert reconcile_res["checked"] == 1
        guild.create_role.assert_not_called()
    finally:
        await db.close()
