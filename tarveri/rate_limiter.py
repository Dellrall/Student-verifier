"""
High-performance, memory-safe sliding window rate limiter.
"""

from __future__ import annotations

import time


class RateLimiter:
    """
    Thread-safe/async-compatible sliding window rate limiter with automated
    garbage collection of expired entries.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 600, max_tracked_users: int = 10_000):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_tracked_users = max_tracked_users
        self._attempts: dict[int, list[float]] = {}
        self._last_cleanup = time.monotonic()

    def is_rate_limited(self, user_id: int) -> bool:
        """Checks whether the user has exceeded the allowed attempt limit within the sliding window."""
        self._maybe_cleanup()

        now = time.time()
        user_attempts = self._attempts.get(user_id)
        if not user_attempts:
            return False

        valid_attempts = [t for t in user_attempts if now - t < self.window_seconds]
        if valid_attempts:
            self._attempts[user_id] = valid_attempts
        else:
            self._attempts.pop(user_id, None)

        return len(valid_attempts) >= self.max_attempts

    def record_attempt(self, user_id: int) -> None:
        """Records an attempt for the user."""
        self._maybe_cleanup()
        now = time.time()
        if user_id not in self._attempts:
            self._attempts[user_id] = [now]
        else:
            self._attempts[user_id].append(now)

    def reset(self, user_id: int | None = None) -> None:
        """Resets rate limiting for a specific user or all users."""
        if user_id is not None:
            self._attempts.pop(user_id, None)
        else:
            self._attempts.clear()

    def _maybe_cleanup(self) -> None:
        """Periodically cleans up expired keys to prevent memory leaks."""
        now_mono = time.monotonic()
        # Run cleanup at most once per 60 seconds, or if tracked users exceed capacity
        if (now_mono - self._last_cleanup < 60) and (len(self._attempts) < self.max_tracked_users):
            return

        self._last_cleanup = now_mono
        now = time.time()
        expired_keys = []
        for uid, timestamps in self._attempts.items():
            valid = [t for t in timestamps if now - t < self.window_seconds]
            if valid:
                self._attempts[uid] = valid
            else:
                expired_keys.append(uid)

        for uid in expired_keys:
            self._attempts.pop(uid, None)
