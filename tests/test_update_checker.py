import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tarveri.services.update_checker import UpdateCheckerService
from tarveri.database import Database


@pytest.mark.asyncio
async def test_update_checker_no_update():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    db.is_connected = True
    db.log = AsyncMock()

    checker = UpdateCheckerService(bot, db, hoster_discord_id=None)

    with patch("subprocess.run") as mock_run:
        # Mock git returns same hash for HEAD and upstream
        mock_run.side_effect = [
            MagicMock(returncode=0),  # fetch
            MagicMock(stdout="commit123\n", returncode=0),  # HEAD
            MagicMock(stdout="origin/main\n", returncode=0),  # upstream branch
            MagicMock(stdout="commit123\n", returncode=0),  # remote HEAD
        ]

        is_avail, count, local_h, remote_h = await checker.check_for_updates()
        assert is_avail is False
        assert count == 0


@pytest.mark.asyncio
async def test_update_checker_update_available():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    db.is_connected = True
    db.log = AsyncMock()

    hoster_user = AsyncMock()
    bot.get_user.return_value = hoster_user

    checker = UpdateCheckerService(bot, db, hoster_discord_id=987654321)

    with patch("subprocess.run") as mock_run:
        # Mock git returns 2 commits ahead
        mock_run.side_effect = [
            MagicMock(returncode=0),  # fetch
            MagicMock(stdout="oldcommit123\n", returncode=0),  # HEAD
            MagicMock(stdout="origin/main\n", returncode=0),  # upstream branch
            MagicMock(stdout="newcommit456\n", returncode=0),  # remote HEAD
            MagicMock(stdout="2\n", returncode=0),  # count
        ]

        is_avail, count, local_h, remote_h = await checker.check_for_updates()
        assert is_avail is True
        assert count == 2
        assert local_h == "oldcommit123"
        assert remote_h == "newcommit456"

        # Check that DB log and DM were triggered
        db.log.assert_awaited_once()
        hoster_user.send.assert_awaited_once()
