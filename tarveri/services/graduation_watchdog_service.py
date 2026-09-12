"""
Graduation & Student Lifecycle Watchdog Service.
Monitors student card expiry dates and dispatches graduation / further study / extension prompts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import discord

from tarveri.config import format_card_expiry_display, get_configured_tz
from tarveri.database import Database
from tarveri.utils import parse_db_timestamp

if TYPE_CHECKING:
    from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri")


class GraduationWatchdogService:
    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        verification_service: VerificationService,
        interval_hours: int = 24,
        prompt_cooldown_days: int = 7,
    ):
        self.bot = bot
        self.db = db
        self.verification_service = verification_service
        self.interval_hours = max(1, interval_hours)
        self.prompt_cooldown_days = max(1, prompt_cooldown_days)
        self._task: asyncio.Task[None] | None = None
        self._running: bool = False

    def start(self) -> None:
        """Starts the periodic background watchdog task."""
        if self._running or (self._task and not self._task.done()):
            return
        self._running = True
        self._task = asyncio.create_task(self._watchdog_loop(), name="tarveri_graduation_watchdog")
        logger.info(
            f"Graduation watchdog started (interval: {self.interval_hours}h, cooldown: {self.prompt_cooldown_days}d)."
        )

    def stop(self) -> None:
        """Stops the background watchdog task gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
        logger.info("Graduation watchdog stopped.")

    async def _watchdog_loop(self) -> None:
        """Periodic loop executing graduation expiry checks."""
        # Initial slight delay after bot startup so guilds & users cache is warm
        await asyncio.sleep(10)
        while self._running:
            try:
                stats = await self.check_expired_students_now()
                logger.info(f"Graduation watchdog check completed: {stats}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error during graduation watchdog check: {e}", exc_info=True)

            try:
                await asyncio.sleep(self.interval_hours * 3600)
            except asyncio.CancelledError:
                break

    async def check_expired_students_now(self) -> dict[str, int]:
        """
        Scans database for expired student verifications and sends lifecycle prompt DMs.
        Returns execution statistics.
        """
        expired_records = await self.db.get_expired_student_verifications()
        stats = {
            "total_expired": len(expired_records),
            "prompted": 0,
            "skipped_cooldown": 0,
            "dm_blocked": 0,
            "user_not_found": 0,
            "errors": 0,
        }

        now_dt = datetime.now(get_configured_tz())
        cooldown_delta = timedelta(days=self.prompt_cooldown_days)

        # Lazy import of StudentLifecycleResolutionView to prevent circular dependency
        from tarveri.cogs.verification_cog import StudentLifecycleResolutionView

        for row in expired_records:
            user_id = row["discord_user_id"]
            card_expiry = row.get("card_expiry_date")
            last_prompt_str = row.get("last_lifecycle_prompt_at")

            # Check cooldown
            if last_prompt_str:
                last_prompt_dt = parse_db_timestamp(last_prompt_str)
                if last_prompt_dt and (now_dt - last_prompt_dt < cooldown_delta):
                    stats["skipped_cooldown"] += 1
                    continue

            # Fetch user
            user = self.bot.get_user(user_id)
            if not user:
                try:
                    user = await self.bot.fetch_user(user_id)
                except (discord.NotFound, discord.HTTPException):
                    stats["user_not_found"] += 1
                    continue

            expiry_display = format_card_expiry_display(card_expiry) if card_expiry else "Expired"

            embed = discord.Embed(
                title="🎓 TARUMT Student Card Expiry & Academic Status Confirmation",
                description=(
                    f"Hello {user.mention}!\n\n"
                    f"According to TARVeri records, your TARUMT student card validity has reached its expiry date (**{expiry_display}**).\n\n"
                    "Please confirm your current academic status using the options below:\n\n"
                    "• 🎓 **I have Graduated:** Claim your official **TARUMT Alumni** role & card badge.\n"
                    "• 📚 **Further Studies at TARUMT:** Progressing to Degree / Masters? Enter your new Student ID to update your study level.\n"
                    "• ⏳ **Still Studying / Extension:** Extending a semester or final year project? Update your student card expiry date.\n"
                    "• 🚪 **Discontinue Studies / Dropout:** Discontinuing studies? Withdraw your student verification."
                ),
                color=discord.Color.from_rgb(212, 175, 55),
            )
            embed.set_footer(text="TARVeri Academic Lifecycle Engine • Click an option below to update")

            view = StudentLifecycleResolutionView(self.verification_service, self.db)

            try:
                await user.send(embed=embed, view=view)
                now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                await self.db.update_verification_profile(
                    discord_user_id=user_id,
                    lifecycle_prompt_status="prompted",
                    last_lifecycle_prompt_at=now_iso,
                )
                await self.db.log(
                    "INFO",
                    "GRADUATION_PROMPT_SENT",
                    f"Sent academic lifecycle resolution prompt to {user} (ID: {user_id}, card expired: {expiry_display})",
                    user_id=user_id,
                )
                stats["prompted"] += 1
            except discord.Forbidden:
                now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                await self.db.update_verification_profile(
                    discord_user_id=user_id,
                    last_lifecycle_prompt_at=now_iso,
                )
                await self.db.log(
                    "INFO",
                    "GRADUATION_PROMPT_DM_BLOCKED",
                    f"Could not send lifecycle prompt DM to {user} (ID: {user_id}) — DMs disabled.",
                    user_id=user_id,
                )
                stats["dm_blocked"] += 1
            except Exception as e:
                logger.warning(f"Failed to send lifecycle prompt to {user_id}: {e}")
                stats["errors"] += 1

        return stats
