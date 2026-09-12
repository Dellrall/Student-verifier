"""
General utility functions for TARVeri.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, tzinfo
import logging

import discord

from tarveri.config import get_configured_tz

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


def parse_ticket_seq(seq: int | str | None) -> int | None:
    """
    Parses a ticket sequence or alphanumeric tracking code back to its integer sequence number.
    Handles:
    - Integers: 1 -> 1, 42 -> 42
    - Numeric strings: "1" -> 1, "#42" -> 42
    - Alphanumeric tracking codes: "A0001" -> 1, "#A0002" -> 2, "B0001" -> 10000, "b0042" -> 10041
    - Returns None if input cannot be parsed.
    """
    if seq is None:
        return None
    if isinstance(seq, int):
        return seq if seq > 0 else None

    if isinstance(seq, str):
        cleaned = seq.strip().lstrip("#").strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            val = int(cleaned)
            return val if val > 0 else None

        import re

        match = re.match(r"^([A-Za-z]+)(\d+)$", cleaned)
        if match:
            letters = match.group(1).upper()
            num_part = int(match.group(2))
            if num_part < 1 or num_part > 9999:
                return None
            series_idx = 0
            for ch in letters:
                series_idx = series_idx * 26 + (ord(ch) - ord("A") + 1)
            series_idx -= 1
            return series_idx * 9999 + num_part

    return None


def parse_db_timestamp(ts_str: str | None, tz: tzinfo | None = None) -> datetime | None:
    """Parses a database timestamp string into a timezone-aware datetime object."""
    if not ts_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(ts_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz or get_configured_tz())
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(ts_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz or get_configured_tz())
        return dt
    except ValueError:
        return None


