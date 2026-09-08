"""
TARVeri — Discord student verification bot for TARUMT.
"""

from __future__ import annotations

from tarveri.bot import TARVeriBot, run_bot
from tarveri.config import (
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    Settings,
    hash_student_id,
    mask_student_id,
    setup_logger,
    validate_student_id,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService

__version__ = "2.0.0"

__all__ = [
    "__version__",
    "Database",
    "FACULTY_ROLES",
    "FACULTY_ROLE_NAMES",
    "GuestService",
    "RateLimiter",
    "Settings",
    "TARVeriBot",
    "VerificationService",
    "hash_student_id",
    "mask_student_id",
    "run_bot",
    "setup_logger",
    "validate_student_id",
]

