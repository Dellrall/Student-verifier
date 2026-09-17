# 🛡️ Administrator Operations & Control Center Manual

TARVeri includes an interactive administration suite with diagnostics, telemetry, role synchronization, and server operations.

---

## 📋 Table of Contents
1. [Interactive Admin Dashboard](#interactive-admin-dashboard)
2. [Diagnostic & Self-Healing Commands](#diagnostic--self-healing-commands)
3. [Moderation & Verification Actions](#moderation--verification-actions)
4. [Channel & Role Configuration](#channel--role-configuration)
5. [Database Backups & Audit Logs](#database-backups--audit-logs)
6. [Complete Slash Command Matrix](#complete-slash-command-matrix)

---

## 1. Interactive Admin Dashboard (`/admin dashboard`)

Opens the interactive Administrator Control Center featuring:
- **Navigation Menu**: Dropdown categories (`Overview`, `Configuration`, `Diagnostics`, `Moderation`, `Tickets`, `Backups`, `Logs`, `Gateway Panel`).
- **One-Click Actions**: Trigger self-healing diagnostics, database backups, email requirement toggles, and member unverification via native Discord modals.

---

## 2. Diagnostic & Self-Healing Commands

- **`/admin diagnose`**: Checks role hierarchies, scans for duplicate faculty roles, restores missing council/SRC roles, and cleans orphaned ticket states.
- **`/admin backfill_roles [default_campus] [default_level] [all_servers]`**: Batch-syncs campus branch and study level roles for all previously verified members.
- **`/admin resync`**: Reconciles and synchronizes member roles across all mutual guilds.
- **`/admin sync_commands`**: Forces command tree synchronization with the Discord API.

---

## 3. Moderation & Verification Actions

- **`/admin unverify @user [reason]`**: Unlinks a student ID, records audit history, and revokes faculty roles.
- **`/admin alumni_revoke @user [reason]`**: Revokes alumni status and removes the `TARUMT Alumni` role across mutual servers.
- **`/admin close_ticket [ticket] [reason]`**: Manually closes a guest review ticket without kicking the applicant from the server.

---

## 4. Channel & Role Configuration

- **`/admin set_channel [type] [channel]`**: Configures `welcome`, `help`, or guest `review` channels.
- **`/admin set_role [type] [role]`**: Configures `guest` or `admin` reviewer roles.
- **`/admin panel [channel]`**: Posts the persistent 3-button verification gateway panel.
- **`/admin email_verification [enabled]`**: Enables or disables mandatory email OTP verification on the current server.

---

## 5. Database Backups & Audit Logs

- **`/admin backup [action]`**: Creates an immediate SQLite snapshot or lists previous backup archives.
- **`/admin logs [action]`**: Tails live logs or inspects 10-day `.tar.gz` compressed archives.
- **`/admin audit [limit] [event_type]`**: Queries structured database audit records with filter support.

---

## 6. Complete Slash Command Matrix

| Slash Command | Permissions | Description |
| :--- | :---: | :--- |
| `/admin dashboard` | Administrator | Open the interactive control center dashboard. |
| `/admin stats` | Administrator | View student metrics and faculty distribution. |
| `/admin email_stats` | Administrator | View email verification and opt-in rates. |
| `/admin email_verification` | Administrator | Toggle mandatory student email OTP verification. |
| `/admin diagnose` | Administrator | Run server health check, role recovery & hierarchy diagnostics. |
| `/admin backfill_roles` | Administrator | Backfill branch campus and study level roles. |
| `/admin unverify` | Administrator | Unlink student ID and revoke roles across servers. |
| `/admin alumni_revoke` | Administrator | Revoke alumni status and card badges. |
| `/admin panel` | Administrator | Post the 3-button verification gateway panel. |
| `/admin tickets` | Administrator | List and inspect guest review tickets. |
| `/admin close_ticket` | Administrator | Close a review ticket without kicking applicant. |
| `/admin backup` | Administrator | Manage on-demand and automated SQLite snapshots. |
| `/admin logs` | Administrator | View live log files or compressed archives. |
| `/admin audit` | Administrator | Query structured security audit records. |
| `/admin set_channel` | Administrator | Bind welcome, help, or review channels. |
| `/admin set_role` | Administrator | Set guest role or reviewer role names. |
| `/admin resync` | Administrator | Force verification role reconciliation. |
| `/admin updates` | Administrator | Check for upstream Git releases. |
| `/admin sync_commands` | Administrator | Force slash command registration with Discord. |
