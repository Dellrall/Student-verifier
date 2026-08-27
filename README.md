# TARVeri

A Discord verification bot for TARUMT students that assigns faculty roles based on student IDs.

## Setup & Running

### 1. Requirements
* Python 3.10+
* A Discord bot with **Server Members Intent** and **Message Content Intent** enabled in the Developer Portal.
* Bot needs **Manage Roles** permission (placed above faculty roles in the server role list).

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
* `TARVERI_ID_HASH_SECRET`: A random secret string used to hash student IDs at rest (generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`).

### 4. Start the Bot

```bash
python tarveri_bot.py
```

### 5. Updating the Bot

To safely pull upstream updates with automatic database backup, dependency sync, and pre-flight testing:

```bash
./scripts/update.sh
```

*(To only check if updates are available without applying: `./scripts/update.sh --check`)*

## Usage

### Students
* `/verify` — Opens a private modal popup in the server to submit your student ID.
* `!verify` — Fallback prefix command.
* Or DM your student ID (e.g. `23WMD09867`) directly to the bot.

### Admins
* `/stats` — View verification numbers and faculty breakdown.
* `/unverify @user` — Unlink a student ID and remove their roles.
* `/audit` — View recent audit log entries.
* `/backup` — Create an immediate database snapshot in `backups/`.
* `/resync` — Re-check and update roles.
