import asyncio
import os
import signal
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord

from tarveri.config import Settings
from tarveri.database import Database
from tarveri.services.outage_service import OutageService, DEFAULT_PROBE_TARGETS


@pytest.mark.asyncio
async def test_outage_service_initialization(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "outage_init.db"))
    await db.connect()

    service = OutageService(bot, db, timeout_seconds=300, probe_interval=15, alert_grace_seconds=20)
    assert service.timeout_seconds == 300
    assert service.probe_interval == 15
    assert service.alert_grace_seconds == 20
    assert service.probe_targets == DEFAULT_PROBE_TARGETS
    assert not service.is_running
    assert not service.is_outage_active
    assert not service.alert_logged
    assert service.disconnected_at is None
    assert service.disconnect_duration == 0.0
    assert service.last_probe_success is True

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_check_connectivity_success(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "outage_probe_ok.db"))
    await db.connect()

    service = OutageService(bot, db)

    mock_reader = MagicMock()
    mock_writer = MagicMock()
    mock_writer.wait_closed = AsyncMock()

    with patch("asyncio.open_connection", AsyncMock(return_value=(mock_reader, mock_writer))):
        success, latency = await service.check_connectivity(timeout=1.0)
        assert success is True
        assert latency is not None
        assert latency >= 0.0
        assert service.last_probe_success is True
        assert service.last_probe_latency_ms == latency

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_check_connectivity_failure(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "outage_probe_fail.db"))
    await db.connect()

    service = OutageService(bot, db)

    with patch("asyncio.open_connection", AsyncMock(side_effect=OSError("Network unreachable"))):
        success, latency = await service.check_connectivity(timeout=1.0)
        assert success is False
        assert latency is None
        assert service.last_probe_success is False
        assert service.last_probe_latency_ms is None

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_disconnect_and_reconnect_lifecycle(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "outage_lifecycle.db"))
    await db.connect()

    service = OutageService(bot, db, timeout_seconds=300)

    # 1. Trigger disconnect
    service.on_disconnect()
    assert service.is_outage_active is True
    assert service.disconnected_at is not None
    assert service.disconnect_duration >= 0.0

    # Calling on_disconnect again while active should not reset start time
    t_first = service.disconnected_at
    service.on_disconnect()
    assert service.disconnected_at == t_first

    # 2. Trigger reconnect
    service.on_reconnect()
    assert service.is_outage_active is False
    assert service.disconnected_at is None
    assert service.disconnect_duration == 0.0

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_on_power_signal(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "outage_power.db"))
    await db.connect()

    service = OutageService(bot, db)
    assert not service._shutdown_triggered

    service.on_power_signal("SIGPWR")
    assert service._shutdown_triggered is True
    assert "SIGPWR" in (service._shutdown_reason or "")

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_watchdog_shutdown_on_timeout(tmp_path):
    bot = MagicMock()
    bot.is_closed = MagicMock(side_effect=[False, False, True])
    bot.is_ready = MagicMock(return_value=False)
    bot.close = AsyncMock()

    db = Database(str(tmp_path / "outage_watchdog.db"))
    await db.connect()

    service = OutageService(bot, db, timeout_seconds=10, probe_interval=1)

    # Set disconnected_at to 15 seconds in the past (exceeding 10s timeout)
    service._disconnected_at = time.monotonic() - 15.0

    with patch.object(service, "check_connectivity", AsyncMock(return_value=(False, None))):
        with patch("asyncio.sleep", AsyncMock(return_value=None)):
            await service._watchdog_loop()

    assert service._shutdown_triggered is True
    assert "Network outage exceeded" in (service._shutdown_reason or "")

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_watchdog_aborts_shutdown_when_reconnected(tmp_path):
    bot = MagicMock()
    bot.is_closed = MagicMock(side_effect=[False, False, True])
    bot.is_ready = MagicMock(return_value=True)
    bot.close = AsyncMock()

    db = Database(str(tmp_path / "outage_abort.db"))
    await db.connect()

    service = OutageService(bot, db, timeout_seconds=10, probe_interval=1)
    service.on_disconnect()
    assert service.is_outage_active is True

    # Reconnect immediately
    service.on_reconnect()
    assert service.is_outage_active is False

    with patch.object(service, "check_connectivity", AsyncMock(return_value=(True, 25.0))):
        with patch("asyncio.sleep", AsyncMock(return_value=None)):
            await service._watchdog_loop()

    # bot.close should NOT have been called
    bot.close.assert_not_called()
    assert service._shutdown_triggered is False

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_start_and_stop(tmp_path):
    bot = MagicMock()
    bot.is_closed = MagicMock(return_value=False)

    db = Database(str(tmp_path / "outage_task.db"))
    await db.connect()

    service = OutageService(bot, db, timeout_seconds=300, probe_interval=1)

    assert not service.is_running
    service.start()
    assert service.is_running

    service.stop()
    assert not service.is_running

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_debounce_suppresses_quick_resumes(tmp_path):
    bot = MagicMock()
    bot.is_closed = MagicMock(side_effect=[False, True])
    bot.close = AsyncMock()

    db = Database(str(tmp_path / "outage_debounce.db"))
    await db.connect()

    # 20s alert grace period
    service = OutageService(bot, db, timeout_seconds=300, probe_interval=1, alert_grace_seconds=20)
    service.on_disconnect()
    assert service.is_outage_active is True
    assert not service.alert_logged

    # Simulate 5s elapsed (less than 20s grace period) with reachable internet
    service._disconnected_at = time.monotonic() - 5.0

    with patch.object(service, "check_connectivity", AsyncMock(return_value=(True, 4.5))):
        with patch("asyncio.sleep", AsyncMock(return_value=None)):
            await service._watchdog_loop()

    # Alert should NOT have been logged because elapsed (5s) < alert_grace_seconds (20s) and internet is reachable
    assert service.alert_logged is False

    # Simulate fast reconnect (e.g. after 0.7s)
    service.on_reconnect()
    assert service.is_outage_active is False
    assert service.alert_logged is False

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_debounce_alerts_immediately_when_offline(tmp_path):
    bot = MagicMock()
    bot.is_closed = MagicMock(side_effect=[False, True])
    bot.close = AsyncMock()

    db = Database(str(tmp_path / "outage_offline_alert.db"))
    await db.connect()

    service = OutageService(bot, db, timeout_seconds=300, probe_interval=1, alert_grace_seconds=20)
    service.on_disconnect()
    assert service.is_outage_active is True
    assert not service.alert_logged

    # Even with only 2s elapsed, unreachable internet must trigger alert immediately
    service._disconnected_at = time.monotonic() - 2.0

    with patch.object(service, "check_connectivity", AsyncMock(return_value=(False, None))):
        with patch("asyncio.sleep", AsyncMock(return_value=None)):
            await service._watchdog_loop()

    assert service.alert_logged is True

    # Reconnect after alert logged resets state
    service.on_reconnect()
    assert service.is_outage_active is False
    assert service.alert_logged is False

    await db.close()


@pytest.mark.asyncio
async def test_outage_service_debounce_alerts_on_sustained_gateway_disconnect(tmp_path):
    bot = MagicMock()
    bot.is_closed = MagicMock(side_effect=[False, True])
    bot.close = AsyncMock()

    db = Database(str(tmp_path / "outage_sustained_alert.db"))
    await db.connect()

    service = OutageService(bot, db, timeout_seconds=300, probe_interval=1, alert_grace_seconds=20)
    service.on_disconnect()

    # Disconnect persisted for 25s (exceeding 20s grace period)
    service._disconnected_at = time.monotonic() - 25.0

    with patch.object(service, "check_connectivity", AsyncMock(return_value=(True, 5.0))):
        with patch("asyncio.sleep", AsyncMock(return_value=None)):
            await service._watchdog_loop()

    assert service.alert_logged is True

    await db.close()


def test_settings_outage_watchdog_env_parsing(monkeypatch):
    monkeypatch.setenv("TARVERI_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TARVERI_ID_HASH_SECRET", "test_secret")
    monkeypatch.setenv("TARVERI_ENABLE_OUTAGE_WATCHDOG", "true")
    monkeypatch.setenv("TARVERI_OUTAGE_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("TARVERI_OUTAGE_PROBE_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("TARVERI_OUTAGE_ALERT_GRACE_SECONDS", "30")

    s = Settings.from_env()
    assert s.enable_outage_watchdog is True
    assert s.outage_timeout_seconds == 180
    assert s.outage_probe_interval_seconds == 10
    assert s.outage_alert_grace_seconds == 30

    monkeypatch.setenv("TARVERI_ENABLE_OUTAGE_WATCHDOG", "false")
    s2 = Settings.from_env()
    assert s2.enable_outage_watchdog is False
