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

# Pattern: 2 digits + 3 uppercase letters + 2 digits + 3 digits (e.g. 23WMD09867)
STUDENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{2}[A-Z]{3}\d{2}\d{3}$")

# Pattern matching role inquiries or help queries from members
ROLE_HELP_KEYWORDS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"how\s+(?:to|do\s+i)\s+get\s+(?:a\s+)?role|"
    r"how\s+(?:to|do\s+i)\s+verify|"
    r"where\s+(?:to|do\s+i)\s+verify|"
    r"get\s+role|"
    r"need\s+role|"
    r"give\s+role|"
    r"claim\s+role|"
    r"no\s+role|"
    r"faculty\s+role|"
    r"roles?"
    r")\b",
    re.IGNORECASE,
)


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

    @property
    def database_path(self) -> str:
        return self.db_path

    @property
    def rate_limit_requests(self) -> int:
        return self.rate_limit_max_attempts

    @classmethod
    def from_env(cls, validate: bool = True) -> Settings:
        bot_token = os.getenv("TARVERI_BOT_TOKEN", "")
        id_hash_secret = os.getenv("TARVERI_ID_HASH_SECRET", "")
        db_path = os.getenv("TARVERI_DB_PATH", "tarveri.db")
        admin_role_name = os.getenv("TARVERI_ADMIN_ROLE_NAME", "TARVeri Admin")
        log_file = os.getenv("TARVERI_LOG_FILE", "tarveri.log")

        hoster_id_raw = os.getenv("TARVERI_HOSTER_DISCORD_ID", "").strip()
        hoster_discord_id = int(hoster_id_raw) if hoster_id_raw.isdigit() else None

        enable_checker_raw = os.getenv("TARVERI_ENABLE_UPDATE_CHECKER", "true").lower()
        enable_update_checker = enable_checker_raw in ("true", "1", "yes")

        interval_raw = os.getenv("TARVERI_UPDATE_CHECK_INTERVAL_HOURS", "24").strip()
        update_check_interval_hours = int(interval_raw) if interval_raw.isdigit() else 24

        update_stream_raw = (
            os.getenv("TARVERI_UPDATE_STREAM")
            or os.getenv("TARVERI_UPDATE_BRANCH")
            or "auto"
        ).strip()
        update_stream = update_stream_raw if update_stream_raw else "auto"

        help_channel_raw = os.getenv("TARVERI_HELP_CHANNEL_ID", "").strip()
        help_channel_id = int(help_channel_raw) if help_channel_raw.isdigit() else None

        welcome_channel_raw = os.getenv("TARVERI_WELCOME_CHANNEL_ID", "").strip()
        welcome_channel_id = int(welcome_channel_raw) if welcome_channel_raw.isdigit() else None

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
        )


def setup_logger(log_file: str = "tarveri.log", max_bytes: int = 2_000_000, backup_count: int = 5) -> logging.Logger:
    logger = logging.getLogger("tarveri")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
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


