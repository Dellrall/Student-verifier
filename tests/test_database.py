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
    assert settings == (123456, None, "Guest", None)

    # Set help channel
    await db.set_guild_help_channel(guild_id, 654321)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, 654321, "Guest", None)

    # Set guest role and review channel
    await db.set_guild_guest_role(guild_id, "Guest (Approved)")
    await db.set_guild_review_channel(guild_id, 999000)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, 654321, "Guest (Approved)", 999000)

    # Reset welcome channel
    await db.set_guild_welcome_channel(guild_id, None)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (None, 654321, "Guest (Approved)", 999000)

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

    await db.set_guild_guest_role(99999, "Legacy Guest")
    updated_settings = await db.get_guild_settings(99999)
    assert updated_settings[2] == "Legacy Guest"

    await db.close()



