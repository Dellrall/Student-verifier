"""
General utility functions for TARVeri.
"""

from __future__ import annotations

import asyncio
import logging

import discord

logger = logging.getLogger("tarveri")


async def delete_after_delay(
    target: discord.Interaction | discord.Message | discord.WebhookMessage,
    delay: float = 60.0,
) -> None:
    """
    Safely deletes an interaction response or message after a TTL delay in seconds.
    Works for both ephemeral interaction responses and standard messages.
    """
    try:
        await asyncio.sleep(delay)
        if isinstance(target, discord.Interaction):
            try:
                await target.delete_original_response()
            except Exception:
                pass
        elif hasattr(target, "delete"):
            try:
                await target.delete()
            except Exception:
                pass
    except (asyncio.CancelledError, discord.NotFound, discord.HTTPException, discord.Forbidden):
        pass
    except Exception as e:
        logger.debug("Failed to delete message after delay: %s", e)


def schedule_ttl_delete(
    target: discord.Interaction | discord.Message | discord.WebhookMessage,
    delay: float = 60.0,
) -> asyncio.Task[None]:
    """Schedules a non-blocking background task to delete the interaction response or message after a TTL delay."""
    return asyncio.create_task(delete_after_delay(target, delay=delay))


def format_ticket_seq(seq: int | str | None) -> str:
    """
    Formats a sequential ticket number into an alphanumeric tracking code.
    Includes letters and digits (e.g. 1 -> 'A0001', 2 -> 'A0002', 10000 -> 'B0001').
    """
    if seq is None:
        return "A0001"
    if isinstance(seq, str):
        if seq.strip().isdigit():
            val = int(seq.strip())
        else:
            return seq.strip().upper()
    else:
        try:
            val = int(seq)
        except (ValueError, TypeError):
            return "A0001"

    if val <= 0:
        val = 1

    idx = val - 1
    num = (idx % 9999) + 1
    series_idx = idx // 9999

    prefix = ""
    while True:
        prefix = chr(ord("A") + (series_idx % 26)) + prefix
        series_idx = series_idx // 26 - 1
        if series_idx < 0:
            break

    return f"{prefix}{num:04d}"

