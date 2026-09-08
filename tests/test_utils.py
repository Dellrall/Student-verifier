import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
import discord

from tarveri.utils import delete_after_delay, schedule_ttl_delete


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

