# TARVeri

A high-performance Discord verification bot for TARUMT students that validates student IDs and assigns faculty roles across servers.

## Features

- **Privacy & Security**: Student IDs are hashed at rest with HMAC-SHA256 (no raw student IDs stored in DB).
- **Persistent SQLite + WAL**: Asynchronous database with WAL mode and indexing for fast non-blocking concurrent queries.
- **Concurrent Discord API Operations**: Cache-first member lookups and parallelized role assignments across mutual guilds.
- **Memory-Safe Rate Limiting**: Sliding window rate limiter with automatic garbage collection of expired entries.
- **Auto-Sync & Self-Healing**: Automatically assigns faculty roles when verified members join new servers.
- **Admin Management Suite**: Slash commands for statistics (`/stats`), account unlinking (`/unverify`), audit query (`/audit`), and role resync (`/resync`).
- **Safe Backend Updater**: Automated pre-update database snapshot, git sync, dependency upgrade, pre-flight test validation, and automatic rollback on failure (`./scripts/update.sh`).
- **Hoster Notifications**: Non-blocking background updater service that notifies the bot hoster (via server logs or private Discord DM) when upstream updates are available.
- **Graceful Shutdown**: OS signal handling (SIGINT/SIGTERM) with database WAL checkpoint truncation.

---

## Setup & Running

### 1. Requirements
* Python 3.10+
* A Discord bot with **Server Members Intent** and **Message Content Intent** enabled in the Developer Portal.
* Bot needs **Manage Roles** permission (placed above faculty roles in the server role hierarchy).

### 2. Installation

```bash
git clone https://github.com/Dellrall/Student-verifier.git
cd Student-verifier

python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configuration

Copy the sample environment file:

```bash
cp .env.example .env
```

Edit `.env`:
* `TARVERI_BOT_TOKEN`: Your Discord bot token.
* `TARVERI_ID_HASH_SECRET`: A secret string used to hash student IDs (generate one with `python3 -c "import secrets; print(secrets.token_hex(32))"`).
* `TARVERI_DB_PATH`: *(Optional)* Path to SQLite database (default: `tarveri.db`).
* `TARVERI_ADMIN_ROLE_NAME`: *(Optional)* Name of admin role for management commands (default: `TARVeri Admin`).
* `TARVERI_HOSTER_DISCORD_ID`: *(Optional)* Host Maintainer Discord User ID (for receiving private update notifications).

### 4. Start the Bot

```bash
python tarveri_bot.py
```

### 5. Running Tests

```bash
pytest -v
```

---

## Safe Backend Updates & Maintenance

TARVeri includes a repository-agnostic backend update script that automatically backs up your SQLite database, pulls changes from your configured upstream Git remote/fork, tests the build, and rolls back if anything fails.

### Check for Available Updates
```bash
./scripts/update.sh --check
```

### Apply Update with Automated DB Backup & Pre-flight Testing
```bash
./scripts/update.sh
```

**Lifecycle Executed by `update.sh`**:
1. 📦 **Database Snapshot**: Creates a timestamped `.db` backup in `backups/tarveri_pre_update_<timestamp>.db`.
2. ⬇️ **Git Pull**: Pulls fast-forward upstream changes.
3. 🐍 **Dependency Sync**: Upgrades packages in `.venv`.
4. 🧪 **Pre-Flight Tests**: Executes `pytest`. If any test fails, it **instantly rolls back Git HEAD and restores the database snapshot**.
5. 🚀 **Service Restart**: Restarts `systemd` service or outputs restart instructions.

---

## Commands

### Student Commands
* `/verify [student_id]` — Submits student ID or opens an interactive modal.
* `!verify [student_id]` — Fallback text command (ephemeral/auto-cleaned).
* **Direct Messages** — Send your student ID (e.g. `23WMD09867`) directly to the bot.

### Admin Commands (Requires Admin Permission or `TARVeri Admin` role)
* `/stats` — Displays total verifications, faculty breakdown, and 24h / 7d activity.
* `/unverify <user> [reason]` — Unlinks student ID and removes faculty roles across shared servers.
* `/audit [limit] [event_type]` — Views recent audit log entries.
* `/resync [user]` — Forces role resynchronization.
* `/backup` — Creates an immediate point-in-time database snapshot in `backups/`.
* `/sync_commands [guild_only]` — Syncs application command tree.

---

## Faculty Mappings

| Faculty Code | Faculty Name | Role Assigned |
| :--- | :--- | :--- |
| `B` | Faculty of Accountancy, Finance and Business | **FAFB** |
| `K` | Faculty of Communication and Creative Industries | **FCCI** |
| `L` | Faculty of Applied Sciences | **FOAS** |
| `J` | Faculty of Social Science and Humanities | **FSSH** |
| `V` | Faculty of Built Environment | **FOBE** |
| `F` | Centre for Pre-University Studies | **CPUS** |
| `M` | Faculty of Computing and Information Technology | **FOCS** |
| `G` | Faculty of Engineering and Technology | **FOET** |
