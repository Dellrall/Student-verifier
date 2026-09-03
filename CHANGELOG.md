# 📋 TARVeri Changelog

All notable changes to the **TARVeri** Discord Student & Guest Verification Bot are documented in this file.

---

## [v2.2.0] — 2026-09-03 (Current)
### 🎟️ Guest Access, Referral Codes & Private Thread Reviews
* **Verified Student Referral Codes (`/referral generate`, `/referral list`)**:
  - Allowed verified TARUMT students to generate single-use, expiring referral codes (e.g. `TAR-8X2K9P`) for friends and collaborators.
  - Added rate limiting (max 3 active codes per student) and configurable TTL (default 48 hours).
  - Code lifecycle tracking (`ACTIVE` → `PENDING_APPROVAL` → `USED` / `EXPIRED`).
* **Persistent Verification Gateway Panel (`/send_gateway_panel`)**:
  - Added persistent 3-button welcome panel with `[Verify TARUMT Student]`, `[Enter Referral Code]`, and `[Apply as Guest]`.
* **Private Discord Thread Review System**:
  - Created private review threads under the configured review/help channel.
  - Automatically invites the applicant (`@Friend`), referring student (`@Student`), and pings the admin role.
  - Added interactive review actions: **`[Approve Guest]`**, **`[Reject & Kick]`** (with custom reason modal), and **`[Confirm Vouch]`**.
* **Smart `Guest(Approved)` Role Detection & Reuse**:
  - Prioritizes existing server roles named `Guest(Approved)`, `Guest (Approved)`, or `Guest` before attempting to create duplicate roles.
  - Automatically isolates guest access to general channels while excluding faculty-restricted channels.
  - Updated `is_unverified_member()` so approved guests are not prompted with student verification tips.

### 🛡️ Security & Anti-Oracle Protections
* **Uniform Error Oracle Mitigation**:
  - Sanitized referral validation error outputs to prevent attackers from enumerating valid/expired/used codes.
* **Rate-Limited Guest & Referral Submissions**:
  - Extended sliding-window rate limiting to modal submission endpoints to block automated brute-force attempts.
* **Sensitive Data Masking**:
  - Student IDs remain strictly masked (`23***867`) in all database logs and admin audits.

### ⚡ Performance & Engine Optimizations
* **SQLite Engine Performance Tuning**:
  - Enabled 64MB memory-mapped I/O (`PRAGMA mmap_size = 67108864;`).
  - Allocated 4MB dedicated RAM page cache (`PRAGMA cache_size = -4000;`).
  - Forced temporary tables, sort buffers, and indexes to memory (`PRAGMA temp_store = MEMORY;`).
* **Drift-Immune Monotonic Rate Limiting**:
  - Converted `RateLimiter` to `time.monotonic()` to eliminate vulnerabilities from NTP system clock drift and leap seconds.
* **Bulk Referral Cleanup**:
  - Added `cleanup_expired_referrals()` for high-speed background batch expiration.

### 🪵 Logging Cleanliness & Multi-Server Tagging
* **Clean Startup Output**:
  - Replaced verbose multi-line channel dump banner on startup with a single, clear summary line.
* **Enforced Server Tagging**:
  - Added explicit `[Server: 'Server Name']` context to all backend mutation logs, admin actions, and warnings.

---

## [v2.1.0] — 2026-09-03
### 🌐 Multi-Server Configuration & Admin Suite
* **Per-Server Channel Mapping (`/setwelcomec`, `/sethelpc`, `/setguestrole`, `/setreviewchannel`)**:
  - Added SQLite `guild_settings` table to persist independent welcome channels, help channels, guest roles, and review channels per Discord server.
* **Zero-Delay Command Synchronization (`!sync`, `/sync_commands`)**:
  - Added `!sync` (and `!sync guild`) prefix command to instantly copy global commands to the current server without waiting up to 1 hour for Discord's global cache.
* **Backend Database Inspector (`scripts/show_servers.py`)**:
  - Created a CLI tool for bot hosters to inspect connected servers, channel IDs, guest roles, and verification metrics.
* **Automated Member Assistance**:
  - **New Member Onboarding**: Automatically tags unverified new joiners in the server's welcome channel.
  - **Smart Role Help Tips**: Proactively replies to unverified members asking how to get roles in support channels.

---

## [v2.0.0] — 2026-09-02
### 🏗️ Modular Architecture & Safe Automated Updates
* **Modular Codebase Refactoring**:
  - Reorganized the monolithic script into clean, decoupled modules: `tarveri/bot.py`, `tarveri/config.py`, `tarveri/database.py`, `tarveri/rate_limiter.py`, `tarveri/services/`, and `tarveri/cogs/`.
* **Zero-Downtime Safe Updater (`scripts/update.sh`)**:
  - Automated updater featuring atomic pre-update SQLite database snapshots (`VACUUM INTO`), virtualenv dependency sync, pre-flight test verification, and automated rollback upon failure.
  - Multi-stream branch support (`--stream main`, `--stream refactor/modular-optimization`, or `auto`).
* **Background Update Checker (`UpdateCheckerService`)**:
  - Non-blocking background service that checks git upstream and sends DM alerts to the hoster when updates are available.
* **Administrative Tooling**:
  - Added `/stats`, `/unverify`, `/audit`, `/backup`, `/resync`, and `/check_updates` slash commands.
* **Automated Async Test Suite**:
  - Built comprehensive test suite covering all services, rate limiters, databases, and cogs (39 unit tests).

---

## [v1.0.0] — 2026-09-01
### 🚀 Initial Release
* Basic student verification pipeline for TARUMT.
* HMAC-SHA256 privacy hashing to protect raw student IDs at rest.
* Faculty code mapping (`FAFB`, `FCCI`, `FOAS`, `FSSH`, `FOBE`, `CPUS`, `FOCS`, `FOET`).
* Basic `/verify` slash command and prefix commands.
* Graceful shutdown handlers.
