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
