#!/usr/bin/env python3
"""
Inspect per-server channel configuration in the SQLite database backend.
Usage: python scripts/show_servers.py [path_to_db]
"""

from __future__ import annotations

import os
import sqlite3
import sys


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TARVERI_DB_PATH", "tarveri.db")

    if not os.path.exists(db_path):
        print(f"Database not found at: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get total verified
    try:
        cursor.execute("SELECT COUNT(*) FROM verifications")
        total_verified = cursor.fetchone()[0]
    except sqlite3.OperationalError:
        total_verified = 0

    print("=" * 75)
    print(" TARVeri Backend Database Inspector")
    print(f" Database File: {db_path}")
    print(f" Total Verified Students: {total_verified}")
    print("=" * 75)

    try:
        cursor.execute(
            "SELECT guild_id, welcome_channel_id, help_channel_id, updated_at FROM guild_settings ORDER BY updated_at DESC"
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        rows = []

    if not rows:
        print("\nNo servers configured yet in 'guild_settings' table.")
        print("Servers will use auto-detection until an admin configures channels using:")
        print("  /setwelcomec #channel")
        print("  /sethelpc #channel\n")
    else:
        print(f"\nConfigured Servers ({len(rows)} server(s) in SQLite 'guild_settings'):")
        print("-" * 75)
        print(f"{'Guild ID':<20} | {'Welcome Channel ID':<20} | {'Help Channel ID':<20} | {'Last Updated'}")
        print("-" * 75)
        for g_id, w_id, h_id, updated in rows:
            w_str = str(w_id) if w_id else "Auto-detect"
            h_str = str(h_id) if h_id else "Auto-detect"
            print(f"{str(g_id):<20} | {w_str:<20} | {h_str:<20} | {updated}")
        print("-" * 75)

    conn.close()


if __name__ == "__main__":
    main()
