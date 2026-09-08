import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
import discord

from tarveri.utils import delete_after_delay, format_ticket_seq, schedule_ttl_delete


@pytest.mark.asyncio
async def test_delete_after_delay_interaction():
    interaction = MagicMock(spec=discord.Interaction)
    interaction.delete_original_response = AsyncMock()

    await delete_after_delay(interaction, delay=0.01)
    interaction.delete_original_response.assert_called_once()


@pytest.mark.asyncio
async def test_delete_after_delay_message():
    message = MagicMock(spec=discord.Message)
    message.delete = AsyncMock()

    await delete_after_delay(message, delay=0.01)
    message.delete.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_ttl_delete_task():
    interaction = MagicMock(spec=discord.Interaction)
    interaction.delete_original_response = AsyncMock()

    task = schedule_ttl_delete(interaction, delay=0.01)
    assert isinstance(task, asyncio.Task)
    await task
    interaction.delete_original_response.assert_called_once()


@pytest.mark.asyncio
async def test_delete_after_delay_not_found_handled():
    # If the message is already deleted by user, it raises NotFound
    message = MagicMock(spec=discord.Message)
    message.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(), "Unknown Message"))

    # Should not raise exception
    await delete_after_delay(message, delay=0.01)
    message.delete.assert_called_once()


@pytest.mark.asyncio
async def test_delete_after_delay_forbidden_handled():
    # Bot lacks manage_messages permission
    interaction = MagicMock(spec=discord.Interaction)
    interaction.delete_original_response = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(), "Missing Permissions")
    )

    # Should not raise exception
    await delete_after_delay(interaction, delay=0.01)
    interaction.delete_original_response.assert_called_once()


@pytest.mark.asyncio
async def test_delete_after_delay_cancellation_handled():
    interaction = MagicMock(spec=discord.Interaction)
    interaction.delete_original_response = AsyncMock()

    task = schedule_ttl_delete(interaction, delay=10.0)
    task.cancel()
    # Awaiting cancelled task should not raise unhandled exception
    try:
        await task
    except asyncio.CancelledError:
        pass
    interaction.delete_original_response.assert_not_called()


def test_format_ticket_seq():
    # Base cases
    assert format_ticket_seq(1) == "A0001"
    assert format_ticket_seq(2) == "A0002"
    assert format_ticket_seq(9999) == "A9999"
    assert format_ticket_seq(10000) == "B0001"
    assert format_ticket_seq(19998) == "B9999"
    assert format_ticket_seq(19999) == "C0001"
    assert format_ticket_seq(26 * 9999 + 1) == "AA0001"

    # Edge cases / fallbacks
    assert format_ticket_seq(None) == "A0001"
    assert format_ticket_seq(0) == "A0001"
    assert format_ticket_seq(-10) == "A0001"
    assert format_ticket_seq("15") == "A0015"
    assert format_ticket_seq("A0042") == "A0042"
    assert format_ticket_seq("b0010") == "B0010"
    assert format_ticket_seq("custom_code") == "CUSTOM_CODE"


