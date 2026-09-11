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

# Alumni role configurations
ALUMNI_ROLE_NAME: Final[str] = "TARUMT Alumni"
ALUMNI_ROLE_COLOR: Final[int] = 0xD4AF37  # Academic Gold (#D4AF37)
ALUMNI_ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(alumni|graduates?|alumnus|alumna|graduate|graduated)\b",
    re.IGNORECASE,
)
ALUMNI_ALIASES: Final[list[str]] = [
    "TARUMT Alumni",
    "TAR UMT Alumni",
    "Alumni",
    "TARUC Alumni",
    "TAR UC Alumni",
    "TARC Alumni",
    "TAR College Alumni",
    "Alumni TARUMT",
    "TARUMT Graduate",
    "TARUMT Graduates",
    "TARUMT Graduated",
    "Graduated",
    "Graduate",
    "Graduates",
    "Alumnus",
    "Alumna",
    "Alumni Member",
    "Alumni Members",
]

# Branch campus code mapping (index 2 of student ID) -> Role Name
CAMPUS_ROLES: Final[dict[str, str]] = {
    "W": "KL Main Campus",
    "P": "Penang Branch",
    "A": "Perak Branch",
    "J": "Johor Branch",
    "C": "Pahang Branch",
    "K": "Pahang Branch",
    "S": "Sabah Branch",
}
CAMPUS_ROLE_NAMES: Final[set[str]] = set(CAMPUS_ROLES.values())

CAMPUS_ALIASES: Final[dict[str, list[str]]] = {
    "KL Main Campus": [
        "KL Main Campus",
        "KL Campus",
        "Kuala Lumpur Campus",
        "Kuala Lumpur Main Campus",
        "Main Campus",
        "KL",
        "Setapak Campus",
    ],
    "Penang Branch": [
        "Penang Branch",
        "Penang Campus",
        "Penang Branch Campus",
        "Pulau Pinang Campus",
        "Pulau Pinang Branch",
        "Penang",
    ],
    "Perak Branch": [
        "Perak Branch",
        "Perak Campus",
        "Perak Branch Campus",
        "Kampar Campus",
        "Kampar Branch",
        "Perak",
    ],
    "Johor Branch": [
        "Johor Branch",
        "Johor Campus",
        "Johor Branch Campus",
        "Segamat Campus",
        "Segamat Branch",
        "Johor",
    ],
    "Pahang Branch": [
        "Pahang Branch",
        "Pahang Campus",
        "Pahang Branch Campus",
        "Kuantan Campus",
        "Kuantan Branch",
        "Pahang",
    ],
    "Sabah Branch": [
        "Sabah Branch",
        "Sabah Campus",
        "Sabah Branch Campus",
        "Kota Kinabalu Campus",
        "Kota Kinabalu Branch",
        "Sabah",
    ],
}

CAMPUS_COLORS: Final[dict[str, int]] = {
    "KL Main Campus": 0x3498DB,  # Sky Blue (#3498DB)
    "Penang Branch": 0x1ABC9C,   # Turquoise (#1ABC9C)
    "Perak Branch": 0xE67E22,    # Orange (#E67E22)
    "Johor Branch": 0x9B59B6,    # Amethyst (#9B59B6)
    "Pahang Branch": 0x27AE60,   # Green (#27AE60)
    "Sabah Branch": 0xF39C12,    # Sun Yellow (#F39C12)
}

# Study level code mapping (index 4 of student ID) -> Role Name
STUDY_LEVEL_ROLES: Final[dict[str, str]] = {
    "D": "Diploma",
    "R": "Degree",
    "F": "Foundation",
    "P": "Postgraduate",
}
STUDY_LEVEL_ROLE_NAMES: Final[set[str]] = set(STUDY_LEVEL_ROLES.values())

STUDY_LEVEL_ALIASES: Final[dict[str, list[str]]] = {
    "Diploma": [
        "Diploma",
        "Diploma Student",
        "Diploma Students",
        "Dip",
    ],
    "Degree": [
        "Degree",
        "Degree Student",
        "Degree Students",
        "Bachelor",
        "Bachelor's Degree",
        "Bachelors Degree",
        "Undergraduate",
    ],
    "Foundation": [
        "Foundation",
        "Foundation Student",
        "Pre-U",
        "Pre-University",
    ],
    "Postgraduate": [
        "Postgraduate",
        "Postgrad",
        "Master",
        "Masters",
        "Master's",
        "PhD",
        "Doctorate",
    ],
}

STUDY_LEVEL_COLORS: Final[dict[str, int]] = {
    "Degree": 0x2980B9,        # Belize Blue (#2980B9)
    "Diploma": 0x16A085,       # Green Sea (#16A085)
    "Foundation": 0x8E44AD,    # Wisteria (#8E44AD)
    "Postgraduate": 0xD35400,  # Pumpkin (#D35400)
}

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
    logs_dir: str = "logs"
    log_file: str = "tarveri.log"
    log_max_bytes: int = 2_000_000
    log_backup_count: int = 5
    log_archive_days: int = 10
    enable_log_rotator: bool = True
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
    outage_alert_grace_seconds: int = 20
    enable_graduation_watchdog: bool = True
    graduation_check_interval_hours: int = 24
    graduation_prompt_cooldown_days: int = 7

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

        logs_dir = (
            os.getenv("TARVERI_LOGS_DIR")
            or os.getenv("LOGS_DIR")
            or "logs"
        ).strip()

        log_file = (
            os.getenv("TARVERI_LOG_FILE")
            or os.getenv("LOG_FILE")
            or "tarveri.log"
        ).strip()

        archive_days_raw = (
            os.getenv("TARVERI_LOG_ARCHIVE_DAYS")
            or os.getenv("LOG_ARCHIVE_DAYS")
            or "10"
        ).strip()
        log_archive_days = int(archive_days_raw) if archive_days_raw.isdigit() else 10

        enable_rotator_raw = (
            os.getenv("TARVERI_ENABLE_LOG_ROTATOR")
            or os.getenv("ENABLE_LOG_ROTATOR")
            or "true"
        ).lower().strip()
        enable_log_rotator = enable_rotator_raw in ("true", "1", "yes")

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

        outage_grace_raw = (
            os.getenv("TARVERI_OUTAGE_ALERT_GRACE_SECONDS")
            or os.getenv("OUTAGE_ALERT_GRACE_SECONDS")
            or "20"
        ).strip()
        outage_alert_grace_seconds = int(outage_grace_raw) if outage_grace_raw.isdigit() else 20

        enable_grad_raw = (
            os.getenv("TARVERI_ENABLE_GRADUATION_WATCHDOG")
            or os.getenv("ENABLE_GRADUATION_WATCHDOG")
            or "true"
        ).lower().strip()
        enable_graduation_watchdog = enable_grad_raw in ("true", "1", "yes")

        grad_interval_raw = (
            os.getenv("TARVERI_GRADUATION_CHECK_INTERVAL_HOURS")
            or os.getenv("GRADUATION_CHECK_INTERVAL_HOURS")
            or "24"
        ).strip()
        graduation_check_interval_hours = int(grad_interval_raw) if grad_interval_raw.isdigit() else 24

        grad_cooldown_raw = (
            os.getenv("TARVERI_GRADUATION_PROMPT_COOLDOWN_DAYS")
            or os.getenv("GRADUATION_PROMPT_COOLDOWN_DAYS")
            or "7"
        ).strip()
        graduation_prompt_cooldown_days = int(grad_cooldown_raw) if grad_cooldown_raw.isdigit() else 7

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
            logs_dir=logs_dir,
            log_file=log_file,
            log_archive_days=log_archive_days,
            enable_log_rotator=enable_log_rotator,
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
            outage_alert_grace_seconds=outage_alert_grace_seconds,
            enable_graduation_watchdog=enable_graduation_watchdog,
            graduation_check_interval_hours=graduation_check_interval_hours,
            graduation_prompt_cooldown_days=graduation_prompt_cooldown_days,
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


class DailyRotatingFileHandler(logging.Handler):
    """
    Timezone-aware daily rotating file handler that writes log records to date-separated
    files (e.g. logs/tarveri-YYYY-MM-DD.log) inside `logs_dir`.
    Automatically rolls over to a new daily log file when the date advances in the configured timezone.
    """

    def __init__(
        self,
        logs_dir: str = "logs",
        prefix: str = "tarveri",
        tz_name: str | None = None,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self.logs_dir = logs_dir
        self.prefix = prefix
        self.tz = get_configured_tz(tz_name)
        self.encoding = encoding
        self.current_date_str: str | None = None
        self._stream: Any = None
        self._current_file_path: str | None = None
        os.makedirs(self.logs_dir, exist_ok=True)

    @property
    def current_file_path(self) -> str | None:
        return self._current_file_path

    def _get_date_str(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created, tz=self.tz)
        return dt.strftime("%Y-%m-%d")

    def _open_stream(self, date_str: str) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
                self._stream.close()
            except Exception:
                pass
        self.current_date_str = date_str
        self._current_file_path = os.path.join(self.logs_dir, f"{self.prefix}-{date_str}.log")
        os.makedirs(self.logs_dir, exist_ok=True)
        self._stream = open(self._current_file_path, "a", encoding=self.encoding)

    def emit(self, record: logging.LogRecord) -> None:
        self.acquire()
        try:
            date_str = self._get_date_str(record)
            if self._stream is None or date_str != self.current_date_str:
                self._open_stream(date_str)
            msg = self.format(record)
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)
        finally:
            self.release()

    def flush(self) -> None:
        self.acquire()
        try:
            if self._stream is not None and hasattr(self._stream, "flush"):
                self._stream.flush()
        finally:
            self.release()

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                try:
                    self._stream.flush()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            super().close()
        finally:
            self.release()


def setup_logger(
    log_file: str = "tarveri.log",
    max_bytes: int = 2_000_000,
    backup_count: int = 5,
    tz_name: str | None = None,
    logs_dir: str = "logs",
) -> logging.Logger:
    """
    Sets up the application logger with daily file rotation in the logs folder
    and formatted console output.
    """
    logger = logging.getLogger("tarveri")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = TimezoneFormatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        tz_name=tz_name,
    )

    # Determine effective logs directory and log file prefix
    if os.path.dirname(log_file):
        effective_logs_dir = os.path.dirname(log_file)
        base = os.path.basename(log_file)
        prefix = base.rsplit(".", 1)[0] if "." in base else base
    else:
        effective_logs_dir = logs_dir
        base = log_file
        prefix = base.rsplit(".", 1)[0] if "." in base else base
        if not prefix:
            prefix = "tarveri"

    file_handler = DailyRotatingFileHandler(
        logs_dir=effective_logs_dir,
        prefix=prefix,
        tz_name=tz_name,
        encoding="utf-8",
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


@dataclass(frozen=True, slots=True)
class StudentIdInfo:
    """Detailed parsed components of a TARUMT student ID."""
    is_valid: bool
    student_id: str
    faculty_code: str | None
    faculty_role: str | None
    campus_code: str | None = None
    campus_role: str | None = None
    level_code: str | None = None
    level_role: str | None = None


def parse_student_id(raw_id: str) -> StudentIdInfo:
    """
    Parses a student ID into detailed components:
    - Intake year (digits 0..1)
    - Branch Campus (character 2 -> CAMPUS_ROLES)
    - Faculty Code (character 3 -> FACULTY_ROLES)
    - Study Level (character 4 -> STUDY_LEVEL_ROLES)
    - Registration Sequence (digits 5..9)
    """
    normalized = raw_id.strip().upper().replace("-", "").replace(" ", "")
    if not STUDENT_ID_PATTERN.match(normalized):
        return StudentIdInfo(
            is_valid=False,
            student_id=normalized,
            faculty_code=None,
            faculty_role=None,
        )

    campus_code = normalized[2] if len(normalized) > 2 else None
    faculty_code = normalized[3] if len(normalized) > 3 else None
    level_code = normalized[4] if len(normalized) > 4 else None

    campus_role = CAMPUS_ROLES.get(campus_code) if campus_code else None
    faculty_role = FACULTY_ROLES.get(faculty_code) if faculty_code else None
    level_role = STUDY_LEVEL_ROLES.get(level_code) if level_code else None

    if not faculty_role:
        return StudentIdInfo(
            is_valid=False,
            student_id=normalized,
            faculty_code=faculty_code,
            faculty_role=None,
            campus_code=campus_code,
            campus_role=campus_role,
            level_code=level_code,
            level_role=level_role,
        )

    return StudentIdInfo(
        is_valid=True,
        student_id=normalized,
        faculty_code=faculty_code,
        faculty_role=faculty_role,
        campus_code=campus_code,
        campus_role=campus_role,
        level_code=level_code,
        level_role=level_role,
    )


def validate_student_id(raw_id: str) -> tuple[bool, str, str | None, str | None]:
    """
    Validates and parses a student ID.
    Returns (is_valid, normalized_id, faculty_code, faculty_role_name) for 100% backwards compatibility.
    """
    info = parse_student_id(raw_id)
    return info.is_valid, info.student_id, info.faculty_code, info.faculty_role


import calendar


def parse_card_expiry_date(raw_date: str | None) -> str | None:
    """
    Parses and normalizes student card expiry date strings into ISO format (YYYY-MM-DD).
    Supports formats:
    - MM/YY (e.g. '10/26' -> '2026-10-31')
    - MM/YYYY (e.g. '10/2026' -> '2026-10-31')
    - MM-YY / MM-YYYY
    - YYYY-MM (e.g. '2026-10' -> '2026-10-31')
    - YYYY-MM-DD (e.g. '2026-10-31')
    - Month Year (e.g. 'OCT 2026' or 'October 2026')
    """
    if not raw_date:
        return None

    cleaned = raw_date.strip().upper()
    if not cleaned:
        return None

    # 1. Check ISO full date YYYY-MM-DD
    iso_full_match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", cleaned)
    if iso_full_match:
        try:
            year = int(iso_full_match.group(1))
            month = int(iso_full_match.group(2))
            day = int(iso_full_match.group(3))
            max_days = calendar.monthrange(year, month)[1]
            if 1 <= month <= 12 and 1 <= day <= max_days:
                return f"{year:04d}-{month:02d}-{day:02d}"
        except Exception:
            return None

    # 2. Check YYYY-MM
    iso_year_month = re.match(r"^(\d{4})[-/](\d{1,2})$", cleaned)
    if iso_year_month:
        try:
            year = int(iso_year_month.group(1))
            month = int(iso_year_month.group(2))
            if 1 <= month <= 12:
                last_day = calendar.monthrange(year, month)[1]
                return f"{year:04d}-{month:02d}-{last_day:02d}"
        except Exception:
            return None

    # 3. Check MM/YY or MM/YYYY (or with hyphens/dots)
    m_y_match = re.match(r"^(\d{1,2})[-/\.](\d{2}|\d{4})$", cleaned)
    if m_y_match:
        try:
            month = int(m_y_match.group(1))
            raw_year = int(m_y_match.group(2))
            year = (2000 + raw_year if raw_year < 70 else 1900 + raw_year) if raw_year < 100 else raw_year
            if 1 <= month <= 12 and 1969 <= year <= 2100:
                last_day = calendar.monthrange(year, month)[1]
                return f"{year:04d}-{month:02d}-{last_day:02d}"
        except Exception:
            return None

    # 4. Check Month Name Year (e.g. 'OCT 2026', 'OCTOBER 26')
    month_names = {
        "JAN": 1, "JANUARY": 1,
        "FEB": 2, "FEBRUARY": 2,
        "MAR": 3, "MARCH": 3,
        "APR": 4, "APRIL": 4,
        "MAY": 5,
        "JUN": 6, "JUNE": 6,
        "JUL": 7, "JULY": 7,
        "AUG": 8, "AUGUST": 8,
        "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
        "OCT": 10, "OCTOBER": 10,
        "NOV": 11, "NOVEMBER": 11,
        "DEC": 12, "DECEMBER": 12,
    }
    month_text_match = re.match(r"^([A-Z]{3,9})\s+(\d{2}|\d{4})$", cleaned)
    if month_text_match:
        m_str = month_text_match.group(1)
        raw_year = int(month_text_match.group(2))
        month = month_names.get(m_str)
        year = (2000 + raw_year if raw_year < 70 else 1900 + raw_year) if raw_year < 100 else raw_year
        if month and 1969 <= year <= 2100:
            last_day = calendar.monthrange(year, month)[1]
            return f"{year:04d}-{month:02d}-{last_day:02d}"

    return None


def format_card_expiry_display(iso_date: str | None) -> str | None:
    """Formats an ISO date (YYYY-MM-DD) as 'MM/YY' for card rendering."""
    if not iso_date:
        return None
    try:
        parts = iso_date.split("-")
        if len(parts) >= 2:
            year_short = parts[0][-2:]
            month = parts[1].zfill(2)
            return f"{month}/{year_short}"
    except Exception:
        pass
    return None


def estimate_student_card_expiry(student_id: str | None, level_code: str | None = None) -> str | None:
    """
    Intelligently estimates student card expiry date from the Student ID intake year and study level.
    Zero-effort automated fallback when the student does not provide an explicit card expiry date.
    Examples:
    - 23WMD09867 (Diploma, 2 yrs): Intake 2023 -> 2025-10-31
    - 24WMR12345 (Degree, 3 yrs): Intake 2024 -> 2027-10-31
    - 25WMF01234 (Foundation, 1 yr): Intake 2025 -> 2026-05-31
    - 23WMP00111 (Postgrad, 2 yrs): Intake 2023 -> 2025-10-31
    """
    if not student_id or len(student_id) < 5:
        return None
    raw_yy = student_id[:2]
    if not raw_yy.isdigit():
        return None
    intake_yy = int(raw_yy)
    intake_year = 2000 + intake_yy if intake_yy < 70 else 1900 + intake_yy

    lvl = (level_code or (student_id[4] if len(student_id) > 4 else "R")).upper()
    if lvl == "F":  # Foundation (1 year)
        return f"{intake_year + 1:04d}-05-31"
    elif lvl == "D":  # Diploma (2 years)
        return f"{intake_year + 2:04d}-10-31"
    elif lvl == "R":  # Degree (3 years typical)
        return f"{intake_year + 3:04d}-10-31"
    elif lvl == "P":  # Postgraduate (2 years typical)
        return f"{intake_year + 2:04d}-10-31"
    else:
        # Default 3 years
        return f"{intake_year + 3:04d}-10-31"




