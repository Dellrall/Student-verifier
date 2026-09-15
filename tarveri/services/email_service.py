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
from email.header import Header
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

    def _render_html_template(self, otp_code: str, server_name: str, ttl_minutes: int) -> str:
        """Renders ultra-clean, minimalist TARUMT verification email."""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TARVeri Code</title>
</head>
<body style="margin: 0; padding: 0; background-color: #F8FAFC; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; -webkit-font-smoothing: antialiased; color: #0F172A;">
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #F8FAFC; padding: 40px 12px;">
        <tr>
            <td align="center">
                <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 440px; background-color: #FFFFFF; border-radius: 12px; border: 1px solid #E2E8F0; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04); overflow: hidden;">
                    
                    <!-- Subtle Accent Line -->
                    <tr>
                        <td height="4" style="background-color: #C8102E; line-height: 4px; font-size: 1px;">&nbsp;</td>
                    </tr>

                    <!-- Card Body -->
                    <tr>
                        <td style="padding: 32px 32px 28px 32px;">
                            <!-- University Header -->
                            <div style="margin-bottom: 24px;">
                                <span style="font-size: 16px; font-weight: 800; color: #0F172A; letter-spacing: 0.5px;">TARUMT</span>
                                <span style="font-size: 13px; font-weight: 500; color: #64748B; margin-left: 4px;">Student Verification</span>
                            </div>

                            <h1 style="margin: 0 0 8px 0; font-size: 20px; font-weight: 700; color: #0F172A; letter-spacing: -0.2px;">
                                Verification code
                            </h1>
                            <p style="margin: 0 0 20px 0; font-size: 14px; line-height: 20px; color: #475569;">
                                Enter this code to verify your student status on <strong>{server_name}</strong>:
                            </p>

                            <!-- Modern OTP Box -->
                            <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 20px;">
                                <tr>
                                    <td align="center" style="background-color: #F1F5F9; border-radius: 8px; padding: 16px 20px;">
                                        <div style="font-size: 32px; font-weight: 700; letter-spacing: 6px; color: #C8102E; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;">
                                            {otp_code}
                                        </div>
                                    </td>
                                </tr>
                            </table>

                            <p style="margin: 0; font-size: 13px; color: #64748B;">
                                This code expires in <strong>{ttl_minutes} minutes</strong>.
                            </p>
                        </td>
                    </tr>

                    <!-- Minimal Footer -->
                    <tr>
                        <td style="padding: 16px 32px; background-color: #F8FAFC; border-top: 1px solid #E2E8F0;">
                            <p style="margin: 0; font-size: 11px; line-height: 16px; color: #94A3B8;">
                                If you did not request this code, you can safely ignore this email.
                            </p>
                        </td>
                    </tr>

                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""

    def _send_to_smtp_endpoint(
        self,
        to_email: str,
        otp_code: str,
        server_name: str,
        ttl_minutes: int,
        host: str,
        port: int,
        user: str,
        password: str,
        from_email: str,
        from_name: str,
        use_tls: bool,
        relay_label: str = "SMTP",
    ) -> tuple[bool, str | None]:
        """Transmits an email to a specific SMTP endpoint. Returns (success, error_message)."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"{otp_code} is your TARVeri verification code"
        clean_name = (from_name or "").strip().strip('"').strip("'")
        clean_from_email = (from_email or "").strip().strip('"').strip("'")
        if clean_name:
            msg["From"] = email.utils.formataddr((str(Header(clean_name, "utf-8")), clean_from_email))
        else:
            msg["From"] = clean_from_email
        msg["To"] = to_email.strip()
        msg["Date"] = email.utils.formatdate(localtime=True)
        domain = clean_from_email.split("@")[-1] if "@" in clean_from_email else "tarveri.local"
        msg["Message-ID"] = email.utils.make_msgid(domain=domain)

        text_content = (
            f"Hello TARUMT Student,\n\n"
            f"Your TARVeri verification code for '{server_name}' is: {otp_code}\n\n"
            f"This code will expire in {ttl_minutes} minutes.\n"
            f"If you did not request this verification, please ignore this email.\n\n"
            f"— TARVeri Verification System"
        )
        html_content = self._render_html_template(otp_code, server_name, ttl_minutes)

        msg.attach(MIMEText(text_content, "plain", "utf-8"))
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        try:
            if port == 465:
                server_ctx = smtplib.SMTP_SSL(host=host, port=port, timeout=12.0)
            else:
                server_ctx = smtplib.SMTP(host=host, port=port, timeout=12.0)

            with server_ctx as server:
                server.ehlo()
                if use_tls and port != 465:
                    server.starttls()
                    server.ehlo()
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
            return True, None
        except Exception as e:
            return False, str(e)

    def _send_smtp_sync(
        self,
        to_email: str,
        otp_code: str,
        server_name: str,
        ttl_minutes: int,
    ) -> bool:
        """
        Synchronous SMTP worker with primary transmission and automatic fallback.
        If the primary relay (e.g. SMTP2GO) fails (quota exceeded, connection error, etc.),
        it automatically fails over to the direct email server SMTP.
        """
        # 1. Attempt Primary SMTP Delivery (e.g. SMTP2GO relay)
        primary_ok, primary_err = self._send_to_smtp_endpoint(
            to_email=to_email,
            otp_code=otp_code,
            server_name=server_name,
            ttl_minutes=ttl_minutes,
            host=self.settings.smtp_host,
            port=self.settings.smtp_port,
            user=self.settings.smtp_user,
            password=self.settings.smtp_password,
            from_email=self.settings.smtp_from_email,
            from_name=self.settings.smtp_from_name,
            use_tls=self.settings.smtp_use_tls,
            relay_label="Primary SMTP",
        )
        if primary_ok:
            return True

        # 2. Check if Fallback Direct SMTP Server is configured
        fallback_host = self.settings.smtp_fallback_host.strip()
        if not fallback_host:
            logger.error(
                f"Primary SMTP delivery failed to {mask_email(to_email)} via {self.settings.smtp_host}:{self.settings.smtp_port}: {primary_err}. "
                "No fallback SMTP configured."
            )
            return False

        logger.warning(
            f"Primary SMTP relay ({self.settings.smtp_host}) failed ({primary_err}). "
            f"Failing over to fallback email server SMTP ({fallback_host}:{self.settings.smtp_fallback_port})..."
        )

        fallback_from_email = self.settings.smtp_fallback_from_email.strip() or self.settings.smtp_from_email
        fallback_from_name = self.settings.smtp_fallback_from_name.strip() or self.settings.smtp_from_name

        fallback_ok, fallback_err = self._send_to_smtp_endpoint(
            to_email=to_email,
            otp_code=otp_code,
            server_name=server_name,
            ttl_minutes=ttl_minutes,
            host=fallback_host,
            port=self.settings.smtp_fallback_port,
            user=self.settings.smtp_fallback_user,
            password=self.settings.smtp_fallback_password,
            from_email=fallback_from_email,
            from_name=fallback_from_name,
            use_tls=self.settings.smtp_fallback_use_tls,
            relay_label="Fallback Direct SMTP",
        )
        if fallback_ok:
            logger.info(
                f"Fallback direct SMTP delivery SUCCEEDED to {mask_email(to_email)} via {fallback_host}:{self.settings.smtp_fallback_port}"
            )
            return True

        logger.error(
            f"Fallback SMTP delivery also failed to {mask_email(to_email)} via {fallback_host}:{self.settings.smtp_fallback_port}: {fallback_err}"
        )
        return False
