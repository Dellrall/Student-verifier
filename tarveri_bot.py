"""
TARVeri — Discord student verification bot.

Safety improvements over the original version:
1. Bot token loaded from environment (.env), never hardcoded.
2. Persistent SQLite database (aiosqlite) instead of stateless role-checking:
   - Prevents the same student ID being used to verify two different Discord accounts.
   - Prevents one Discord account being re-verified under a different student ID.
   - Survives bot restarts / role deletions with an audit trail.
3. Student IDs are stored as HMAC-SHA256 hashes, not plaintext — the bot can still
   detect duplicates (hashes are deterministic) without keeping raw IDs at rest.
4. Rate limiting on verification attempts (mitigates brute-forcing the ID format
   or spamming the bot).
5. Audit log written to BOTH a rotating file and a DB table, so history survives
   log rotation and is queryable.
6. Narrower exception handling — no bare `except discord.Forbidden` swallowing
   unrelated errors; explicit permission checks before attempting role creation.
"""

import os
import re
import hmac
import hashlib
import logging
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import aiosqlite
import discord
from discord.ext import commands
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config / secrets
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("TARVERI_BOT_TOKEN")
# Used to HMAC-hash student IDs at rest. Never log or print this.
ID_HASH_SECRET = os.getenv("TARVERI_ID_HASH_SECRET")
DB_PATH = os.getenv("TARVERI_DB_PATH", "tarveri.db")
# Name of the Discord role allowed to run admin commands (in addition to anyone
# with the server's Administrator permission).
ADMIN_ROLE_NAME = os.getenv("TARVERI_ADMIN_ROLE_NAME", "TARVeri Admin")

if not BOT_TOKEN:
    raise RuntimeError(
        "TARVERI_BOT_TOKEN is not set. Put it in a .env file or the environment "
        "— never hardcode it in source."
    )
if not ID_HASH_SECRET:
    raise RuntimeError(
        "TARVERI_ID_HASH_SECRET is not set. Generate one with, e.g., "
        "`python -c \"import secrets; print(secrets.token_hex(32))\"` and store it "
        "as an env var. This is used to hash student IDs at rest."
    )

# ---------------------------------------------------------------------------
# Logging (file + console). DB audit log is handled separately in Database.log()
# so log history survives file rotation and is queryable.
# ---------------------------------------------------------------------------
logger = logging.getLogger("tarveri")
logger.setLevel(logging.INFO)

_formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = RotatingFileHandler(
    "tarveri.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_formatter)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

logger.addHandler(_file_handler)
logger.addHandler(_console_handler)

# ---------------------------------------------------------------------------
# Static config
# ---------------------------------------------------------------------------
faculty_roles = {
    "B": "FAFB",
    "K": "FCCI",
    "L": "FOAS",
    "J": "FSSH",
    "V": "FOBE",
    "F": "CPUS",
    "M": "FOCS",
    "G": "FOET",
}
faculty_role_names = set(faculty_roles.values())

student_id_pattern = re.compile(r"^\d{2}[A-Z]{3}\d{2}\d{3}$")

# Rate limiting: max attempts per user within a time window
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 600  # 10 minutes
_attempt_log: dict[int, list[float]] = {}  # user_id -> list of unix timestamps


def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    attempts = [t for t in _attempt_log.get(user_id, []) if now - t < WINDOW_SECONDS]
    _attempt_log[user_id] = attempts
    return len(attempts) >= MAX_ATTEMPTS


def record_attempt(user_id: int) -> None:
    _attempt_log.setdefault(user_id, []).append(time.time())


def hash_student_id(student_id: str) -> str:
    """Deterministic HMAC hash — lets us detect duplicate IDs without storing
    the raw ID at rest."""
    return hmac.new(
        ID_HASH_SECRET.encode("utf-8"), student_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mask_student_id(student_id: str) -> str:
    """For logs: keep enough to be useful for support, not enough to be sensitive."""
    return student_id[:2] + "***" + student_id[-3:] if len(student_id) >= 6 else "***"


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        # WAL mode lets reads (e.g. admin queries on audit_log) proceed without
        # blocking on writes (verifications), which matters as guild count grows.
        await self._conn.execute("PRAGMA journal_mode = WAL;")
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
            """
        )
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def log(
        self,
        level: str,
        event_type: str,
        message: str,
        guild: discord.Guild | None = None,
        user_id: int | None = None,
    ):
        """Writes to both the DB audit table and the rotating log file."""
        ts = datetime.now(timezone.utc).isoformat()
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
        getattr(logger, level.lower(), logger.info)(f"[{event_type}] {message}")

    async def get_verification_by_user(self, discord_user_id: int):
        cursor = await self._conn.execute(
            "SELECT student_id_hash, faculty_code, verified_at FROM verifications "
            "WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        return await cursor.fetchone()

    async def get_verification_by_id_hash(self, student_id_hash: str):
        cursor = await self._conn.execute(
            "SELECT discord_user_id FROM verifications WHERE student_id_hash = ?",
            (student_id_hash,),
        )
        return await cursor.fetchone()

    async def record_verification(
        self, discord_user_id: int, student_id_hash: str, faculty_code: str
    ):
        await self._conn.execute(
            """INSERT INTO verifications (discord_user_id, student_id_hash, faculty_code, verified_at)
               VALUES (?, ?, ?, ?)""",
            (discord_user_id, student_id_hash, faculty_code, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    async def delete_verification(self, discord_user_id: int) -> bool:
        """Unlinks a Discord account from its student ID (e.g. for account
        transfers). Returns True if a record was actually deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM verifications WHERE discord_user_id = ?", (discord_user_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def total_verified(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) FROM verifications")
        row = await cursor.fetchone()
        return row[0]

    async def counts_by_faculty(self):
        cursor = await self._conn.execute(
            "SELECT faculty_code, COUNT(*) FROM verifications GROUP BY faculty_code ORDER BY faculty_code"
        )
        return await cursor.fetchall()

    async def verified_in_last(self, hours: int) -> int:
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM verifications WHERE verified_at >= datetime('now', ?)",
            (f"-{hours} hours",),
        )
        row = await cursor.fetchone()
        return row[0]

    async def recent_audit(self, limit: int = 10, event_type: str | None = None):
        if event_type:
            cursor = await self._conn.execute(
                "SELECT timestamp, level, event_type, guild_name, user_id, message "
                "FROM audit_log WHERE event_type = ? ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT timestamp, level, event_type, guild_name, user_id, message "
                "FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return await cursor.fetchall()


db = Database(DB_PATH)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await db.connect()
    await db.log(
        "INFO",
        "STARTUP",
        f"Logged in as {bot.user} (ID: {bot.user.id}); in {len(bot.guilds)} server(s): "
        f"{[g.name for g in bot.guilds]}",
    )


@bot.event
async def on_close():
    await db.close()


@bot.event
async def on_guild_join(guild):
    await db.log(
        "INFO", "GUILD_JOIN",
        f"Joined server '{guild.name}' (members: {guild.member_count})",
        guild=guild,
    )


@bot.event
async def on_guild_remove(guild):
    await db.log("INFO", "GUILD_REMOVE", f"Left/removed from server '{guild.name}'", guild=guild)


@bot.event
async def on_member_join(member):
    try:
        await member.send(
            "🎓 Welcome! Please enter your student ID (e.g., 23WMD09867) to get verified:"
        )
    except discord.Forbidden:
        await db.log(
            "WARNING", "DM_BLOCKED",
            f"Couldn't send welcome DM to {member} (ID: {member.id}) — DMs closed",
            guild=member.guild, user_id=member.id,
        )


@bot.command(name="verify")
async def verify_command(ctx):
    """Fallback for members who joined before the bot, or missed the welcome DM."""
    if ctx.guild is None:
        return
    try:
        await ctx.author.send(
            "🎓 Please enter your student ID (e.g., 23WMD09867) to get verified:"
        )
        await ctx.send(f"{ctx.author.mention} I've sent you a DM to continue verification.")
    except discord.Forbidden:
        await ctx.send(
            f"{ctx.author.mention} I couldn't DM you. Please enable DMs from server members and try again."
        )


async def assign_role_across_guilds(user_id: int, role_name: str, guilds: list[discord.Guild]):
    """Shared by fresh verification and self-heal resync: ensures the given
    user holds `role_name` in every guild passed, creating the role if needed.
    Returns (verified_in, already_had_role_in, missing_role_in, failed_in)."""
    verified_in, already_had_role_in, missing_role_in, failed_in = [], [], [], []

    for guild in guilds:
        member = guild.get_member(user_id)
        if member is None:
            continue

        existing_roles = [r for r in member.roles if r.name in faculty_role_names]
        if existing_roles:
            already_had_role_in.append((guild.name, existing_roles[0].name))
            continue

        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            if not guild.me.guild_permissions.manage_roles:
                missing_role_in.append(guild.name)
                continue
            try:
                role = await guild.create_role(
                    name=role_name,
                    reason="TARVeri: auto-created missing faculty role for verification",
                )
                await db.log("INFO", "ROLE_CREATED", f"Created role '{role_name}'", guild=guild)
            except discord.HTTPException as e:
                await db.log(
                    "ERROR", "ROLE_CREATE_FAILED",
                    f"Failed to create role '{role_name}' in '{guild.name}': {e}",
                    guild=guild,
                )
                missing_role_in.append(guild.name)
                continue

        if role >= guild.me.top_role or not guild.me.guild_permissions.manage_roles:
            failed_in.append(guild.name)
            continue

        try:
            await member.add_roles(role)
            verified_in.append((guild.name, role_name))
        except discord.HTTPException as e:
            failed_in.append(guild.name)
            await db.log(
                "ERROR", "ROLE_ASSIGN_FAILED",
                f"Failed to assign '{role_name}' to user {user_id} in '{guild.name}': {e}",
                guild=guild, user_id=user_id,
            )

    return verified_in, already_had_role_in, missing_role_in, failed_in


def format_role_summary(verified_in, already_had_role_in, missing_role_in, failed_in) -> str:
    lines = []
    if verified_in:
        lines.append("✅ You've been given the following role(s):")
        lines += [f"   • **{g}** → {r}" for g, r in verified_in]
    if already_had_role_in:
        lines.append("ℹ️ You already had a faculty role in:")
        lines += [f"   • **{g}** → {r} (unchanged)" for g, r in already_had_role_in]
    if missing_role_in:
        lines.append("⚠️ I couldn't create/find the required role (contact an admin) in:")
        lines += [f"   • **{g}** (I likely need 'Manage Roles' permission there)" for g in missing_role_in]
    if failed_in:
        lines.append("⚠️ I don't have permission to assign roles in:")
        lines += [f"   • **{g}** (my role needs to be moved above the faculty roles)" for g in failed_in]
    return "\n".join(lines)


async def process_verification(message: discord.Message):
    """Handles a student ID submitted via DM: validates format, enforces
    one-ID-per-account and one-account-per-ID, then assigns the faculty role
    across every mutual server."""
    user = message.author

    if is_rate_limited(user.id):
        await user.send(
            "⏳ You've made too many verification attempts. Please wait a few minutes "
            "and try again, or contact an admin if this is a mistake."
        )
        await db.log("WARNING", "RATE_LIMITED", f"{user} hit the attempt limit", user_id=user.id)
        return
    record_attempt(user.id)

    student_id = message.content.strip().upper()

    if not student_id_pattern.match(student_id):
        await user.send("❌ Invalid student ID format. Please use the format like `23WMD09867`.")
        return

    faculty_code = student_id[3]
    role_name = faculty_roles.get(faculty_code)
    if not role_name:
        await user.send("❌ Student ID does not match any known faculty. Please check and try again.")
        return

    id_hash = hash_student_id(student_id)

    # --- Already-verified accounts: resync instead of flatly refusing.
    # This is what makes joining a NEW server after being verified elsewhere
    # actually grant the role there, instead of silently doing nothing.
    existing_for_user = await db.get_verification_by_user(user.id)
    if existing_for_user:
        stored_hash, stored_faculty, _ = existing_for_user
        if stored_hash != id_hash:
            await user.send(
                "ℹ️ This Discord account is already verified under a different student ID. "
                "If you need to change the ID on file (e.g. account transfer), contact an admin."
            )
            return

        mutual_guilds = [g for g in bot.guilds if g.get_member(user.id)]
        result = await assign_role_across_guilds(user.id, faculty_roles[stored_faculty], mutual_guilds)
        summary = format_role_summary(*result)
        await user.send(summary or "ℹ️ You're already verified and up to date in every server I share with you.")
        return

    existing_for_id = await db.get_verification_by_id_hash(id_hash)
    if existing_for_id:
        await user.send(
            "❌ This student ID has already been used to verify a different Discord "
            "account. If that wasn't you, contact an admin immediately."
        )
        await db.log(
            "WARNING", "DUPLICATE_ID_ATTEMPT",
            f"{user} (ID: {user.id}) tried to reuse a student ID "
            f"(masked: {mask_student_id(student_id)}) already bound to another account",
            user_id=user.id,
        )
        return

    mutual_guilds = [g for g in bot.guilds if g.get_member(user.id)]
    if not mutual_guilds:
        await user.send(
            "⚠️ I couldn't find you in any server I'm in. Please join the server first, then try again."
        )
        return

    verified_in, already_had_role_in, missing_role_in, failed_in = [], [], [], []

    for guild in mutual_guilds:
        member = guild.get_member(user.id)

        existing_roles = [r for r in member.roles if r.name in faculty_role_names]
        if existing_roles:
            already_had_role_in.append((guild.name, existing_roles[0].name))
            continue

        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            if not guild.me.guild_permissions.manage_roles:
                missing_role_in.append(guild.name)
                continue
            try:
                role = await guild.create_role(
                    name=role_name,
                    reason="TARVeri: auto-created missing faculty role for verification",
                )
                await db.log("INFO", "ROLE_CREATED", f"Created role '{role_name}'", guild=guild)
            except discord.HTTPException as e:
                await db.log(
                    "ERROR", "ROLE_CREATE_FAILED",
                    f"Failed to create role '{role_name}' in '{guild.name}': {e}",
                    guild=guild,
                )
                missing_role_in.append(guild.name)
                continue

        if role >= guild.me.top_role or not guild.me.guild_permissions.manage_roles:
            failed_in.append(guild.name)
            continue

        try:
            await member.add_roles(role)
            verified_in.append((guild.name, role_name))
        except discord.HTTPException as e:
            failed_in.append(guild.name)
            await db.log(
                "ERROR", "ROLE_ASSIGN_FAILED",
                f"Failed to assign '{role_name}' to {user} in '{guild.name}': {e}",
                guild=guild, user_id=user.id,
            )

    # Only persist the verification if it succeeded in at least one server —
    # avoids permanently binding an ID to a user who was never actually verified.
    if verified_in:
        await db.record_verification(user.id, id_hash, faculty_code)
        await db.log(
            "INFO", "VERIFIED",
            f"{user} (ID: {user.id}) verified (student ID masked: {mask_student_id(student_id)}) "
            f"→ role '{role_name}' in {[g for g, _ in verified_in]}",
            user_id=user.id,
        )

    lines = []
    if verified_in:
        lines.append("✅ You've been verified and given the following role(s):")
        lines += [f"   • **{g}** → {r}" for g, r in verified_in]
    if already_had_role_in:
        lines.append("ℹ️ You already had a faculty role in:")
        lines += [f"   • **{g}** → {r} (unchanged)" for g, r in already_had_role_in]
    if missing_role_in:
        lines.append("⚠️ I couldn't create/find the required role (contact an admin) in:")
        lines += [f"   • **{g}** (I likely need 'Manage Roles' permission there)" for g in missing_role_in]
    if failed_in:
        lines.append("⚠️ I don't have permission to assign roles in:")
        lines += [f"   • **{g}** (my role needs to be moved above the faculty roles)" for g in failed_in]

    if lines:
        await user.send("\n".join(lines))


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None and not message.author.bot:
        if not message.content.startswith(bot.command_prefix):
            await process_verification(message)
    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
