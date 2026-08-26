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

# (Optional) Delete all git-related folders and files (.git, .gitignore) to start fresh
rm -rf .git .gitignore  # On Windows: rmdir /s /q .git & del /f /q .gitignore

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
* `TARVERI_ID_HASH_SECRET`: A random secret string used to hash student IDs at rest (generate one with `python -c "import secrets; print(secrets.token_hex(32))"`).

### 4. Start the Bot

```bash
python tarveri_bot.py
```

## Usage

* `/verify` — Opens a private modal popup in the server to submit your student ID.
* `!verify` — Fallback prefix command.
* Or DM your student ID (e.g. `23WMD09867`) directly to the bot.
