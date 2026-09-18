from unittest.mock import MagicMock, patch

import pytest

from tarveri.config import Settings
from tarveri.database import Database
from tarveri.services.storage_guard_service import (
    StorageGuardService,
    StorageLimitExceededError,
)


@pytest.fixture
def mock_settings(tmp_path):
    db_file = tmp_path / "test.db"
    backup_dir = tmp_path / "backups"
    logs_dir = tmp_path / "logs"
    backup_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        db_path=str(db_file),
        backup_dir=str(backup_dir),
        logs_dir=str(logs_dir),
        max_storage_mb=10,  # 10 MB limit for test
        enable_storage_guard=True,
        storage_check_interval_hours=1,
    )


@pytest.mark.asyncio
async def test_storage_usage_under_limit(mock_settings, tmp_path):
    db = Database(mock_settings.db_path)
    await db.connect()
    try:
        service = StorageGuardService(db=db, settings=mock_settings)
        usage = service.get_storage_usage()

        assert usage.max_mb == 10.0
        assert usage.is_critical is False
        assert usage.is_warning is False

        check_res = await service.check_storage_now()
        assert check_res["status"] == "OK"
        assert check_res["self_healed"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_storage_usage_critical_triggers_sentry_and_self_healing(mock_settings, tmp_path):
    db = Database(mock_settings.db_path)
    await db.connect()

    # Create dummy large files in backup_dir to exceed 10MB limit
    large_backup = tmp_path / "backups" / "tarveri_backup_large.db"
    with open(large_backup, "wb") as f:
        f.seek(12 * 1024 * 1024 - 1)  # 12 MB
        f.write(b"\0")

    try:
        service = StorageGuardService(db=db, settings=mock_settings)
        usage = service.get_storage_usage()

        assert usage.total_mb >= 12.0
        assert usage.is_critical is True
        assert usage.is_warning is True

        with patch("sentry_sdk.capture_exception") as mock_sentry:
            with patch("sentry_sdk.push_scope") as mock_scope:
                mock_scope.return_value.__enter__.return_value = MagicMock()
                # Mock is_initialized to return True
                with patch("sentry_sdk.is_initialized", return_value=True):
                    check_res = await service.check_storage_now()

                    assert check_res["status"] == "CRITICAL"
                    assert check_res["self_healed"] is True
                    # Verify Sentry captured the exception
                    assert mock_sentry.called
                    args, _ = mock_sentry.call_args
                    assert isinstance(args[0], StorageLimitExceededError)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_storage_guard_lifecycle(mock_settings):
    db = Database(mock_settings.db_path)
    service = StorageGuardService(db=db, settings=mock_settings)

    service.start()
    assert service._running is True
    assert service._task is not None

    service.stop()
    assert service._running is False
    assert service._task is None
