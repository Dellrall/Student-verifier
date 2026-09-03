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
# Update according to configured stream in .env (or current upstream)
./scripts/update.sh

# Or target a specific stream/branch directly
./scripts/update.sh main
./scripts/update.sh refactor/modular-optimization
```

*(To check if updates are available without applying: `./scripts/update.sh --check` or `./scripts/update.sh --check main`)*

## Usage

### Students
* `/verify` — Opens a private modal popup in the server to submit your student ID.
* `!verify` — Fallback prefix command.
* Or DM your student ID (e.g. `23WMD09867`) directly to the bot.

### Automated Server Assistance
* **New Member Onboarding**: When a new unverified student joins the server, the bot tags them in the welcome/verification channel with clear verification instructions.
* **Smart Role Help Tips**: When an unverified user asks questions like *"How to get role"* or mentions *"role"* in a help/support channel, the bot replies with helpful tips explaining how to verify.

### Admins
* `/stats` — View verification numbers and faculty breakdown.
* `/unverify @user` — Unlink a student ID and remove their roles.
* `/audit` — View recent audit log entries.
* `/backup` — Create an immediate database snapshot in `backups/`.
* `/resync` — Re-check and update roles.
* `/check_updates [stream]` — Check for new git updates on a specific or default stream directly from Discord.

