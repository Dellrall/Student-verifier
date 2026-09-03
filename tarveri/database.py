"""
Asynchronous SQLite database layer with WAL mode, indexing, schema versioning, and backup support.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import discord

logger = logging.getLogger("tarveri")

SCHEMA_VERSION = 1


class Database:
    """
    Database interface for TARVeri.
    Uses SQLite WAL mode for non-blocking concurrent reads during verification writes.
    """

    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Establishes connection, verifies schema version, and creates schema and indexes."""
        if self._conn is not None:
            return

        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        # WAL mode lets reads (e.g. admin queries on audit_log) proceed without
        # blocking on writes (verifications), which matters as guild count grows.
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA synchronous = NORMAL;")

        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS verifications (
                discord_user_id INTEGER PRIMARY KEY,
                student_id_hash TEXT UNIQUE NOT NULL,
                faculty_code TEXT NOT NULL,
                verified_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                event_type TEXT NOT NULL,
                guild_id INTEGER,
                guild_name TEXT,
                user_id INTEGER,
                message TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                welcome_channel_id INTEGER,
                help_channel_id INTEGER,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_verifications_faculty ON verifications(faculty_code);
            """
        )

        cursor = await self._conn.execute("PRAGMA user_version;")
        row = await cursor.fetchone()
        current_version = row[0] if row else 0

        if current_version == 0:
            await self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")

        await self._conn.commit()

    async def close(self) -> None:
        """Flushes SQLite WAL to disk and closes the connection cleanly."""
        if self._conn:
            try:
                # Flush and truncate write-ahead log (WAL) into the main database file
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                await self._conn.commit()
            except Exception as e:
                logger.warning(f"Failed to checkpoint WAL during database shutdown: {e}")
            finally:
                await self._conn.close()
                self._conn = None

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def create_backup(self, backup_dir: str = "backups") -> str:
        """
        Creates a consistent, point-in-time point-and-restore snapshot of the database
        even while WAL writes are occurring.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_filename = f"tarveri_backup_{timestamp}.db"
        backup_path = os.path.join(backup_dir, backup_filename)

        if os.path.exists(backup_path):
            os.remove(backup_path)

        # VACUUM INTO safely creates an atomic copy of active database
        await self._conn.execute(f"VACUUM INTO '{backup_path}';")
        return backup_path

    async def log(
        self,
        level: str,
        event_type: str,
        message: str,
        guild: discord.Guild | None = None,
        user_id: int | None = None,
    ) -> None:
        """Writes to both the DB audit table and standard application logger."""
        log_func = getattr(logger, level.lower(), logger.info)
        log_func(f"[{event_type}] {message}")

        if not self._conn:
            return

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            await self._conn.execute(
                """INSERT INTO audit_log
                   (timestamp, level, event_type, guild_id, guild_name, user_id, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    level,
                    event_type,
                    guild.id if guild else None,
                    guild.name if guild else None,
                    user_id,
                    message,
                ),
            )
            await self._conn.commit()
        except Exception as e:
            logger.error(f"Failed to insert audit log entry into DB: {e}")

    async def get_verification_by_user(self, discord_user_id: int) -> tuple[str, str, str] | None:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT student_id_hash, faculty_code, verified_at FROM verifications WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        return await cursor.fetchone()

    async def get_verification_by_id_hash(self, student_id_hash: str) -> tuple[int] | None:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT discord_user_id FROM verifications WHERE student_id_hash = ?",
            (student_id_hash,),
        )
        return await cursor.fetchone()

    async def record_verification(
        self, discord_user_id: int, student_id_hash: str, faculty_code: str
    ) -> None:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._conn.execute(
            """INSERT INTO verifications (discord_user_id, student_id_hash, faculty_code, verified_at)
               VALUES (?, ?, ?, ?)""",
            (discord_user_id, student_id_hash, faculty_code, ts),
        )
        await self._conn.commit()

    async def delete_verification(self, discord_user_id: int) -> bool:
        """Unlinks a Discord account from its student ID. Returns True if record existed."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "DELETE FROM verifications WHERE discord_user_id = ?", (discord_user_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def total_verified(self) -> int:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute("SELECT COUNT(*) FROM verifications")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def counts_by_faculty(self) -> list[tuple[str, int]]:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT faculty_code, COUNT(*) FROM verifications GROUP BY faculty_code ORDER BY COUNT(*) DESC"
        )
        return await cursor.fetchall()

    async def verified_in_last(self, hours: int) -> int:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM verifications WHERE verified_at >= datetime('now', ?)",
            (f"-{hours} hours",),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def recent_audit(
        self, limit: int = 10, offset: int = 0, event_type: str | None = None
    ) -> list[tuple[str, str, str, str | None, int | None, str]]:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        if event_type:
            cursor = await self._conn.execute(
                """SELECT timestamp, level, event_type, guild_name, user_id, message
                   FROM audit_log
                   WHERE event_type = ?
                   ORDER BY id DESC
                   LIMIT ? OFFSET ?""",
                (event_type, limit, offset),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT timestamp, level, event_type, guild_name, user_id, message
                   FROM audit_log
                   ORDER BY id DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            )
        return await cursor.fetchall()

    async def get_guild_settings(self, guild_id: int) -> tuple[int | None, int | None] | None:
        """Returns (welcome_channel_id, help_channel_id) for the given guild, or None if not set."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT welcome_channel_id, help_channel_id FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        return await cursor.fetchone()

    async def set_guild_welcome_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Sets or clears the welcome channel ID for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._conn.execute(
            """INSERT INTO guild_settings (guild_id, welcome_channel_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   welcome_channel_id = excluded.welcome_channel_id,
                   updated_at = excluded.updated_at""",
            (guild_id, channel_id, ts),
        )
        await self._conn.commit()

    async def set_guild_help_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Sets or clears the help channel ID for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._conn.execute(
            """INSERT INTO guild_settings (guild_id, help_channel_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   help_channel_id = excluded.help_channel_id,
                   updated_at = excluded.updated_at""",
            (guild_id, channel_id, ts),
        )
        await self._conn.commit()

