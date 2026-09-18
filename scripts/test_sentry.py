#!/usr/bin/env python3
"""
CLI Utility to test Sentry Real-Time Telemetry & Exception Dispatching.
Usage:
    python3 scripts/test_sentry.py
"""

import os
import sys

from dotenv import load_dotenv

# Ensure project root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

load_dotenv()

from tarveri.config import Settings


def main() -> None:
    print("=" * 60)
    print("🚨 TARVeri — Sentry Telemetry Verification Utility")
    print("=" * 60)

    try:
        settings = Settings.from_env(validate=False)
    except Exception as e:
        print(f"❌ Failed to parse settings from environment: {e}")
        sys.exit(1)

    dsn = settings.sentry_dsn.strip()
    if not dsn:
        print("⚠️  TARVERI_SENTRY_DSN is not configured in your .env file!")
        print("👉 Add your Sentry DSN to .env like this:")
        print("   TARVERI_SENTRY_DSN=https://yourPublicKey@o0.ingest.sentry.io/0\n")
        sys.exit(1)

    masked_dsn = dsn[:25] + "..." + dsn[-10:] if len(dsn) > 40 else dsn
    print(f"🔑 Using Sentry DSN: {masked_dsn}")

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=1.0,
            profiles_sample_rate=1.0,
            environment=os.getenv("TARVERI_ENVIRONMENT", "production-test"),
        )
        print("✅ Sentry SDK initialized successfully.")
    except Exception as e:
        print(f"❌ Failed to initialize sentry_sdk: {e}")
        sys.exit(1)

    print("\n⚡ Triggering deliberate ZeroDivisionError (1 / 0)...")
    try:
        # Deliberate division by zero
        pass
    except ZeroDivisionError as err:
        event_id = sentry_sdk.capture_exception(err)
        # Flush Sentry event buffer to ensure delivery before script exits
        sentry_sdk.flush(timeout=5.0)

        print("🎉 Exception successfully captured and transmitted to Sentry!")
        print(f"📋 Sentry Event ID: {event_id}")
        print("\n👉 Check your Sentry project dashboard: https://sentry.io")
        print("=" * 60)


if __name__ == "__main__":
    main()
