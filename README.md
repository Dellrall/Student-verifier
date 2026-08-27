# TARVeri

A high-performance Discord verification bot for TARUMT students that validates student IDs and assigns faculty roles across servers.

## Features

- **Privacy & Security**: Student IDs are hashed at rest with HMAC-SHA256 (no raw student IDs stored in DB).
- **Persistent SQLite + WAL**: Asynchronous database with WAL mode and indexing for fast non-blocking concurrent queries.
- **Concurrent Discord API Operations**: Cache-first member lookups and parallelized role assignments across mutual guilds.
- **Memory-Safe Rate Limiting**: Sliding window rate limiter with automatic garbage collection of expired entries.
- **Auto-Sync & Self-Healing**: Automatically assigns faculty roles when verified members join new servers.
- **Admin Management Suite**: Slash commands for statistics (`/stats`), account unlinking (`/unverify`), audit query (`/audit`), and role resync (`/resync`).
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

### 4. Start the Bot

```bash
python tarveri_bot.py
```

### 5. Running Tests

```bash
pytest -v
```

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
