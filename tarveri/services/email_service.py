"""
Asynchronous email service for student verification via SMTP OTP and AES-256 authenticated encryption.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import secrets
import smtplib
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from tarveri.config import (
    Settings,
    decrypt_email,
    encrypt_email,
    hash_email,
    is_valid_student_email,
    mask_email,
)

logger = logging.getLogger("tarveri")


@dataclass(slots=True)
class PendingOtp:
    """In-memory transient state for pending OTP email verification."""
    user_id: int
    student_id: str
    email: str
    otp_code: str
    card_expiry_date: str | None
    created_at: float
    expires_at: float
    attempts_left: int
    last_sent_at: float
    server_name: str


class EmailService:
    """
    Manages institutional student email verification:
    - Generates 6-digit cryptographically secure OTPs
    - Asynchronously transmits branded HTML verification emails via SMTP (e.g. SMTP2GO)
    - Validates OTP submissions with attempt limits and resend cooldowns
    - Encrypts/decrypts student emails at rest with AES-256 (Fernet)
    """

    def __init__(self, settings: Settings, mock_smtp: bool = False) -> None:
        self.settings = settings
        self.mock_smtp = mock_smtp
        self._pending_otps: dict[int, PendingOtp] = {}
        self._lock = asyncio.Lock()
        # Test helper hook for inspecting sent emails during test runs
        self.sent_emails: list[dict[str, Any]] = []

    @property
    def is_enabled(self) -> bool:
        return self.settings.enable_email_verification

    def is_email_valid(self, email_address: str) -> bool:
        """Checks format and ensures domain is within allowed institutional domains."""
        return is_valid_student_email(
            email_address, allowed_domains=self.settings.email_allowed_domains
        )

    def encrypt_student_email(self, email_address: str) -> str:
        """Encrypts email with configured AES-256 key."""
        return encrypt_email(email_address, self.settings.email_encryption_key)

    def decrypt_student_email(self, ciphertext: str) -> str:
        """Decrypts encrypted email string."""
        return decrypt_email(ciphertext, self.settings.email_encryption_key)

    def hash_student_email(self, email_address: str) -> str:
        """Produces deterministic HMAC-SHA256 blind index hash."""
        return hash_email(email_address, self.settings.id_hash_secret)

    async def generate_and_send_otp(
        self,
        user_id: int,
        student_id: str,
        email_address: str,
        server_name: str,
        card_expiry_date: str | None = None,
    ) -> dict[str, Any]:
        """
        Generates a 6-digit OTP, stores transient pending state, and dispatches the email.
        Returns a result dict: {'success': bool, 'error': str | None, 'ttl_seconds': int}.
        """
        email_clean = email_address.strip().lower()
        if not self.is_email_valid(email_clean):
            allowed = ", ".join(f"@{d}" for d in self.settings.email_allowed_domains)
            return {
                "success": False,
                "error": f"Invalid student email address. Please use your official institutional email ({allowed}).",
                "ttl_seconds": 0,
            }

        now = time.monotonic()
        async with self._lock:
            existing = self._pending_otps.get(user_id)
            if existing:
                cooldown_remaining = (
                    existing.last_sent_at
                    + self.settings.email_otp_resend_cooldown_seconds
                    - now
                )
                if cooldown_remaining > 0:
                    return {
                        "success": False,
                        "error": f"Please wait {int(cooldown_remaining)} seconds before requesting a new verification code.",
                        "ttl_seconds": int(existing.expires_at - now),
                    }

            # Generate secure 6-digit OTP code
            otp_code = "".join(secrets.choice("0123456789") for _ in range(6))
            ttl = self.settings.email_otp_ttl_seconds
            expires_at = now + ttl

            pending = PendingOtp(
                user_id=user_id,
                student_id=student_id,
                email=email_clean,
                otp_code=otp_code,
                card_expiry_date=card_expiry_date,
                created_at=now,
                expires_at=expires_at,
                attempts_left=self.settings.email_otp_max_attempts,
                last_sent_at=now,
                server_name=server_name,
            )
            self._pending_otps[user_id] = pending

        # Send email in background thread to avoid blocking asyncio event loop
        send_success = await self._send_otp_email_async(
            to_email=email_clean,
            otp_code=otp_code,
            server_name=server_name,
            ttl_minutes=max(1, ttl // 60),
        )

        if not send_success:
            async with self._lock:
                self._pending_otps.pop(user_id, None)
            return {
                "success": False,
                "error": "Failed to transmit verification email via SMTP server. Please notify server staff.",
                "ttl_seconds": 0,
            }

        logger.info(
            f"Verification OTP successfully sent to {mask_email(email_clean)} for user ID {user_id}"
        )
        return {
            "success": True,
            "error": None,
            "ttl_seconds": ttl,
        }

    async def verify_otp(
        self,
        user_id: int,
        submitted_code: str,
    ) -> dict[str, Any]:
        """
        Validates the submitted OTP.
        Returns: {'success': bool, 'error': str | None, 'pending': PendingOtp | None}
        """
        now = time.monotonic()
        async with self._lock:
            pending = self._pending_otps.get(user_id)
            if not pending:
                return {
                    "success": False,
                    "error": "No pending verification code found. Please run `/verify` to request a new code.",
                    "pending": None,
                }

            if now > pending.expires_at:
                self._pending_otps.pop(user_id, None)
                return {
                    "success": False,
                    "error": "Your verification code has expired. Please run `/verify` again to receive a fresh code.",
                    "pending": None,
                }

            if pending.attempts_left <= 0:
                self._pending_otps.pop(user_id, None)
                return {
                    "success": False,
                    "error": "Too many incorrect attempts. For security, this verification code has been invalidated. Please run `/verify` again.",
                    "pending": None,
                }

            clean_code = submitted_code.strip()
            if not secrets.compare_digest(pending.otp_code, clean_code):
                pending.attempts_left -= 1
                if pending.attempts_left <= 0:
                    self._pending_otps.pop(user_id, None)
                    return {
                        "success": False,
                        "error": "❌ Incorrect verification code (0 attempts remaining. Code invalidated). Please run `/verify` to request a fresh code.",
                        "pending": None,
                    }
                attempts_str = f"{pending.attempts_left} attempt{'s' if pending.attempts_left != 1 else ''} remaining"
                return {
                    "success": False,
                    "error": f"❌ Incorrect verification code ({attempts_str}). Please double check your email and try again.",
                    "pending": None,
                }

            # Verification successful — pop pending record
            self._pending_otps.pop(user_id, None)
            return {
                "success": True,
                "error": None,
                "pending": pending,
            }

    async def cancel_pending_otp(self, user_id: int) -> None:
        """Discards any active pending OTP for a user."""
        async with self._lock:
            self._pending_otps.pop(user_id, None)

    def get_pending_otp(self, user_id: int) -> PendingOtp | None:
        """Returns active pending OTP object if not expired."""
        pending = self._pending_otps.get(user_id)
        if pending and time.monotonic() <= pending.expires_at:
            return pending
        return None

    async def _send_otp_email_async(
        self,
        to_email: str,
        otp_code: str,
        server_name: str,
        ttl_minutes: int,
    ) -> bool:
        """Executes SMTP transmission in a worker thread."""
        if self.mock_smtp or not self.settings.smtp_user or not self.settings.smtp_password:
            # Mock / Developer Mode: Log code without real SMTP transmission
            logger.info(
                f"[MOCK_SMTP] Sent verification email to {to_email} with OTP: {otp_code} for {server_name}"
            )
            self.sent_emails.append({
                "to": to_email,
                "otp": otp_code,
                "server": server_name,
                "timestamp": time.time(),
            })
            return True

        return await asyncio.to_thread(
            self._send_smtp_sync,
            to_email=to_email,
            otp_code=otp_code,
            server_name=server_name,
            ttl_minutes=ttl_minutes,
        )

    def _send_smtp_sync(
        self,
        to_email: str,
        otp_code: str,
        server_name: str,
        ttl_minutes: int,
    ) -> bool:
        """Synchronous SMTP worker with TLS and error handling."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"{otp_code} is your TARVeri verification code"
        msg["From"] = email.utils.formataddr((
            self.settings.smtp_from_name,
            self.settings.smtp_from_email,
        ))
        msg["To"] = to_email
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=self.settings.smtp_from_email.split("@")[-1] if "@" in self.settings.smtp_from_email else "tarveri.local")

        text_content = (
            f"Hello TARUMT Student,\n\n"
            f"Your TARVeri verification code for '{server_name}' is: {otp_code}\n\n"
            f"This code will expire in {ttl_minutes} minutes.\n"
            f"If you did not request this verification, please ignore this email.\n\n"
            f"— TARVeri Verification System"
        )

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TARVeri Verification Code</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0d1117; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #c9d1d9;">
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #0d1117; padding: 40px 10px;">
        <tr>
            <td align="center">
                <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 520px; background-color: #161b22; border-radius: 12px; border: 1px solid #30363d; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
                    <!-- Header -->
                    <tr>
                        <td style="padding: 30px 40px; background: linear-gradient(135deg, #1f6feb 0%, #1158c7 100%); text-align: center;">
                            <div style="font-size: 32px; line-height: 32px; margin-bottom: 8px;">🎓</div>
                            <h1 style="margin: 0; font-size: 22px; font-weight: 700; color: #ffffff; letter-spacing: 0.5px;">TARVeri Verification</h1>
                            <p style="margin: 6px 0 0 0; font-size: 13px; color: rgba(255,255,255,0.85);">{server_name}</p>
                        </td>
                    </tr>
                    <!-- Body -->
                    <tr>
                        <td style="padding: 35px 40px;">
                            <p style="margin: 0 0 16px 0; font-size: 15px; line-height: 24px; color: #e6edf3;">
                                Hello TARUMT Student,
                            </p>
                            <p style="margin: 0 0 25px 0; font-size: 14px; line-height: 22px; color: #8b949e;">
                                Use the verification code below to verify your student status on Discord and receive your official faculty role:
                            </p>
                            
                            <!-- OTP Code Box -->
                            <div style="background-color: #0d1117; border: 1px solid #388bfd; border-radius: 8px; padding: 18px 20px; text-align: center; margin: 0 0 25px 0;">
                                <span style="font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, Courier, monospace; font-size: 34px; font-weight: 800; letter-spacing: 8px; color: #58a6ff;">{otp_code}</span>
                            </div>

                            <p style="margin: 0 0 12px 0; font-size: 13px; line-height: 20px; color: #8b949e;">
                                ⏱️ This one-time code expires in <strong>{ttl_minutes} minutes</strong>.
                            </p>
                            <p style="margin: 0; font-size: 12px; line-height: 18px; color: #6e7681;">
                                🔒 Never share this code with anyone. TARVeri staff will never ask for your verification code.
                            </p>
                        </td>
                    </tr>
                    <!-- Footer -->
                    <tr>
                        <td style="padding: 20px 40px; background-color: #0d1117; border-top: 1px solid #21262d; text-align: center;">
                            <p style="margin: 0; font-size: 11px; color: #484f58;">
                                Tunku Abdul Rahman University of Management and Technology (TARUMT)<br>
                                Automated Student & Guest Verification Bot
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""

        msg.attach(MIMEText(text_content, "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        try:
            with smtplib.SMTP(
                host=self.settings.smtp_host,
                port=self.settings.smtp_port,
                timeout=12.0,
            ) as server:
                server.ehlo()
                if self.settings.smtp_use_tls:
                    server.starttls()
                    server.ehlo()
                if self.settings.smtp_user and self.settings.smtp_password:
                    server.login(self.settings.smtp_user, self.settings.smtp_password)
                server.send_message(msg)
            return True
        except Exception as e:
            logger.error(f"SMTP delivery failed to {to_email} via {self.settings.smtp_host}:{self.settings.smtp_port}: {e}")
            return False
