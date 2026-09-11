"""
Asynchronous SQLite database layer with WAL mode, indexing, schema versioning, and backup support.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import aiosqlite
import discord

from tarveri.config import get_configured_tz, now_formatted

logger = logging.getLogger("tarveri")

SCHEMA_VERSION = 1


def rotate_backups(backup_dir: str = "backups", max_backups: int = 10) -> list[str]:
    """
    Keeps only the `max_backups` most recent backup database files in `backup_dir`,
    deleting older backups. Returns the list of deleted backup file paths.
    """
    if not os.path.exists(backup_dir) or max_backups <= 0:
        return []

    backup_files: list[str] = []
    for entry in os.listdir(backup_dir):
        full_path = os.path.join(backup_dir, entry)
        if os.path.isfile(full_path) and entry.endswith(".db"):
            backup_files.append(full_path)

    # Sort files by modification time descending (newest first)
    backup_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    deleted: list[str] = []
    if len(backup_files) > max_backups:
        to_delete = backup_files[max_backups:]
        for path in to_delete:
            try:
                os.remove(path)
                deleted.append(path)
                logger.info(f"Rotated old database backup: {path}")
            except OSError as e:
                logger.warning(f"Failed to remove old backup file '{path}': {e}")

    return deleted


def list_backups(backup_dir: str = "backups") -> list[dict[str, Any]]:
    """
    Returns a list of available backup files in `backup_dir` sorted newest to oldest.
    Each item contains 'filename', 'path', 'mtime', 'size_bytes', and 'timestamp'.
    """
    if not os.path.exists(backup_dir):
        return []
    backup_files: list[dict[str, Any]] = []
    for entry in os.listdir(backup_dir):
        full_path = os.path.join(backup_dir, entry)
        if os.path.isfile(full_path) and entry.endswith(".db"):
            mtime = os.path.getmtime(full_path)
            size = os.path.getsize(full_path)
            backup_files.append(
                {
                    "filename": entry,
                    "path": full_path,
                    "mtime": mtime,
                    "size_bytes": size,
                    "timestamp": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
    backup_files.sort(key=lambda x: x["mtime"], reverse=True)
    return backup_files


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

        # Self-healing: verify database integrity upon connection
        try:
            cursor = await self._conn.execute("PRAGMA integrity_check;")
            rows = await cursor.fetchall()
            if rows == [("ok",)]:
                logger.debug("Database integrity check passed (ok).")
            else:
                logger.error(f"Database integrity check issue detected: {rows}")
        except Exception as e:
            logger.warning(f"Could not execute database integrity check: {e}")

        # Checkpoint WAL on startup to merge any uncheckpointed journal from previous process
        try:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception as e:
            logger.debug(f"Initial WAL checkpoint notice: {e}")

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
                admin_role_name TEXT,
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
                ticket_seq INTEGER,
                applicant_id INTEGER NOT NULL,
                referrer_id INTEGER,
                channel_id INTEGER NOT NULL,
                referral_code TEXT,
                reason TEXT,
                vouch_note TEXT,
                vouched_by_id INTEGER,
                vouched_at TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TEXT NOT NULL,
                closed_at TEXT,
                closed_by_admin_id INTEGER,
                close_reason TEXT,
                pinged_admin_ids TEXT,
                last_pinged_at TEXT
            );

            CREATE TABLE IF NOT EXISTS bot_created_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER PRIMARY KEY,
                role_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS verification_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id INTEGER NOT NULL,
                from_id_hash TEXT NOT NULL,
                from_faculty_code TEXT NOT NULL,
                from_campus_code TEXT NOT NULL,
                from_level_code TEXT NOT NULL,
                to_id_hash TEXT NOT NULL,
                to_faculty_code TEXT NOT NULL,
                to_campus_code TEXT NOT NULL,
                to_level_code TEXT NOT NULL,
                transitioned_at TEXT NOT NULL,
                notes TEXT
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
            CREATE INDEX IF NOT EXISTS idx_bot_created_roles_guild ON bot_created_roles(guild_id);
            CREATE INDEX IF NOT EXISTS idx_transitions_user ON verification_transitions(discord_user_id);
            """
        )

        # Migration helper for existing databases: ensure all expected columns exist
        # 1. guild_settings
        cursor = await self._conn.execute("PRAGMA table_info(guild_settings);")
        existing_guild_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [
            ("welcome_channel_id", "INTEGER"),
            ("help_channel_id", "INTEGER"),
            ("guest_role_name", "TEXT DEFAULT 'Guest'"),
            ("review_channel_id", "INTEGER"),
            ("admin_role_name", "TEXT"),
            ("updated_at", "TEXT DEFAULT ''"),
        ]:
            if col not in existing_guild_cols:
                await self._conn.execute(f"ALTER TABLE guild_settings ADD COLUMN {col} {col_def};")

        # 2. referral_codes
        cursor = await self._conn.execute("PRAGMA table_info(referral_codes);")
        existing_referral_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [
            ("used_by_discord_id", "INTEGER"),
            ("used_at", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
        ]:
            if col not in existing_referral_cols:
                await self._conn.execute(f"ALTER TABLE referral_codes ADD COLUMN {col} {col_def};")

        # 3. guest_tickets
        cursor = await self._conn.execute("PRAGMA table_info(guest_tickets);")
        existing_ticket_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [
            ("ticket_seq", "INTEGER"),
            ("referrer_id", "INTEGER"),
            ("referral_code", "TEXT"),
            ("reason", "TEXT"),
            ("vouch_note", "TEXT"),
            ("vouched_by_id", "INTEGER"),
            ("vouched_at", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'OPEN'"),
            ("closed_at", "TEXT"),
            ("closed_by_admin_id", "INTEGER"),
            ("close_reason", "TEXT"),
            ("pinged_admin_ids", "TEXT"),
            ("last_pinged_at", "TEXT"),
        ]:
            if col not in existing_ticket_cols:
                await self._conn.execute(f"ALTER TABLE guest_tickets ADD COLUMN {col} {col_def};")

        # 4. verifications (Alumni fields + Campus & Study Level fields + Expiry fields)
        cursor = await self._conn.execute("PRAGMA table_info(verifications);")
        existing_veri_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [
            ("is_alumni", "INTEGER DEFAULT 0"),
            ("graduated_year", "INTEGER"),
            ("programme", "TEXT"),
            ("graduated_at", "TEXT"),
            ("campus_code", "TEXT"),
            ("level_code", "TEXT"),
            ("card_expiry_date", "TEXT"),
            ("lifecycle_prompt_status", "TEXT DEFAULT 'ACTIVE'"),
            ("last_lifecycle_prompt_at", "TEXT"),
        ]:
            if col not in existing_veri_cols:
                await self._conn.execute(f"ALTER TABLE verifications ADD COLUMN {col} {col_def};")

        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verifications_alumni ON verifications(is_alumni);"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verifications_expiry ON verifications(card_expiry_date);"
        )

        # 5. One-time data migration: Backfill legacy verifications missing campus_code to 'W' (KL Main Campus)
        try:
            cursor = await self._conn.execute(
                """UPDATE verifications
                   SET campus_code = 'W'
                   WHERE campus_code IS NULL"""
            )
            if cursor.rowcount > 0:
                logger.info(
                    f"Migrated {cursor.rowcount} legacy student verification record(s) with default campus_code='W' (KL Main Campus)."
                )
        except Exception as e:
            logger.debug(f"Legacy campus_code migration notice: {e}")

        # 6. One-time data migration: Backfill legacy active student verifications missing card_expiry_date
        try:
            cursor = await self._conn.execute(
                """SELECT discord_user_id, verified_at, level_code
                   FROM verifications
                   WHERE card_expiry_date IS NULL AND is_alumni = 0"""
            )
            rows = await cursor.fetchall()
            backfilled_count = 0
            for u_id, v_at, lvl_code in rows:
                calc_year = None
                if v_at:
                    try:
                        dt = datetime.fromisoformat(v_at.replace(" ", "T"))
                        calc_year = dt.year
                    except Exception:
                        pass
                if not calc_year:
                    calc_year = datetime.now().year

                lvl = (lvl_code or "R").upper()
                if lvl == "F":
                    est_date = f"{calc_year + 1:04d}-05-31"
                elif lvl == "D":
                    est_date = f"{calc_year + 2:04d}-10-31"
                elif lvl == "R":
                    est_date = f"{calc_year + 3:04d}-10-31"
                elif lvl == "P":
                    est_date = f"{calc_year + 2:04d}-10-31"
                else:
                    est_date = f"{calc_year + 3:04d}-10-31"

                await self._conn.execute(
                    "UPDATE verifications SET card_expiry_date = ? WHERE discord_user_id = ?",
                    (est_date, u_id),
                )
                backfilled_count += 1

            if backfilled_count > 0:
                logger.info(
                    f"Backfilled estimated card_expiry_date for {backfilled_count} legacy student verification record(s)."
                )
        except Exception as e:
            logger.debug(f"Legacy card_expiry_date migration notice: {e}")

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

    async def create_backup(self, backup_dir: str = "backups", max_backups: int = 10) -> str:
        """
        Creates a consistent, point-in-time point-and-restore snapshot of the database
        even while WAL writes are occurring, and rotates older backups so only the
        `max_backups` most recent backups are kept.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        os.makedirs(backup_dir, exist_ok=True)
        timestamp = now_formatted(fmt="%Y%m%d_%H%M%S")
        backup_filename = f"tarveri_backup_{timestamp}.db"
        backup_path = os.path.join(backup_dir, backup_filename)

        if os.path.exists(backup_path):
            os.remove(backup_path)

        # VACUUM INTO safely creates an atomic copy of active database
        safe_path = backup_path.replace("'", "''")
        await self._conn.execute(f"VACUUM INTO '{safe_path}';")

        # Rotate older backups keeping only the most recent max_backups
        if max_backups > 0:
            rotate_backups(backup_dir=backup_dir, max_backups=max_backups)

        return backup_path

    def list_backups(self, backup_dir: str = "backups") -> list[dict[str, Any]]:
        """Instance helper to list available database backups."""
        return list_backups(backup_dir=backup_dir)

    async def restore_guild_settings_from_backup(
        self, backup_path: str, guild_id: int | None = None
    ) -> dict[str, Any]:
        """
        Restores guild_settings from a specified backup database into the current active database.
        If guild_id is provided, only that guild's settings are restored; otherwise all guilds are restored.
        Returns a dictionary summarizing the restored settings.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        if not os.path.isfile(backup_path):
            raise FileNotFoundError(f"Backup file not found at '{backup_path}'.")

        restored_guilds = 0
        details: list[dict[str, Any]] = []

        async with aiosqlite.connect(backup_path) as b_conn:
            cursor = await b_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_settings';"
            )
            if not await cursor.fetchone():
                return {"restored_guilds": 0, "details": [], "message": "No guild_settings table found in backup."}

            query = (
                "SELECT guild_id, welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name, updated_at "
                "FROM guild_settings"
            )
            params: tuple = ()
            if guild_id is not None:
                query += " WHERE guild_id = ?"
                params = (guild_id,)

            cursor = await b_conn.execute(query, params)
            rows = await cursor.fetchall()

            for row in rows:
                g_id, w_id, h_id, g_role, r_id, adm_role, u_at = row
                await self._conn.execute(
                    """
                    INSERT INTO guild_settings (guild_id, welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        welcome_channel_id = excluded.welcome_channel_id,
                        help_channel_id = excluded.help_channel_id,
                        guest_role_name = excluded.guest_role_name,
                        review_channel_id = excluded.review_channel_id,
                        admin_role_name = excluded.admin_role_name,
                        updated_at = excluded.updated_at;
                    """,
                    (g_id, w_id, h_id, g_role, r_id, adm_role, u_at or now_formatted()),
                )
                restored_guilds += 1
                details.append(
                    {
                        "guild_id": g_id,
                        "welcome_channel_id": w_id,
                        "help_channel_id": h_id,
                        "guest_role_name": g_role,
                        "review_channel_id": r_id,
                        "admin_role_name": adm_role,
                    }
                )

            await self._conn.commit()

        return {"restored_guilds": restored_guilds, "details": details}

    async def restore_latest_guild_settings(
        self, guild_id: int | None = None, backup_dir: str = "backups"
    ) -> dict[str, Any] | None:
        """Restores guild settings from the newest available backup file in backup_dir."""
        backups = self.list_backups(backup_dir=backup_dir)
        if not backups:
            return None
        latest = backups[0]
        result = await self.restore_guild_settings_from_backup(latest["path"], guild_id=guild_id)
        result["backup_file"] = latest["filename"]
        result["backup_path"] = latest["path"]
        return result

    async def restore_full_database(self, backup_path: str) -> None:
        """
        Restores the entire active database from a backup snapshot.
        Safely closes active connection, replaces file, and reconnects.
        """
        import shutil

        if not os.path.isfile(backup_path):
            raise FileNotFoundError(f"Backup file not found at '{backup_path}'.")

        await self.close()
        shutil.copy2(backup_path, self.path)

        wal_file = f"{self.path}-wal"
        shm_file = f"{self.path}-shm"
        for f in (wal_file, shm_file):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

        await self.connect()

    async def record_bot_created_role(self, guild_id: int, role_id: int, role_name: str) -> None:
        """Records a role created by the bot so it can be distinguished from admin-created roles."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        try:
            g_id = int(guild_id)
            r_id = int(role_id)
            r_name = str(role_name)
        except (ValueError, TypeError):
            return
        ts = now_formatted()
        await self._conn.execute(
            """
            INSERT INTO bot_created_roles (guild_id, role_id, role_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(role_id) DO UPDATE SET
                role_name = excluded.role_name,
                created_at = excluded.created_at;
            """,
            (g_id, r_id, r_name, ts),
        )
        await self._conn.commit()

    async def get_bot_created_role_ids(self, guild_id: int) -> set[int]:
        """Returns set of role IDs in a guild that were created by the bot."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        try:
            g_id = int(guild_id)
        except (ValueError, TypeError):
            return set()
        cursor = await self._conn.execute(
            "SELECT role_id FROM bot_created_roles WHERE guild_id = ?",
            (g_id,),
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def delete_bot_created_role(self, role_id: int) -> None:
        """Deletes a role tracking entry after the role is deleted."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        try:
            r_id = int(role_id)
        except (ValueError, TypeError):
            return
        await self._conn.execute(
            "DELETE FROM bot_created_roles WHERE role_id = ?",
            (r_id,),
        )
        await self._conn.commit()

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

        ts = now_formatted()
        g_id = None
        g_name = None
        if guild is not None:
            try:
                g_id = int(guild.id)
            except (ValueError, TypeError, AttributeError):
                g_id = None
            try:
                g_name = str(guild.name)
            except (ValueError, TypeError, AttributeError):
                g_name = None

        u_id = None
        if user_id is not None:
            try:
                u_id = int(user_id)
            except (ValueError, TypeError, AttributeError):
                u_id = None

        try:
            await self._conn.execute(
                """INSERT INTO audit_log
                   (timestamp, level, event_type, guild_id, guild_name, user_id, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    level,
                    event_type,
                    g_id,
                    g_name,
                    u_id,
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
        self,
        discord_user_id: int,
        student_id_hash: str,
        faculty_code: str,
        campus_code: str | None = None,
        level_code: str | None = None,
        card_expiry_date: str | None = None,
        lifecycle_prompt_status: str = "ACTIVE",
    ) -> None:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        await self._conn.execute(
            """INSERT INTO verifications (
                   discord_user_id, student_id_hash, faculty_code, verified_at,
                   campus_code, level_code, card_expiry_date, lifecycle_prompt_status
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                discord_user_id,
                student_id_hash,
                faculty_code,
                ts,
                campus_code,
                level_code,
                card_expiry_date,
                lifecycle_prompt_status,
            ),
        )
        await self._conn.commit()

    async def get_verification_details(self, discord_user_id: int) -> dict[str, Any] | None:
        """Retrieves complete verification details (faculty, campus, level, expiry, alumni status) for a user."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT student_id_hash, faculty_code, verified_at, is_alumni, graduated_year,
                      programme, graduated_at, campus_code, level_code, card_expiry_date,
                      lifecycle_prompt_status, last_lifecycle_prompt_at
               FROM verifications WHERE discord_user_id = ?""",
            (discord_user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "student_id_hash": row[0],
            "faculty_code": row[1],
            "verified_at": row[2],
            "is_alumni": bool(row[3]) if row[3] is not None else False,
            "graduated_year": row[4],
            "programme": row[5],
            "graduated_at": row[6],
            "campus_code": row[7],
            "level_code": row[8],
            "card_expiry_date": row[9],
            "lifecycle_prompt_status": row[10] or "ACTIVE",
            "last_lifecycle_prompt_at": row[11],
        }

    async def backfill_legacy_verifications(self, default_campus: str = "W") -> int:
        """Backfills legacy verifications missing campus_code to the specified campus code."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """UPDATE verifications
               SET campus_code = ?
               WHERE campus_code IS NULL""",
            (default_campus,),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def update_verification_details(
        self,
        discord_user_id: int,
        campus_code: str | None = None,
        level_code: str | None = None,
        card_expiry_date: str | None = None,
        lifecycle_prompt_status: str | None = None,
        last_lifecycle_prompt_at: str | None = None,
    ) -> bool:
        """Updates optional fields for an existing verified student."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        updates: list[str] = []
        params: list[Any] = []
        if campus_code is not None:
            updates.append("campus_code = ?")
            params.append(campus_code)
        if level_code is not None:
            updates.append("level_code = ?")
            params.append(level_code)
        if card_expiry_date is not None:
            updates.append("card_expiry_date = ?")
            params.append(card_expiry_date)
        if lifecycle_prompt_status is not None:
            updates.append("lifecycle_prompt_status = ?")
            params.append(lifecycle_prompt_status)
        if last_lifecycle_prompt_at is not None:
            updates.append("last_lifecycle_prompt_at = ?")
            params.append(last_lifecycle_prompt_at)
        if not updates:
            return False
        params.append(discord_user_id)
        sql = f"UPDATE verifications SET {', '.join(updates)} WHERE discord_user_id = ?"
        cursor = await self._conn.execute(sql, tuple(params))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def record_academic_transition(
        self,
        discord_user_id: int,
        from_id_hash: str,
        from_faculty_code: str,
        from_campus_code: str,
        from_level_code: str,
        to_id_hash: str,
        to_faculty_code: str,
        to_campus_code: str,
        to_level_code: str,
        notes: str | None = None,
    ) -> int:
        """Archives a student's previous academic level/faculty profile into transition history."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        cursor = await self._conn.execute(
            """INSERT INTO verification_transitions (
                   discord_user_id, from_id_hash, from_faculty_code, from_campus_code, from_level_code,
                   to_id_hash, to_faculty_code, to_campus_code, to_level_code, transitioned_at, notes
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                discord_user_id,
                from_id_hash,
                from_faculty_code,
                from_campus_code,
                from_level_code,
                to_id_hash,
                to_faculty_code,
                to_campus_code,
                to_level_code,
                ts,
                notes,
            ),
        )
        await self._conn.commit()
        return cursor.lastrowid

    async def get_academic_transitions_for_user(
        self, discord_user_id: int
    ) -> list[dict[str, Any]]:
        """Retrieves full academic progression history for a student."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT id, discord_user_id, from_id_hash, from_faculty_code, from_campus_code, from_level_code,
                      to_id_hash, to_faculty_code, to_campus_code, to_level_code, transitioned_at, notes
               FROM verification_transitions
               WHERE discord_user_id = ?
               ORDER BY id ASC""",
            (discord_user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "discord_user_id": r[1],
                "from_id_hash": r[2],
                "from_faculty_code": r[3],
                "from_campus_code": r[4],
                "from_level_code": r[5],
                "to_id_hash": r[6],
                "to_faculty_code": r[7],
                "to_campus_code": r[8],
                "to_level_code": r[9],
                "transitioned_at": r[10],
                "notes": r[11],
            }
            for r in rows
        ]

    async def update_verification_profile(
        self,
        discord_user_id: int,
        student_id_hash: str | None = None,
        faculty_code: str | None = None,
        campus_code: str | None = None,
        level_code: str | None = None,
        card_expiry_date: str | None = None,
        lifecycle_prompt_status: str | None = None,
        last_lifecycle_prompt_at: str | None = None,
    ) -> bool:
        """Updates the active verification record during an academic level transition or lifecycle prompt update."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        updates: list[str] = []
        params: list[Any] = []

        if student_id_hash is not None:
            updates.append("student_id_hash = ?")
            params.append(student_id_hash)
            updates.append("is_alumni = 0")
            updates.append("graduated_year = NULL")
            updates.append("programme = NULL")
            updates.append("graduated_at = NULL")
            updates.append("verified_at = ?")
            params.append(now_formatted())

        if faculty_code is not None:
            updates.append("faculty_code = ?")
            params.append(faculty_code)

        if campus_code is not None:
            updates.append("campus_code = ?")
            params.append(campus_code)

        if level_code is not None:
            updates.append("level_code = ?")
            params.append(level_code)

        if card_expiry_date is not None:
            updates.append("card_expiry_date = ?")
            params.append(card_expiry_date)

        if lifecycle_prompt_status is not None:
            updates.append("lifecycle_prompt_status = ?")
            params.append(lifecycle_prompt_status)

        if last_lifecycle_prompt_at is not None:
            updates.append("last_lifecycle_prompt_at = ?")
            params.append(last_lifecycle_prompt_at)

        if not updates:
            return False

        params.append(discord_user_id)
        sql = f"UPDATE verifications SET {', '.join(updates)} WHERE discord_user_id = ?"
        cursor = await self._conn.execute(sql, tuple(params))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get_expired_student_verifications(
        self, before_date: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Retrieves active verified students (is_alumni = 0) whose card_expiry_date is on or before before_date.
        Defaults before_date to today (YYYY-MM-DD in Asia/Kuala_Lumpur).
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        if not before_date:
            from tarveri.config import get_configured_tz
            now_dt = datetime.now(get_configured_tz())
            before_date = now_dt.strftime("%Y-%m-%d")

        cursor = await self._conn.execute(
            """SELECT discord_user_id, student_id_hash, faculty_code, campus_code, level_code,
                      card_expiry_date, lifecycle_prompt_status, last_lifecycle_prompt_at, verified_at
               FROM verifications
               WHERE is_alumni = 0
                 AND card_expiry_date IS NOT NULL
                 AND card_expiry_date <= ?
               ORDER BY card_expiry_date ASC""",
            (before_date,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "discord_user_id": r[0],
                "student_id_hash": r[1],
                "faculty_code": r[2],
                "campus_code": r[3],
                "level_code": r[4],
                "card_expiry_date": r[5],
                "lifecycle_prompt_status": r[6] or "ACTIVE",
                "last_lifecycle_prompt_at": r[7],
                "verified_at": r[8],
            }
            for r in rows
        ]

    async def record_alumni_claim(
        self,
        discord_user_id: int,
        graduated_year: int,
        programme: str | None = None,
    ) -> bool:
        """Records a verified student's transition to alumni status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        cursor = await self._conn.execute(
            """UPDATE verifications
               SET is_alumni = 1, graduated_year = ?, programme = ?, graduated_at = ?
               WHERE discord_user_id = ?""",
            (graduated_year, programme, ts, discord_user_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def revoke_alumni_status(self, discord_user_id: int) -> bool:
        """Revokes alumni status from a student in the database."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """UPDATE verifications
               SET is_alumni = 0, graduated_year = NULL, programme = NULL, graduated_at = NULL
               WHERE discord_user_id = ?""",
            (discord_user_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get_alumni_info_by_user(self, discord_user_id: int) -> dict[str, Any] | None:
        """Retrieves alumni details for a user if they have claimed alumni status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT is_alumni, graduated_year, programme, graduated_at, faculty_code, verified_at
               FROM verifications WHERE discord_user_id = ?""",
            (discord_user_id,),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        return {
            "is_alumni": bool(row[0]),
            "graduated_year": row[1],
            "programme": row[2],
            "graduated_at": row[3],
            "faculty_code": row[4],
            "verified_at": row[5],
        }

    async def get_all_alumni_user_ids(self) -> list[int]:
        """Returns a list of all user IDs with active alumni status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT discord_user_id FROM verifications WHERE is_alumni = 1"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def count_alumni(self) -> int:
        """Returns total count of registered alumni students."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute("SELECT COUNT(*) FROM verifications WHERE is_alumni = 1")
        row = await cursor.fetchone()
        return row[0] if row else 0

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

    async def get_all_verifications(self) -> list[tuple[int, str, str, str]]:
        """Returns all verified student records as (discord_user_id, student_id_hash, faculty_code, verified_at)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT discord_user_id, student_id_hash, faculty_code, verified_at FROM verifications"
        )
        return await cursor.fetchall()

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
        cutoff = (datetime.now(get_configured_tz()) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM verifications WHERE verified_at >= ?",
            (cutoff,),
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
    ) -> tuple[int | None, int | None, str | None, int | None, str | None] | None:
        """Returns (welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name) for the given guild, or None."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name
               FROM guild_settings WHERE guild_id = ?""",
            (guild_id,),
        )
        return await cursor.fetchone()

    async def set_guild_welcome_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Sets or clears the welcome channel ID for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
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
        ts = now_formatted()
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
        ts = now_formatted()
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
        ts = now_formatted()
        await self._conn.execute(
            """INSERT INTO guild_settings (guild_id, review_channel_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   review_channel_id = excluded.review_channel_id,
                   updated_at = excluded.updated_at""",
            (guild_id, channel_id, ts),
        )
        await self._conn.commit()

    async def set_guild_admin_role(self, guild_id: int, admin_role_name: str | None) -> None:
        """Sets or clears the custom admin role name for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        role_to_set = admin_role_name.strip() if admin_role_name else None
        await self._conn.execute(
            """INSERT INTO guild_settings (guild_id, admin_role_name, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   admin_role_name = excluded.admin_role_name,
                   updated_at = excluded.updated_at""",
            (guild_id, role_to_set, ts),
        )
        await self._conn.commit()

    async def clear_stale_channel_setting(self, guild_id: int, channel_type: str) -> bool:
        """
        Clears a deleted or invalid channel setting (welcome, help, or review) from guild_settings.
        Returns True if a setting was successfully cleared.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        col_map = {
            "welcome": "welcome_channel_id",
            "welcome_channel_id": "welcome_channel_id",
            "help": "help_channel_id",
            "help_channel_id": "help_channel_id",
            "review": "review_channel_id",
            "review_channel_id": "review_channel_id",
            "admin": "admin_role_name",
            "admin_role_name": "admin_role_name",
        }
        normalized = channel_type.strip().lower()
        target_col = col_map.get(normalized)
        if not target_col:
            raise ValueError(f"Invalid channel/setting type: {channel_type}")

        ts = now_formatted()
        cursor = await self._conn.execute(
            f"""UPDATE guild_settings
                SET {target_col} = NULL, updated_at = ?
                WHERE guild_id = ? AND {target_col} IS NOT NULL""",
            (ts, guild_id),
        )
        await self._conn.commit()
        cleared = cursor.rowcount > 0
        if cleared:
            await self.log(
                "INFO",
                "STALE_SETTING_CLEARED",
                f"Cleared stale guild setting '{target_col}' for guild ID {guild_id}",
            )
        return cleared


    async def create_referral_code(
        self, code: str, guild_id: int, referrer_discord_id: int, expires_at: str
    ) -> None:
        """Saves a newly generated referral code."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
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
        ts = now_formatted()
        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM referral_codes
               WHERE guild_id = ? AND referrer_discord_id = ?
               AND status IN ('ACTIVE', 'PENDING_APPROVAL')
               AND expires_at > ?""",
            (guild_id, referrer_discord_id, ts),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_successful_referrals_by_user(self, referrer_discord_id: int) -> int:
        """Counts total guest referrals successfully used/approved across all guilds for a student."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM referral_codes WHERE referrer_discord_id = ? AND status = 'USED'",
            (referrer_discord_id,),
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
        ts = now_formatted()
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

    async def get_next_guild_ticket_seq(self, guild_id: int) -> int:
        """Returns the next sequence number (1-indexed) for guest tickets in the given guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT COALESCE(MAX(ticket_seq), COUNT(*), 0) FROM guest_tickets WHERE guild_id = ?""",
            (guild_id,),
        )
        row = await cursor.fetchone()
        current_max = row[0] if row and row[0] is not None else 0
        return current_max + 1

    def _row_to_ticket(self, r: tuple) -> dict[str, Any]:
        """Maps a guest_tickets DB row tuple to a structured dictionary."""
        return {
            "ticket_id": r[0],
            "guild_id": r[1],
            "applicant_id": r[2],
            "referrer_id": r[3],
            "channel_id": r[4],
            "referral_code": r[5],
            "reason": r[6],
            "vouch_note": r[7],
            "vouched_by_id": r[8],
            "vouched_at": r[9],
            "status": r[10],
            "created_at": r[11],
            "closed_at": r[12],
            "closed_by_admin_id": r[13],
            "close_reason": r[14],
            "ticket_seq": r[15] if len(r) > 15 and r[15] is not None else r[0],
            "pinged_admin_ids": r[16] if len(r) > 16 else None,
            "last_pinged_at": r[17] if len(r) > 17 else None,
        }

    async def create_guest_ticket(
        self,
        guild_id: int,
        applicant_id: int,
        channel_id: int,
        referrer_id: int | None = None,
        referral_code: str | None = None,
        reason: str | None = None,
        ticket_seq: int | None = None,
        pinged_admin_ids: str | None = None,
        last_pinged_at: str | None = None,
    ) -> int:
        """Creates a guest ticket record and returns its ticket_id."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        seq = ticket_seq if ticket_seq is not None else await self.get_next_guild_ticket_seq(guild_id)
        cursor = await self._conn.execute(
            """INSERT INTO guest_tickets
               (guild_id, ticket_seq, applicant_id, referrer_id, channel_id, referral_code, reason, status, created_at, pinged_admin_ids, last_pinged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
            (guild_id, seq, applicant_id, referrer_id, channel_id, referral_code, reason, ts, pinged_admin_ids, last_pinged_at or ts),
        )
        await self._conn.commit()
        return cursor.lastrowid or 0

    async def get_guest_ticket_by_channel(self, channel_id: int) -> dict[str, Any] | None:
        """Fetches guest ticket by thread/channel ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets WHERE channel_id = ?""",
            (channel_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_guest_ticket_by_id(self, ticket_id: int) -> dict[str, Any] | None:
        """Fetches guest ticket by ticket ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets WHERE ticket_id = ?""",
            (ticket_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_open_guest_ticket_for_applicant(
        self, guild_id: int, applicant_id: int
    ) -> dict[str, Any] | None:
        """Checks if the user already has an active open ticket in this guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets
               WHERE guild_id = ? AND applicant_id = ? AND status = 'OPEN'""",
            (guild_id, applicant_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_latest_guest_ticket_for_user(
        self, guild_id: int, applicant_id: int
    ) -> dict[str, Any] | None:
        """Fetches the newest guest ticket for an applicant in this guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets
               WHERE guild_id = ? AND applicant_id = ?
               ORDER BY ticket_id DESC LIMIT 1""",
            (guild_id, applicant_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_open_guest_tickets(self) -> list[dict[str, Any]]:
        """Fetches all tickets with status 'OPEN' across all guilds for escalation monitoring."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets WHERE status = 'OPEN' ORDER BY ticket_id ASC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_ticket(r) for r in rows]

    async def update_guest_ticket_escalation(
        self, ticket_id: int, pinged_admin_ids: str, last_pinged_at: str | None = None
    ) -> bool:
        """Updates the list of pinged admin IDs and last pinged timestamp for a ticket."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = last_pinged_at or now_formatted()
        cursor = await self._conn.execute(
            """UPDATE guest_tickets
               SET pinged_admin_ids = ?, last_pinged_at = ?
               WHERE ticket_id = ?""",
            (pinged_admin_ids, ts, ticket_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def update_guest_ticket_vouch(
        self, ticket_id: int, vouch_note: str, vouched_by_id: int | None = None
    ) -> bool:
        """Saves a student vouch statement along with the voucher ID and timestamp on a guest ticket."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        cursor = await self._conn.execute(
            """UPDATE guest_tickets
               SET vouch_note = ?, vouched_by_id = ?, vouched_at = ?
               WHERE ticket_id = ?""",
            (vouch_note, vouched_by_id, ts, ticket_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def close_guest_ticket(
        self,
        ticket_id: int,
        status: str,
        closed_by_admin_id: int | None = None,
        close_reason: str | None = None,
        only_if_open: bool = False,
    ) -> bool:
        """Closes a guest ticket with status ('APPROVED', 'REJECTED', 'EXPIRED'), admin ID, and reason/comment."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        where_clause = "WHERE ticket_id = ? AND status = 'OPEN'" if only_if_open else "WHERE ticket_id = ?"
        cursor = await self._conn.execute(
            f"""UPDATE guest_tickets
               SET status = ?, closed_at = ?, closed_by_admin_id = ?, close_reason = ?
               {where_clause}""",
            (status, ts, closed_by_admin_id, close_reason, ticket_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def list_guest_tickets(
        self, guild_id: int, status: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Returns recent guest tickets for a guild, optionally filtered by status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        if status:
            cursor = await self._conn.execute(
                """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                          reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                          closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
                   FROM guest_tickets
                   WHERE guild_id = ? AND status = ?
                   ORDER BY ticket_id DESC LIMIT ?""",
                (guild_id, status.upper(), limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                          reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                          closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
                   FROM guest_tickets
                   WHERE guild_id = ?
                   ORDER BY ticket_id DESC LIMIT ?""",
                (guild_id, limit),
            )
        rows = await cursor.fetchall()
        return [self._row_to_ticket(r) for r in rows]

    async def cleanup_expired_referrals(self) -> int:
        """Bulk updates all expired active referral codes to EXPIRED status."""
        if not self._conn:
            return 0
        ts = now_formatted()
        cursor = await self._conn.execute(
            """UPDATE referral_codes SET status = 'EXPIRED'
               WHERE status = 'ACTIVE' AND expires_at <= ?""",
            (ts,),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def revoke_guest_tickets_for_user(
        self, guild_id: int, user_id: int, status: str = "REVOKED", close_reason: str | None = None
    ) -> int:
        """Revokes all active or approved guest tickets for a user in a guild."""
        if not self._conn:
            return 0
        ts = now_formatted()
        cursor = await self._conn.execute(
            """UPDATE guest_tickets
               SET status = ?, closed_at = ?, close_reason = ?
               WHERE guild_id = ? AND applicant_id = ? AND status IN ('OPEN', 'APPROVED')""",
            (status, ts, close_reason, guild_id, user_id),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def revoke_active_referrals_for_user(
        self, guild_id: int, user_id: int, status: str = "REVOKED"
    ) -> int:
        """Revokes all active referral codes generated by a user in a guild."""
        if not self._conn:
            return 0
        cursor = await self._conn.execute(
            """UPDATE referral_codes
               SET status = ?
               WHERE guild_id = ? AND referrer_discord_id = ? AND status = 'ACTIVE'""",
            (status, guild_id, user_id),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def cancel_open_tickets_referred_by_user(
        self, guild_id: int, referrer_id: int, close_reason: str = "Referring student left or was removed from server"
    ) -> int:
        """Cancels open review tickets where the referring student left or was banned."""
        if not self._conn:
            return 0
        ts = now_formatted()
        cursor = await self._conn.execute(
            """UPDATE guest_tickets
               SET status = 'REVOKED', closed_at = ?, close_reason = ?
               WHERE guild_id = ? AND referrer_id = ? AND status = 'OPEN'""",
            (ts, close_reason, guild_id, referrer_id),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def get_all_active_referrals(self) -> list[dict[str, Any]]:
        """Fetches all referral codes with status 'ACTIVE' or 'PENDING_APPROVAL' for maintenance reconciliation."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT code, guild_id, referrer_discord_id, created_at, expires_at,
                      used_by_discord_id, used_at, status
               FROM referral_codes WHERE status IN ('ACTIVE', 'PENDING_APPROVAL')"""
        )
        rows = await cursor.fetchall()
        return [
            {
                "code": r[0],
                "guild_id": r[1],
                "referrer_discord_id": r[2],
                "created_at": r[3],
                "expires_at": r[4],
                "used_by_discord_id": r[5],
                "used_at": r[6],
                "status": r[7],
            }
            for r in rows
        ]


