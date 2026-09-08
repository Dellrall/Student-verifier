"""
Module entry point allowing TARVeri to be executed with:
    python -m tarveri
"""

from __future__ import annotations

import asyncio
import logging
import sys

from tarveri.bot import run_bot
from tarveri.config import Settings

logger = logging.getLogger("tarveri")


def main() -> None:
    try:
        settings = Settings.from_env()
    except RuntimeError as e:
        print(f"Configuration Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(run_bot(settings))
    except (KeyboardInterrupt, SystemExit):
        logger.info("TARVeri process stopped cleanly.")


if __name__ == "__main__":
    main()
