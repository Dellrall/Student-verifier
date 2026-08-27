"""
Non-blocking background service that checks for git upstream updates and notifies the hoster.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

import discord

from tarveri.database import Database

logger = logging.getLogger("tarveri")


class UpdateCheckerService:
    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        hoster_discord_id: int | None = None,
        interval_hours: int = 24,
        update_stream: str = "auto",
    ):
        self.bot = bot
        self.db = db
        self.hoster_discord_id = hoster_discord_id
        self.interval_seconds = max(300, interval_hours * 3600)
        self.update_stream = update_stream.strip() if update_stream else "auto"
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        """Starts the background update checker loop."""
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop(), name="tarveri_update_checker")

    def stop(self) -> None:
        """Cancels the background task."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        # Wait 30 seconds after startup before the initial check to allow bot to finish logging in
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return

        while not self._stop_event.is_set():
            try:
                await self.check_for_updates()
            except Exception as e:
                logger.debug(f"Update check encountered non-fatal error: {e}")

            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    async def check_for_updates(self, custom_stream: str | None = None) -> tuple[bool, int, str, str, str]:
        """
        Queries git to check if the target upstream stream/branch is ahead of current local HEAD.
        Returns (is_update_available, behind_count, local_hash, remote_hash, target_stream).
        """
        loop = asyncio.get_running_loop()
        stream_to_check = (custom_stream or self.update_stream or "auto").strip()

        def _git_check() -> tuple[bool, int, str, str, str]:
            try:
                target_branch = ""
                if stream_to_check.lower() != "auto":
                    clean_branch = stream_to_check.replace("origin/", "").strip()
                    target_branch = f"origin/{clean_branch}"
                    # Fetch target branch specifically from origin
                    subprocess.run(
                        ["git", "fetch", "origin", clean_branch, "--quiet"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                    )
                else:
                    # 1. Fetch quietly from upstream
                    subprocess.run(
                        ["git", "fetch", "--quiet"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                    )

                    # Get upstream branch from @{u}
                    upstream_res = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    target_branch = upstream_res.stdout.strip()
                    if not target_branch:
                        current_res = subprocess.run(
                            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        curr = current_res.stdout.strip() or "main"
                        target_branch = f"origin/{curr}"

                # 2. Get local HEAD hash
                local_res = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                local_hash = local_res.stdout.strip()

                # 3. Get remote hash
                remote_res = subprocess.run(
                    ["git", "rev-parse", target_branch],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if remote_res.returncode != 0:
                    return False, 0, local_hash, "", target_branch

                remote_hash = remote_res.stdout.strip() or local_hash
                if not local_hash or not remote_hash or local_hash == remote_hash:
                    return False, 0, local_hash, remote_hash, target_branch

                # 4. Count commits ahead
                count_res = subprocess.run(
                    ["git", "rev-list", "--count", f"HEAD..{target_branch}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                behind_count = int(count_res.stdout.strip() or "0")
                return behind_count > 0, behind_count, local_hash, remote_hash, target_branch
            except Exception:
                return False, 0, "", "", target_branch or stream_to_check

        is_avail, count, local_h, remote_h, target_str = await loop.run_in_executor(None, _git_check)

        if is_avail:
            msg = (
                f"A new TARVeri update is available on upstream stream '{target_str}'! "
                f"(Current: {local_h[:7]}, Remote: {remote_h[:7]} — {count} new commit(s)). "
                f"Run './scripts/update.sh' on the server host to backup and apply."
            )
            logger.info(f"[UPDATE_AVAILABLE] {msg}")

            if self.db.is_connected:
                await self.db.log("INFO", "UPDATE_AVAILABLE", msg)

            # Direct notify to hoster if configured
            if self.hoster_discord_id:
                try:
                    hoster_user = self.bot.get_user(self.hoster_discord_id)
                    if not hoster_user:
                        hoster_user = await self.bot.fetch_user(self.hoster_discord_id)

                    if hoster_user:
                        await hoster_user.send(
                            f"🔔 **TARVeri Host Notification**\n"
                            f"A new update is available on stream `{target_str}` ({count} new commit(s)).\n\n"
                            f"• Current version: `{local_h[:7]}`\n"
                            f"• Upstream version: `{remote_h[:7]}`\n\n"
                            f"Run `./scripts/update.sh` on your server terminal to test, backup, and apply."
                        )
                except Exception as e:
                    logger.debug(f"Could not send DM to hoster {self.hoster_discord_id}: {e}")

        return is_avail, count, local_h, remote_h, target_str
