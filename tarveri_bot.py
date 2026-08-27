"""
TARVeri — Discord student verification bot for TARUMT.

Entry point for running the bot.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from tarveri.bot import TARVeriBot, run_bot
from tarveri.config import (
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    Settings,
    hash_student_id,
    mask_student_id,
    setup_logger,
    validate_student_id,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri")


async def main() -> None:
    try:
        settings = Settings.from_env()
    except RuntimeError as e:
        print(f"Configuration Error: {e}", file=sys.stderr)
        sys.exit(1)

    await run_bot(settings)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("TARVeri process stopped cleanly.")
