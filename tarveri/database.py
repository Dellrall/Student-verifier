"""
Asynchronous SQLite database layer with WAL mode, indexing, schema versioning, and backup support.
"""

from __future__ import annotations

import logging
import os
import sqlite3
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
        await self._conn.execute("PRAGMA cache_size = -4000;")  # 4MB in-memory page cache
        await self._conn.execute("PRAGMA temp_store = MEMORY;")  # Keep temp tables & sorts in RAM
        await self._conn.execute("PRAGMA mmap_size = 67108864;")  # 64MB memory-mapped I/O

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
                guest_role_name TEXT DEFAULT 'Guest',
                review_channel_id INTEGER,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS referral_codes (
                code TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                referrer_discord_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_by_discord_id INTEGER,
                used_at TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE'
            );

            CREATE TABLE IF NOT EXISTS guest_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                applicant_id INTEGER NOT NULL,
                referrer_id INTEGER,
                channel_id INTEGER NOT NULL,
                referral_code TEXT,
                reason TEXT,
                vouch_note TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TEXT NOT NULL,
                closed_at TEXT,
                closed_by_admin_id INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_verifications_faculty ON verifications(faculty_code);
            CREATE INDEX IF NOT EXISTS idx_referral_guild_referrer ON referral_codes(guild_id, referrer_discord_id);
            CREATE INDEX IF NOT EXISTS idx_referral_status ON referral_codes(status);
            CREATE INDEX IF NOT EXISTS idx_guest_tickets_guild ON guest_tickets(guild_id);
            CREATE INDEX IF NOT EXISTS idx_guest_tickets_channel ON guest_tickets(channel_id);
            CREATE INDEX IF NOT EXISTS idx_guest_tickets_applicant ON guest_tickets(applicant_id);
            """
        )

        # Migration helper for existing databases: ensure new columns in guild_settings exist
        cursor = await self._conn.execute("PRAGMA table_info(guild_settings);")
        existing_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [("guest_role_name", "TEXT DEFAULT 'Guest'"), ("review_channel_id", "INTEGER")]:
            if col not in existing_cols:
                await self._conn.execute(f"ALTER TABLE guild_settings ADD COLUMN {col} {col_def};")

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
        safe_path = backup_path.replace("'", "''")
        await self._conn.execute(f"VACUUM INTO '{safe_path}';")
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
        guild_ctx = f" [{guild.name}]" if guild else ""
        log_func(f"[{event_type}]{guild_ctx} {message}")

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

    async def get_guild_settings(
        self, guild_id: int
    ) -> tuple[int | None, int | None, str | None, int | None] | None:
        """Returns (welcome_channel_id, help_channel_id, guest_role_name, review_channel_id) for the given guild, or None."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT welcome_channel_id, help_channel_id, guest_role_name, review_channel_id
               FROM guild_settings WHERE guild_id = ?""",
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

    async def set_guild_guest_role(self, guild_id: int, guest_role_name: str | None) -> None:
        """Sets or clears the custom guest role name for a guild (defaults to 'Guest' if None)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        role_to_set = guest_role_name.strip() if guest_role_name else "Guest"
        await self._conn.execute(
            """INSERT INTO guild_settings (guild_id, guest_role_name, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   guest_role_name = excluded.guest_role_name,
                   updated_at = excluded.updated_at""",
            (guild_id, role_to_set, ts),
        )
        await self._conn.commit()

    async def set_guild_review_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Sets or clears the designated parent review channel for private guest threads."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._conn.execute(
            """INSERT INTO guild_settings (guild_id, review_channel_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   review_channel_id = excluded.review_channel_id,
                   updated_at = excluded.updated_at""",
            (guild_id, channel_id, ts),
        )
        await self._conn.commit()

    async def create_referral_code(
        self, code: str, guild_id: int, referrer_discord_id: int, expires_at: str
    ) -> None:
        """Saves a newly generated referral code."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._conn.execute(
            """INSERT INTO referral_codes (code, guild_id, referrer_discord_id, created_at, expires_at, status)
               VALUES (?, ?, ?, ?, ?, 'ACTIVE')""",
            (code, guild_id, referrer_discord_id, ts, expires_at),
        )
        await self._conn.commit()

    async def get_referral_code(self, code: str, guild_id: int) -> dict[str, Any] | None:
        """Fetches referral code information."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT code, guild_id, referrer_discord_id, created_at, expires_at, used_by_discord_id, used_at, status
               FROM referral_codes WHERE code = ? AND guild_id = ?""",
            (code.strip().upper(), guild_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "code": row[0],
            "guild_id": row[1],
            "referrer_discord_id": row[2],
            "created_at": row[3],
            "expires_at": row[4],
            "used_by_discord_id": row[5],
            "used_at": row[6],
            "status": row[7],
        }

    async def count_active_referrals_for_user(self, guild_id: int, referrer_discord_id: int) -> int:
        """Counts how many active (unexpired, unused) referral codes a student currently holds."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM referral_codes
               WHERE guild_id = ? AND referrer_discord_id = ?
               AND status IN ('ACTIVE', 'PENDING_APPROVAL')
               AND expires_at > ?""",
            (guild_id, referrer_discord_id, ts),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_user_referrals(
        self, guild_id: int, referrer_discord_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Lists referral codes created by a user."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT code, created_at, expires_at, used_by_discord_id, status
               FROM referral_codes
               WHERE guild_id = ? AND referrer_discord_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, referrer_discord_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "code": r[0],
                "created_at": r[1],
                "expires_at": r[2],
                "used_by_discord_id": r[3],
                "status": r[4],
            }
            for r in rows
        ]

    async def update_referral_code_status(
        self, code: str, guild_id: int, status: str, used_by_discord_id: int | None = None
    ) -> bool:
        """Updates referral code status (e.g., PENDING_APPROVAL, USED, REJECTED, ACTIVE)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if used_by_discord_id is not None:
            cursor = await self._conn.execute(
                """UPDATE referral_codes
                   SET status = ?, used_by_discord_id = ?, used_at = ?
                   WHERE code = ? AND guild_id = ?""",
                (status, used_by_discord_id, ts, code.strip().upper(), guild_id),
            )
        else:
            cursor = await self._conn.execute(
                """UPDATE referral_codes
                   SET status = ?
                   WHERE code = ? AND guild_id = ?""",
                (status, code.strip().upper(), guild_id),
            )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def create_guest_ticket(
        self,
        guild_id: int,
        applicant_id: int,
        channel_id: int,
        referrer_id: int | None = None,
        referral_code: str | None = None,
        reason: str | None = None,
    ) -> int:
        """Creates a guest ticket record and returns its ticket_id."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._conn.execute(
            """INSERT INTO guest_tickets
               (guild_id, applicant_id, referrer_id, channel_id, referral_code, reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)""",
            (guild_id, applicant_id, referrer_id, channel_id, referral_code, reason, ts),
        )
        await self._conn.commit()
        return cursor.lastrowid or 0

    async def get_guest_ticket_by_channel(self, channel_id: int) -> dict[str, Any] | None:
        """Fetches guest ticket by thread/channel ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, status, created_at, closed_at, closed_by_admin_id
               FROM guest_tickets WHERE channel_id = ?""",
            (channel_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "ticket_id": row[0],
            "guild_id": row[1],
            "applicant_id": row[2],
            "referrer_id": row[3],
            "channel_id": row[4],
            "referral_code": row[5],
            "reason": row[6],
            "vouch_note": row[7],
            "status": row[8],
            "created_at": row[9],
            "closed_at": row[10],
            "closed_by_admin_id": row[11],
        }

    async def get_guest_ticket_by_id(self, ticket_id: int) -> dict[str, Any] | None:
        """Fetches guest ticket by ticket ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, status, created_at, closed_at, closed_by_admin_id
               FROM guest_tickets WHERE ticket_id = ?""",
            (ticket_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "ticket_id": row[0],
            "guild_id": row[1],
            "applicant_id": row[2],
            "referrer_id": row[3],
            "channel_id": row[4],
            "referral_code": row[5],
            "reason": row[6],
            "vouch_note": row[7],
            "status": row[8],
            "created_at": row[9],
            "closed_at": row[10],
            "closed_by_admin_id": row[11],
        }

    async def get_open_guest_ticket_for_applicant(
        self, guild_id: int, applicant_id: int
    ) -> dict[str, Any] | None:
        """Checks if the user already has an active open ticket in this guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, status, created_at
               FROM guest_tickets
               WHERE guild_id = ? AND applicant_id = ? AND status = 'OPEN'""",
            (guild_id, applicant_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "ticket_id": row[0],
            "guild_id": row[1],
            "applicant_id": row[2],
            "referrer_id": row[3],
            "channel_id": row[4],
            "referral_code": row[5],
            "reason": row[6],
            "vouch_note": row[7],
            "status": row[8],
            "created_at": row[9],
        }

    async def update_guest_ticket_vouch(self, ticket_id: int, vouch_note: str) -> bool:
        """Saves a student vouch statement on a guest ticket."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "UPDATE guest_tickets SET vouch_note = ? WHERE ticket_id = ?",
            (vouch_note, ticket_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def close_guest_ticket(
        self, ticket_id: int, status: str, closed_by_admin_id: int | None = None
    ) -> bool:
        """Closes a guest ticket with status ('APPROVED', 'REJECTED', 'EXPIRED')."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._conn.execute(
            """UPDATE guest_tickets
               SET status = ?, closed_at = ?, closed_by_admin_id = ?
               WHERE ticket_id = ?""",
            (status, ts, closed_by_admin_id, ticket_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def cleanup_expired_referrals(self) -> int:
        """Bulk updates all expired active referral codes to EXPIRED status."""
        if not self._conn:
            return 0
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._conn.execute(
            """UPDATE referral_codes SET status = 'EXPIRED'
               WHERE status = 'ACTIVE' AND expires_at <= ?""",
            (ts,),
        )
        await self._conn.commit()
        return cursor.rowcount

