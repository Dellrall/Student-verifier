"""
Bot class, lifecycle orchestration, and main runner.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import discord
from discord.ext import commands

from tarveri.cogs.admin_cog import AdminCog
from tarveri.cogs.guest_cog import GuestCog, GuestReviewThreadView, VerificationGatewayView
from tarveri.cogs.verification_cog import VerificationCog
from tarveri.config import Settings, setup_logger
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.guest_service import GuestService
from tarveri.services.update_checker import UpdateCheckerService
from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri")


class TARVeriBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.db = Database(settings.db_path)
        self.rate_limiter = RateLimiter(
            max_attempts=settings.rate_limit_max_attempts,
            window_seconds=settings.rate_limit_window_seconds,
        )
        self.service = VerificationService(
            bot=self,
            db=self.db,
            secret=settings.id_hash_secret,
            rate_limiter=self.rate_limiter,
        )
        self.guest_service = GuestService(
            bot=self,
            db=self.db,
            admin_role_name=settings.admin_role_name,
            rate_limiter=self.rate_limiter,
        )
        self.update_checker = (
            UpdateCheckerService(
                bot=self,
                db=self.db,
                hoster_discord_id=settings.hoster_discord_id,
                interval_hours=settings.update_check_interval_hours,
                update_stream=settings.update_stream,
            )
            if settings.enable_update_checker
            else None
        )

    async def setup_hook(self) -> None:
        """Initializes database and registers cogs and persistent views during bot startup."""
        await self.db.connect()

        # Add cogs
        await self.add_cog(
            VerificationCog(
                bot=self,
                db=self.db,
                service=self.service,
                rate_limiter=self.rate_limiter,
                settings=self.settings,
            )
        )
        await self.add_cog(
            AdminCog(
                bot=self,
                db=self.db,
                service=self.service,
                rate_limiter=self.rate_limiter,
                admin_role_name=self.settings.admin_role_name,
                update_checker=self.update_checker,
            )
        )
        await self.add_cog(
            GuestCog(
                bot=self,
                db=self.db,
                guest_service=self.guest_service,
                verification_service=self.service,
            )
        )

        # Register persistent views so buttons work across bot reboots
        self.add_view(VerificationGatewayView(self.service, self.guest_service))
        self.add_view(GuestReviewThreadView(self.guest_service))

        # Sync application commands
        await self.tree.sync()
        logger.info("Database connected, cogs loaded, persistent views registered, and command tree synced.")

        if self.update_checker:
            self.update_checker.start()

    async def on_ready(self) -> None:
        if self.user:
            total_verified = await self.db.total_verified()
            guild_names = [g.name for g in self.guilds]
            await self.db.log(
                "INFO",
                "STARTUP",
                f"Logged in as {self.user} (ID: {self.user.id}) | Connected to {len(self.guilds)} server(s): {guild_names} | Total verified students: {total_verified}",
            )

    async def close(self) -> None:
        """Gracefully tears down the bot, logs shutdown, and flushes SQLite WAL."""
        logger.info("Initiating graceful shutdown...")

        if self.update_checker:
            self.update_checker.stop()

        try:
            if self.db.is_connected:
                await self.db.log("INFO", "SHUTDOWN", "TARVeri is shutting down gracefully.")
        except Exception as e:
            logger.warning(f"Could not log shutdown to DB: {e}")

        try:
            await self.db.close()
            logger.info("Database connection closed cleanly with WAL checkpoint.")
        except Exception as e:
            logger.error(f"Error while closing database: {e}")

        await super().close()


async def run_bot(settings: Settings | None = None) -> None:
    """Entry point for running the bot with OS signal handlers."""
    if settings is None:
        settings = Settings.from_env()

    setup_logger(settings.log_file, settings.log_max_bytes, settings.log_backup_count)
    bot = TARVeriBot(settings)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal() -> None:
        if not stop_event.is_set():
            stop_event.set()
            logger.info("Interrupt signal received (SIGINT/SIGTERM). Closing TARVeri...")
            asyncio.create_task(bot.close())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except (NotImplementedError, RuntimeError):
            pass

    async with bot:
        await bot.start(settings.bot_token)
