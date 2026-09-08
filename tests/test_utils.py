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
