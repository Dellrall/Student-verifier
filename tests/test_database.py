import os
import pytest
import sqlite3
import aiosqlite
from tarveri.database import Database


@pytest.mark.asyncio
async def test_database_crud(tmp_path):
    db_file = str(tmp_path / "test.db")
    db = Database(db_file)
    await db.connect()

    assert db.is_connected

    # Initial stats
    assert await db.total_verified() == 0

    # Insert verification
    user_id = 10001
    id_hash = "abcde12345hash"
    faculty = "M"
    await db.record_verification(user_id, id_hash, faculty)

    assert await db.total_verified() == 1

    # Fetch by user
    record = await db.get_verification_by_user(user_id)
    assert record is not None
    assert record[0] == id_hash
    assert record[1] == faculty

    # Fetch by id hash
    record_by_hash = await db.get_verification_by_id_hash(id_hash)
    assert record_by_hash is not None
    assert record_by_hash[0] == user_id

    # Faculty counts
    counts = await db.counts_by_faculty()
    assert len(counts) == 1
    assert counts[0] == ("M", 1)

    # Activity in last 24 hours
    assert await db.verified_in_last(24) == 1

    # Delete verification
    deleted = await db.delete_verification(user_id)
    assert deleted is True
    assert await db.total_verified() == 0

    # Logging test
    await db.log("INFO", "TEST_EVENT", "Test audit message", user_id=user_id)
    audit = await db.recent_audit(limit=10, event_type="TEST_EVENT")
    assert len(audit) == 1
    assert audit[0][2] == "TEST_EVENT"
    assert audit[0][4] == user_id
    assert audit[0][5] == "Test audit message"

    await db.close()
    assert not db.is_connected


@pytest.mark.asyncio
async def test_database_backup(tmp_path):
    db_file = str(tmp_path / "original.db")
    backup_dir = str(tmp_path / "backups")
    db = Database(db_file)
    await db.connect()

    await db.record_verification(2001, "hash2001", "M")
    backup_path = await db.create_backup(backup_dir=backup_dir)

    assert os.path.exists(backup_path)

    # Verify backup contains the record
    backup_db = Database(backup_path)
    await backup_db.connect()
    record = await backup_db.get_verification_by_user(2001)
    assert record is not None
    assert record[0] == "hash2001"

    await backup_db.close()
    await db.close()


@pytest.mark.asyncio
async def test_database_backup_rotation(tmp_path):
    from tarveri.database import rotate_backups
    db_file = str(tmp_path / "original_rot.db")
    backup_dir = str(tmp_path / "backups_rot")
    os.makedirs(backup_dir, exist_ok=True)

    db = Database(db_file)
    await db.connect()
    await db.record_verification(3001, "hash3001", "M")

    # Create 15 dummy backup files with timestamps in sequence
    created_files = []
    for i in range(15):
        b_file = os.path.join(backup_dir, f"tarveri_backup_20260908_{i:02d}0000.db")
        with open(b_file, "w") as f:
            f.write(f"backup content {i}")
        # Set artificial mtime so ordering is strictly preserved
        os.utime(b_file, (1700000000 + i * 100, 1700000000 + i * 100))
        created_files.append(b_file)

    assert len(os.listdir(backup_dir)) == 15

    # Run backup rotation with max_backups=10
    deleted = rotate_backups(backup_dir=backup_dir, max_backups=10)
    assert len(deleted) == 5

    remaining_files = sorted(os.listdir(backup_dir))
    assert len(remaining_files) == 10

    # The 5 oldest (index 00 to 04) should have been deleted
    for i in range(5):
        old_filename = f"tarveri_backup_20260908_{i:02d}0000.db"
        assert old_filename not in remaining_files
        assert not os.path.exists(os.path.join(backup_dir, old_filename))

    # The 10 newest (index 05 to 14) should remain
    for i in range(5, 15):
        new_filename = f"tarveri_backup_20260908_{i:02d}0000.db"
        assert new_filename in remaining_files
        assert os.path.exists(os.path.join(backup_dir, new_filename))

    # Also test create_backup() rotates automatically
    await db.create_backup(backup_dir=backup_dir, max_backups=10)
    # After creating 1 more snapshot with max_backups=10, the total count should still be 10
    assert len(os.listdir(backup_dir)) == 10

    await db.close()


@pytest.mark.asyncio
async def test_database_unique_constraints(tmp_path):
    db_file = str(tmp_path / "test_constraint.db")
    db = Database(db_file)
    await db.connect()

    await db.record_verification(1001, "hash_one", "M")

    # Duplicate user_id should raise IntegrityError
    with pytest.raises((sqlite3.IntegrityError, aiosqlite.IntegrityError)):
        await db.record_verification(1001, "hash_two", "G")

    # Duplicate hash should raise IntegrityError
    with pytest.raises((sqlite3.IntegrityError, aiosqlite.IntegrityError)):
        await db.record_verification(1002, "hash_one", "G")

    await db.close()


@pytest.mark.asyncio
async def test_database_guild_settings(tmp_path):
    db_file = str(tmp_path / "test_guild_settings.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 999111
    # Initially None
    assert await db.get_guild_settings(guild_id) is None

    # Set welcome channel
    await db.set_guild_welcome_channel(guild_id, 123456)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, None, "Guest", None, None)

    # Set help channel
    await db.set_guild_help_channel(guild_id, 654321)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, 654321, "Guest", None, None)

    # Set guest role, review channel, and admin role
    await db.set_guild_guest_role(guild_id, "Guest (Approved)")
    await db.set_guild_review_channel(guild_id, 999000)
    await db.set_guild_admin_role(guild_id, "Special Staff")
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, 654321, "Guest (Approved)", 999000, "Special Staff")

    # Reset welcome channel and admin role
    await db.set_guild_welcome_channel(guild_id, None)
    await db.set_guild_admin_role(guild_id, None)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (None, 654321, "Guest (Approved)", 999000, None)

    await db.close()


@pytest.mark.asyncio
async def test_guest_tickets_reason_giver_and_comments(tmp_path):
    db_file = str(tmp_path / "test_guest_comments.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 112233
    applicant_id = 5555
    referrer_id = 6666
    channel_id = 7777

    # 1. Create ticket with applicant reason
    ticket_id = await db.create_guest_ticket(
        guild_id=guild_id,
        applicant_id=applicant_id,
        referrer_id=referrer_id,
        channel_id=channel_id,
        referral_code="TAR-COMM1",
        reason="Attending TARUMT Hackathon 2026 as mentor",
    )
    assert ticket_id > 0

    ticket = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket["applicant_id"] == applicant_id
    assert ticket["ticket_seq"] == 1
    assert ticket["reason"] == "Attending TARUMT Hackathon 2026 as mentor"
    assert ticket["vouch_note"] is None
    assert ticket["vouched_by_id"] is None
    assert ticket["closed_by_admin_id"] is None
    assert ticket["close_reason"] is None

    # 2. Voucher submits comments/statement (Reason Giver: referrer_id)
    vouch_note = "Confirmed industry speaker and mentor for our team."
    await db.update_guest_ticket_vouch(ticket_id, vouch_note, vouched_by_id=referrer_id)

    ticket_vouched = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_vouched["vouch_note"] == vouch_note
    assert ticket_vouched["vouched_by_id"] == referrer_id
    assert ticket_vouched["vouched_at"] is not None

    # 3. Admin closes/approves ticket with comment (Reason Giver: admin_id)
    admin_id = 9999
    admin_comment = "Verified external mentor credentials."
    await db.close_guest_ticket(ticket_id, "APPROVED", closed_by_admin_id=admin_id, close_reason=admin_comment)

    ticket_approved = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_approved["status"] == "APPROVED"
    assert ticket_approved["closed_by_admin_id"] == admin_id
    assert ticket_approved["close_reason"] == admin_comment
    assert ticket_approved["closed_at"] is not None

    # 4. Another ticket: Admin rejection with reason/comment
    ticket_id_rej = await db.create_guest_ticket(
        guild_id=guild_id,
        applicant_id=8888,
        channel_id=9999,
        reason="Random guest",
    )
    rej_reason = "Unverified affiliation and unresponsive."
    await db.close_guest_ticket(ticket_id_rej, "REJECTED", closed_by_admin_id=admin_id, close_reason=rej_reason)

    ticket_rej = await db.get_guest_ticket_by_id(ticket_id_rej)
    assert ticket_rej["status"] == "REJECTED"
    assert ticket_rej["ticket_seq"] == 2
    assert ticket_rej["closed_by_admin_id"] == admin_id
    assert ticket_rej["close_reason"] == rej_reason

    # 5. Revocation reason when leaving server
    await db.revoke_guest_tickets_for_user(guild_id, applicant_id, status="LEFT_SERVER", close_reason="User left server")
    ticket_revoked = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_revoked["status"] == "LEFT_SERVER"
    assert ticket_revoked["close_reason"] == "User left server"

    await db.close()


@pytest.mark.asyncio
async def test_database_backwards_compatibility_migration(tmp_path):
    """Verifies that an existing database created with an older legacy schema migrates smoothly without losing data."""
    db_file = str(tmp_path / "legacy_v1.db")
    
    # Manually create a legacy v1 schema database with minimal columns
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute(
        """CREATE TABLE verifications (
            discord_user_id INTEGER PRIMARY KEY,
            student_id_hash TEXT UNIQUE NOT NULL,
            faculty_code TEXT NOT NULL,
            verified_at TEXT NOT NULL
        );"""
    )
    cursor.execute(
        """CREATE TABLE guild_settings (
            guild_id INTEGER PRIMARY KEY,
            welcome_channel_id INTEGER,
            help_channel_id INTEGER,
            updated_at TEXT NOT NULL
        );"""
    )
    cursor.execute(
        """CREATE TABLE guest_tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            applicant_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );"""
    )
    cursor.execute(
        "INSERT INTO verifications VALUES (11111, 'hash_legacy', 'M', '2025-01-01 10:00:00');"
    )
    cursor.execute(
        "INSERT INTO guild_settings VALUES (99999, 12345, 67890, '2025-01-01 10:00:00');"
    )
    cursor.execute(
        "INSERT INTO guest_tickets (guild_id, applicant_id, channel_id, created_at) VALUES (99999, 22222, 33333, '2025-01-01 10:00:00');"
    )
    conn.commit()
    conn.close()

    # Now open with TARVeri Database class
    db = Database(db_file)
    await db.connect()

    # Verify existing legacy data is intact
    verif = await db.get_verification_by_user(11111)
    assert verif is not None
    assert verif[0] == "hash_legacy"
    assert verif[1] == "M"

    settings = await db.get_guild_settings(99999)
    assert settings is not None
    assert settings[0] == 12345
    assert settings[1] == 67890
    # Newly added guest_role_name and review_channel_id default gracefully
    assert settings[2] == "Guest" or settings[2] is None

    ticket = await db.get_guest_ticket_by_id(1)
    assert ticket is not None
    assert ticket["applicant_id"] == 22222
    assert ticket["status"] == "OPEN"

    # Verify newly added columns can be written to
    await db.update_guest_ticket_vouch(1, "Backwards compatible vouch", vouched_by_id=55555)
    updated_ticket = await db.get_guest_ticket_by_id(1)
    assert updated_ticket["vouch_note"] == "Backwards compatible vouch"
    assert updated_ticket["vouched_by_id"] == 55555

    await db.close()


@pytest.mark.asyncio
async def test_database_closed_connection_errors():
    db = Database("unopened.db")
    assert not db.is_connected

    with pytest.raises(RuntimeError, match="Database connection is not open"):
        await db.get_verification_by_user(12345)

    with pytest.raises(RuntimeError, match="Database connection is not open"):
        await db.record_verification(12345, "hash", "M")

    with pytest.raises(RuntimeError, match="Database connection is not open"):
        await db.delete_verification(12345)

    with pytest.raises(RuntimeError, match="Database connection is not open"):
        await db.create_backup()


@pytest.mark.asyncio
async def test_database_context_manager(tmp_path):
    db_file = str(tmp_path / "ctx_test.db")
    async with Database(db_file) as db:
        assert db.is_connected
        await db.record_verification(123, "hash123", "M")
        assert await db.total_verified() == 1

    assert not db.is_connected


def test_rotate_backups_edge_cases(tmp_path):
    from tarveri.database import rotate_backups

    # Non-existent dir returns []
    assert rotate_backups(str(tmp_path / "non_existent"), max_backups=5) == []

    # max_backups <= 0 returns []
    empty_dir = str(tmp_path / "empty")
    os.makedirs(empty_dir, exist_ok=True)
    assert rotate_backups(empty_dir, max_backups=0) == []
    assert rotate_backups(empty_dir, max_backups=-1) == []


@pytest.mark.asyncio
async def test_database_cleanup_expired_referrals(tmp_path):
    db_file = str(tmp_path / "cleanup_test.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 111
    # Create 2 active referrals with past expiration date
    await db.create_referral_code("TAR-EXP1", guild_id, 101, "2020-01-01 00:00:00")
    await db.create_referral_code("TAR-EXP2", guild_id, 102, "2020-01-01 00:00:00")
    # Create 1 active referral with future expiration date
    await db.create_referral_code("TAR-ACT1", guild_id, 103, "2099-01-01 00:00:00")

    cleaned_count = await db.cleanup_expired_referrals()
    assert cleaned_count == 2

    exp1 = await db.get_referral_code("TAR-EXP1", guild_id)
    assert exp1["status"] == "EXPIRED"

    exp2 = await db.get_referral_code("TAR-EXP2", guild_id)
    assert exp2["status"] == "EXPIRED"

    act1 = await db.get_referral_code("TAR-ACT1", guild_id)
    assert act1["status"] == "ACTIVE"

    await db.close()


@pytest.mark.asyncio
async def test_database_clear_stale_channel_setting(tmp_path):
    db_file = str(tmp_path / "stale_channel_test.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 445566
    await db.set_guild_welcome_channel(guild_id, 1001)
    await db.set_guild_help_channel(guild_id, 1002)
    await db.set_guild_review_channel(guild_id, 1003)
    await db.set_guild_admin_role(guild_id, "Test Admin")

    settings = await db.get_guild_settings(guild_id)
    assert settings == (1001, 1002, "Guest", 1003, "Test Admin")

    # Clear welcome channel
    cleared_welcome = await db.clear_stale_channel_setting(guild_id, "welcome")
    assert cleared_welcome is True
    settings = await db.get_guild_settings(guild_id)
    assert settings[0] is None
    assert settings[1] == 1002

    # Clear help channel using column name
    cleared_help = await db.clear_stale_channel_setting(guild_id, "help_channel_id")
    assert cleared_help is True
    settings = await db.get_guild_settings(guild_id)
    assert settings[1] is None

    # Clear review channel
    cleared_review = await db.clear_stale_channel_setting(guild_id, "review")
    assert cleared_review is True
    settings = await db.get_guild_settings(guild_id)
    assert settings[3] is None

    # Clearing again returns False because setting is already NULL
    cleared_again = await db.clear_stale_channel_setting(guild_id, "review")
    assert cleared_again is False

    # Invalid setting raises ValueError
    with pytest.raises(ValueError, match="Invalid channel/setting type"):
        await db.clear_stale_channel_setting(guild_id, "invalid_setting")

    await db.close()


@pytest.mark.asyncio
async def test_database_get_all_verifications(tmp_path):
    db_file = str(tmp_path / "all_verif_test.db")
    db = Database(db_file)
    await db.connect()

    # Empty initially
    all_v = await db.get_all_verifications()
    assert all_v == []

    # Record 2 verifications
    await db.record_verification(1001, "hash_user_1", "M")
    await db.record_verification(1002, "hash_user_2", "B")

    all_v = await db.get_all_verifications()
    assert len(all_v) == 2
    u_ids = {row[0] for row in all_v}
    assert u_ids == {1001, 1002}
    faculties = {row[2] for row in all_v}
    assert faculties == {"M", "B"}

    await db.close()


@pytest.mark.asyncio
async def test_bot_created_roles_tracking(tmp_path):
    db_file = str(tmp_path / "bot_roles.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 998877
    assert await db.get_bot_created_role_ids(guild_id) == set()

    # Record 2 bot created roles
    await db.record_bot_created_role(guild_id, 1111, "FOCS")
    await db.record_bot_created_role(guild_id, 2222, "Guest(Approved)")
    # Record for another guild
    await db.record_bot_created_role(888888, 3333, "FAFB")

    role_ids = await db.get_bot_created_role_ids(guild_id)
    assert role_ids == {1111, 2222}

    # Delete one
    await db.delete_bot_created_role(1111)
    role_ids = await db.get_bot_created_role_ids(guild_id)
    assert role_ids == {2222}

    await db.close()


@pytest.mark.asyncio
async def test_database_backup_listing_and_restoration(tmp_path):
    orig_db_file = str(tmp_path / "active.db")
    backup_dir = str(tmp_path / "backups_test")
    db = Database(orig_db_file)
    await db.connect()

    guild_1 = 1001
    await db.set_guild_welcome_channel(guild_1, 5001)
    await db.set_guild_help_channel(guild_1, 5002)
    await db.set_guild_guest_role(guild_1, "Verified Guest")
    await db.set_guild_review_channel(guild_1, 5003)
    await db.set_guild_admin_role(guild_1, "TARVeri Admin")
    await db.record_verification(9001, "hash_9001", "M")

    # Create backup snapshot
    backup_path = await db.create_backup(backup_dir=backup_dir)
    assert os.path.isfile(backup_path)

    # Test list_backups
    backups = db.list_backups(backup_dir=backup_dir)
    assert len(backups) == 1
    assert backups[0]["path"] == backup_path
    assert backups[0]["size_bytes"] > 0

    # Simulate settings loss / corruption in active DB
    await db.clear_stale_channel_setting(guild_1, "welcome")
    await db.clear_stale_channel_setting(guild_1, "help")
    await db.set_guild_guest_role(guild_1, "Guest")
    corrupted = await db.get_guild_settings(guild_1)
    assert corrupted[0] is None  # welcome cleared
    assert corrupted[1] is None  # help cleared
    assert corrupted[2] == "Guest"

    # Restore settings from latest backup
    res = await db.restore_latest_guild_settings(guild_id=guild_1, backup_dir=backup_dir)
    assert res is not None
    assert res["restored_guilds"] == 1

    restored = await db.get_guild_settings(guild_1)
    assert restored == (5001, 5002, "Verified Guest", 5003, "TARVeri Admin")

    # Full database restore
    # Add new dummy data to active
    await db.record_verification(9002, "hash_9002", "B")
    assert await db.total_verified() == 2

    # Restore full DB from snapshot
    await db.restore_full_database(backup_path)
    assert db.is_connected
    assert await db.total_verified() == 1  # reverted to 1 verification in snapshot
    rec = await db.get_verification_by_user(9001)
    assert rec is not None
    await db.close()


@pytest.mark.asyncio
async def test_database_campus_and_level_columns_and_details(tmp_path):
    db_file = str(tmp_path / "campus_level_test.db")
    db = Database(db_file)
    await db.connect()
    try:
        user_id = 77701
        await db.record_verification(user_id, "hash_77701", "WM", campus_code="W", level_code="D")
        
        details = await db.get_verification_details(user_id)
        assert details is not None
        assert details["faculty_code"] == "WM"
        assert details["campus_code"] == "W"
        assert details["level_code"] == "D"
        assert details["is_alumni"] is False

        # Non-existent user
        assert await db.get_verification_details(999999) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_database_backfill_legacy_verifications(tmp_path):
    db_file = str(tmp_path / "backfill_test.db")
    
    # 1. Manually create legacy database with NULL campus_code
    async with aiosqlite.connect(db_file) as conn:
        await conn.execute(
            """CREATE TABLE verifications (
                discord_user_id INTEGER PRIMARY KEY,
                student_id_hash TEXT UNIQUE NOT NULL,
                faculty_code TEXT NOT NULL,
                verified_at TEXT NOT NULL
            );"""
        )
        await conn.execute(
            "INSERT INTO verifications VALUES (101, 'hash101', 'M', '2024-01-01 10:00:00');"
        )
        await conn.execute(
            "INSERT INTO verifications VALUES (102, 'hash102', 'B', '2024-01-02 11:00:00');"
        )
        await conn.commit()

    # 2. Connect Database (triggers automatic migration and backfill)
    db = Database(db_file)
    await db.connect()
    try:
        # Check that legacy rows were backfilled with 'W' (KL Main Campus)
        det101 = await db.get_verification_details(101)
        assert det101 is not None
        assert det101["campus_code"] == "W"

        det102 = await db.get_verification_details(102)
        assert det102 is not None
        assert det102["campus_code"] == "W"

        # Test manual update_verification_details
        res = await db.update_verification_details(101, campus_code="P", level_code="R")
        assert res is True
        updated101 = await db.get_verification_details(101)
        assert updated101["campus_code"] == "P"
        assert updated101["level_code"] == "R"

        # Update non-existent returns False
        assert await db.update_verification_details(99999, campus_code="A") is False
    finally:
        await db.close()


