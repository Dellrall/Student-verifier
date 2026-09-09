"""
Configuration management, constants, and cryptographic/formatting utilities.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
import zoneinfo
from logging.handlers import RotatingFileHandler
from typing import Final

from dotenv import load_dotenv

load_dotenv()

# Faculty code mapping (index 3 of student ID) -> Role Name
FACULTY_ROLES: Final[dict[str, str]] = {
    "B": "FAFB",
    "K": "FCCI",
    "L": "FOAS",
    "J": "FSSH",
    "V": "FOBE",
    "P": "CPUS",
    "M": "FOCS",
    "G": "FOET",
}
FACULTY_ROLE_NAMES: Final[set[str]] = set(FACULTY_ROLES.values())

# Rich dynamic synonyms, expansions, and aliases for each faculty
FACULTY_ALIASES: Final[dict[str, list[str]]] = {
    "FAFB": [
        "Faculty of Accountancy, Finance and Business",
        "Faculty of Accountancy, Finance & Business",
        "Faculty of Accountancy",
        "Accountancy, Finance and Business",
        "Accountancy, Finance & Business",
        "Accountancy & Finance",
        "Accountancy",
        "Finance",
        "FAFB",
    ],
    "CPUS": [
        "Centre for Pre-University Studies",
        "Centre for Pre-U Studies",
        "Center for Pre-University Studies",
        "Pre-University Studies",
        "Pre-University",
        "Pre-U Studies",
        "Pre-U",
        "Foundation Studies",
        "Foundation",
        "CPUS",
    ],
    "FOCS": [
        "Faculty of Computing and Information Technology",
        "Faculty of Computing & Information Technology",
        "Faculty of Computing",
        "Computing and Information Technology",
        "Computing & Information Technology",
        "Computing & IT",
        "Computing",
        "Computer Science",
        "Information Technology",
        "FOCS",
        "FCIT",
    ],
    "FCCI": [
        "Faculty of Communication and Creative Industries",
        "Faculty of Communication & Creative Industries",
        "Faculty of Communication",
        "Communication and Creative Industries",
        "Communication & Creative Industries",
        "Creative Industries",
        "Communication",
        "FCCI",
    ],
    "FOAS": [
        "Faculty of Applied Sciences",
        "Faculty of Applied Science",
        "Applied Sciences",
        "Applied Science",
        "FOAS",
        "FAS",
    ],
    "FOBE": [
        "Faculty of Built Environment",
        "Built Environment",
        "Architecture",
        "Surveying",
        "FOBE",
    ],
    "FSSH": [
        "Faculty of Social Science and Humanities",
        "Faculty of Social Science & Humanities",
        "Faculty of Social Science",
        "Social Science and Humanities",
        "Social Science & Humanities",
        "Social Science",
        "Humanities",
        "FSSH",
        "FSS",
    ],
    "FOET": [
        "Faculty of Engineering and Technology",
        "Faculty of Engineering & Technology",
        "Faculty of Engineering",
        "Engineering and Technology",
        "Engineering & Technology",
        "Engineering",
        "FOET",
        "FOE",
    ],
}

# Faculty SRC (Student Representative Council) roles
SRC_ROLES: Final[dict[str, str]] = {
    "FAFB": "FAFB SRC",
    "CPUS": "CPUS SRC",
    "FOCS": "FOCS SRC",
    "FCCI": "FCCI SRC",
    "FOAS": "FOAS SRC",
    "FOBE": "FOBE SRC",
    "FSSH": "FSSH SRC",
    "FOET": "FOET SRC",
}
SRC_ROLE_NAMES: Final[set[str]] = set(SRC_ROLES.values())

# Organizational, council, and functional role qualifiers that must NEVER be matched as general faculty roles
ROLE_QUALIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(src|council|committee|exco|reps?|representatives?|staff|admins?|leads?|mentors?|tutors?|lecturers?|societ(?:y|ies)|clubs?|presidents?|vp|secretar(?:y|ies)|treasurers?|bureaus?|alumni|seniors?|juniors?|sub[\s\-_]*committee)\b",
    re.IGNORECASE,
)

# Dynamic pattern matching for guest and visitor roles
GUEST_ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(guests?|visitors?|external|non[\s\-_]*students?)\b",
    re.IGNORECASE,
)

# Default role colors matching server design palette
FACULTY_COLORS: Final[dict[str, int]] = {
    "FAFB": 0x992D22,  # Dark Red (#992D22)
    "CPUS": 0x1F8673,  # Dark Teal (#1F8673)
    "FOCS": 0xF1C40F,  # Yellow / Gold (#F1C40F)
    "FCCI": 0x71368A,  # Dark Purple (#71368A)
    "FOAS": 0xE74C3C,  # Red / Coral Red (#E74C3C)
    "FOBE": 0x2ECC71,  # Green / Emerald (#2ECC71)
    "FSSH": 0x3498DB,  # Blue (#3498DB)
    "FOET": 0xBAE973,  # Lime Green (#BAE973)
}
GUEST_ROLE_COLOR: Final[int] = 0x2ECC71  # Green / Emerald (#2ECC71)

# Pattern: 2 digits + 3 uppercase letters + 2 digits + 3 digits (e.g. 23WMD09867)
STUDENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{2}[A-Z]{3}\d{2}\d{3}$")

# Pattern matching role inquiries or help queries from members
ROLE_HELP_KEYWORDS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"help|bantuan|tolong|support|faq|"
    r"verif(?:y|ied|ication|ikasi)?|"
    r"roles?|faculty|faculty\s+role|"
    r"student(?:\s*id)?|matrik?|id\s+number|"
    r"guests?|referrals?|invite(?:\s*code)?|"
    r"tarveri"
    r")\b|"
    r"(?:how|where|macam\s+mana|camne|nak)\s+(?:to|do\s+i|can\s+i|nak)?\s*(?:get|join|verify|enter|claim|access)",
    re.IGNORECASE,
)


def get_configured_tz(tz_name: str | None = None) -> zoneinfo.ZoneInfo | timezone:
    """Resolves the configured timezone (defaults to Asia/Kuala_Lumpur or local system time)."""
    raw = (
        tz_name
        or os.getenv("TARVERI_TIMEZONE")
        or os.getenv("TIMEZONE")
        or os.getenv("TZ")
        or "Asia/Kuala_Lumpur"
    ).strip()
    if raw.lower() in ("auto", "local", "system", ""):
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return zoneinfo.ZoneInfo(raw)
    except Exception:
        return datetime.now().astimezone().tzinfo or timezone.utc


def now_formatted(tz_name: str | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Returns current timestamp string formatted in the configured timezone."""
    tz = get_configured_tz(tz_name)
    return datetime.now(tz).strftime(fmt)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    id_hash_secret: str
    db_path: str = "tarveri.db"
    admin_role_name: str = "TARVeri Admin"
    log_file: str = "tarveri.log"
    log_max_bytes: int = 2_000_000
    log_backup_count: int = 5
    rate_limit_max_attempts: int = 5
    rate_limit_window_seconds: int = 600
    hoster_discord_id: int | None = None
    enable_update_checker: bool = True
    update_check_interval_hours: int = 24
    update_stream: str = "auto"
    help_channel_id: int | None = None
    welcome_channel_id: int | None = None
    timezone_name: str = "Asia/Kuala_Lumpur"
    backup_dir: str = "backups"
    max_backups: int = 10
    enable_outage_watchdog: bool = True
    outage_timeout_seconds: int = 300
    outage_probe_interval_seconds: int = 15

    @property
    def database_path(self) -> str:
        return self.db_path

    @property
    def rate_limit_requests(self) -> int:
        return self.rate_limit_max_attempts

    @classmethod
    def from_env(cls, validate: bool = True) -> Settings:
        bot_token = (
            os.getenv("TARVERI_BOT_TOKEN")
            or os.getenv("DISCORD_BOT_TOKEN")
            or os.getenv("BOT_TOKEN")
            or os.getenv("DISCORD_TOKEN")
            or ""
        ).strip()

        id_hash_secret = (
            os.getenv("TARVERI_ID_HASH_SECRET")
            or os.getenv("ID_HASH_SECRET")
            or os.getenv("HASH_SECRET")
            or ""
        ).strip()

        db_path = (
            os.getenv("TARVERI_DB_PATH")
            or os.getenv("DB_PATH")
            or os.getenv("DATABASE_PATH")
            or "tarveri.db"
        ).strip()

        admin_role_name = (
            os.getenv("TARVERI_ADMIN_ROLE_NAME")
            or os.getenv("ADMIN_ROLE_NAME")
            or os.getenv("ADMIN_ROLE")
            or "TARVeri Admin"
        ).strip()

        log_file = (
            os.getenv("TARVERI_LOG_FILE")
            or os.getenv("LOG_FILE")
            or "tarveri.log"
        ).strip()

        hoster_id_raw = (
            os.getenv("TARVERI_HOSTER_DISCORD_ID")
            or os.getenv("HOSTER_DISCORD_ID")
            or os.getenv("HOSTER_ID")
            or ""
        ).strip()
        hoster_discord_id = int(hoster_id_raw) if hoster_id_raw.isdigit() else None

        enable_checker_raw = (
            os.getenv("TARVERI_ENABLE_UPDATE_CHECKER")
            or os.getenv("ENABLE_UPDATE_CHECKER")
            or "true"
        ).lower().strip()
        enable_update_checker = enable_checker_raw in ("true", "1", "yes")

        interval_raw = (
            os.getenv("TARVERI_UPDATE_CHECK_INTERVAL_HOURS")
            or os.getenv("UPDATE_CHECK_INTERVAL_HOURS")
            or "24"
        ).strip()
        update_check_interval_hours = int(interval_raw) if interval_raw.isdigit() else 24

        update_stream_raw = (
            os.getenv("TARVERI_UPDATE_STREAM")
            or os.getenv("TARVERI_UPDATE_BRANCH")
            or os.getenv("UPDATE_STREAM")
            or os.getenv("UPDATE_BRANCH")
            or "auto"
        ).strip()
        update_stream = update_stream_raw if update_stream_raw else "auto"

        help_channel_raw = (
            os.getenv("TARVERI_HELP_CHANNEL_ID")
            or os.getenv("HELP_CHANNEL_ID")
            or ""
        ).strip()
        help_channel_id = int(help_channel_raw) if help_channel_raw.isdigit() else None

        welcome_channel_raw = (
            os.getenv("TARVERI_WELCOME_CHANNEL_ID")
            or os.getenv("WELCOME_CHANNEL_ID")
            or ""
        ).strip()
        welcome_channel_id = int(welcome_channel_raw) if welcome_channel_raw.isdigit() else None

        timezone_name = (
            os.getenv("TARVERI_TIMEZONE")
            or os.getenv("TIMEZONE")
            or os.getenv("TZ")
            or "Asia/Kuala_Lumpur"
        ).strip()

        backup_dir = (
            os.getenv("TARVERI_BACKUP_DIR")
            or os.getenv("BACKUP_DIR")
            or "backups"
        ).strip()

        max_backups_raw = (
            os.getenv("TARVERI_MAX_BACKUPS")
            or os.getenv("MAX_BACKUPS")
            or "10"
        ).strip()
        max_backups = int(max_backups_raw) if max_backups_raw.isdigit() else 10

        enable_outage_raw = (
            os.getenv("TARVERI_ENABLE_OUTAGE_WATCHDOG")
            or os.getenv("ENABLE_OUTAGE_WATCHDOG")
            or "true"
        ).lower().strip()
        enable_outage_watchdog = enable_outage_raw in ("true", "1", "yes")

        outage_timeout_raw = (
            os.getenv("TARVERI_OUTAGE_TIMEOUT_SECONDS")
            or os.getenv("OUTAGE_TIMEOUT_SECONDS")
            or "300"
        ).strip()
        outage_timeout_seconds = int(outage_timeout_raw) if outage_timeout_raw.isdigit() else 300

        outage_probe_raw = (
            os.getenv("TARVERI_OUTAGE_PROBE_INTERVAL_SECONDS")
            or os.getenv("OUTAGE_PROBE_INTERVAL_SECONDS")
            or "15"
        ).strip()
        outage_probe_interval_seconds = int(outage_probe_raw) if outage_probe_raw.isdigit() else 15

        if validate:
            if not bot_token:
                raise RuntimeError(
                    "TARVERI_BOT_TOKEN is not set. Put it in a .env file or the environment."
                )
            if not id_hash_secret:
                raise RuntimeError(
                    "TARVERI_ID_HASH_SECRET is not set. Generate one with: "
                    '`python -c "import secrets; print(secrets.token_hex(32))"`'
                )

        return cls(
            bot_token=bot_token,
            id_hash_secret=id_hash_secret,
            db_path=db_path,
            admin_role_name=admin_role_name,
            log_file=log_file,
            hoster_discord_id=hoster_discord_id,
            enable_update_checker=enable_update_checker,
            update_check_interval_hours=update_check_interval_hours,
            update_stream=update_stream,
            help_channel_id=help_channel_id,
            welcome_channel_id=welcome_channel_id,
            timezone_name=timezone_name,
            backup_dir=backup_dir,
            max_backups=max_backups,
            enable_outage_watchdog=enable_outage_watchdog,
            outage_timeout_seconds=outage_timeout_seconds,
            outage_probe_interval_seconds=outage_probe_interval_seconds,
        )


class TimezoneFormatter(logging.Formatter):
    """Custom logging formatter that renders timestamps in the local/configured timezone."""

    def __init__(
        self,
        fmt: str = "%(asctime)s | %(levelname)s | %(message)s",
        datefmt: str = "%Y-%m-%d %H:%M:%S",
        tz_name: str | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.tz = get_configured_tz(tz_name)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=self.tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logger(
    log_file: str = "tarveri.log",
    max_bytes: int = 2_000_000,
    backup_count: int = 5,
    tz_name: str | None = None,
) -> logging.Logger:
    logger = logging.getLogger("tarveri")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = TimezoneFormatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        tz_name=tz_name,
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def hash_student_id(student_id: str, secret: str) -> str:
    """Deterministic HMAC-SHA256 hash — lets us detect duplicate IDs without
    storing the raw ID at rest."""
    return hmac.new(
        secret.encode("utf-8"), student_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mask_student_id(student_id: str) -> str:
    """For logs and displays: keep enough to be useful for support, not enough to be sensitive."""
    if len(student_id) >= 6:
        return f"{student_id[:2]}***{student_id[-3:]}"
    return "***"


def validate_student_id(raw_id: str) -> tuple[bool, str, str | None, str | None]:
    """
    Validates and parses a student ID.
    Returns (is_valid, normalized_id, faculty_code, faculty_role_name).
    """
    normalized = raw_id.strip().upper().replace("-", "").replace(" ", "")
    if not STUDENT_ID_PATTERN.match(normalized):
        return False, normalized, None, None

    faculty_code = normalized[3]
    faculty_role = FACULTY_ROLES.get(faculty_code)
    if not faculty_role:
        return False, normalized, faculty_code, None

    return True, normalized, faculty_code, faculty_role


