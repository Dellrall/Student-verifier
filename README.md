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
* `TARVERI_TIMEZONE`: Timezone for logs and database timestamps (default: `Asia/Kuala_Lumpur` / local system time).
* `TARVERI_MAX_BACKUPS`: Number of recent database backups to keep in `backups/` (default: `10`).

### 4. Start the Bot

```bash
python tarveri_bot.py
# Or run as a Python module:
python -m tarveri
```

### 5. Updating the Bot

To safely pull upstream updates with automatic database backup (10-file rotation), dependency sync, and pre-flight testing:

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
* `/referral generate [ttl_hours]` — Verified students generate a single-use guest referral code for friends.
* `/referral list` — View your active and past generated referral codes.

### Guests & Non-TARUMT Outsiders
* **Referral Entry**: Outsiders with a referral code click **"Enter Referral Code"** on the gateway panel or use the modal to enter the code.
* **Direct Application**: Outsiders without a code click **"Apply as Guest"** to submit their name and reason for joining.
* **Double Verification Process**:
  1. **Step 1 (Voucher)**: The referring student submits their vouch statement/context.
  2. **Step 2 (Admin Team)**: Server admins review the context and click **`[Approve Guest]`** or **`[Reject / Veto]`**.
* **Audit Trail**: All reason notes, comments, voucher IDs, and admin verdicts are stored with timestamps in the database.
* **Automatic Revocation**: Guest access and active tickets are automatically revoked if a member leaves, is kicked, or is banned from the server.

### Automated Server Assistance
* **Interactive Gateway Panel**: Admins can post a persistent 3-button verification panel (`/send_gateway_panel`) in the welcome channel.
* **New Member Onboarding**: When a new unverified student joins the server, the bot tags them in the welcome channel with permanent verification instructions.
* **Smart Role Help Tips**: When an unverified user asks questions like *"How to get role"* or *"nak verify"* in support channels, the bot replies with permanent tips explaining how to verify.

### Admins
* `/send_gateway_panel [channel]` — Post the persistent 3-button verification gateway panel (Student Verify / Referral Code / Guest Apply).
* `/setguestrole [role_name]` — Configure the server's guest role name (default: `Guest`).
* `/setreviewchannel [channel]` — Configure the parent channel for private guest review threads.
* `/setwelcomec [channel]` — Configure or reset the server's welcome channel for new member verification tags.
* `/sethelpc [channel]` — Configure or reset the server's specific help channel for automated role tips.
* `/stats` — View verification numbers, faculty breakdown, and configured channels.
* `/unverify @user` — Unlink a student ID and remove their roles.
* `/audit` — View recent audit log entries.
* `/backup` — Create an immediate database snapshot in `backups/` (auto-rotated to 10 recent files).
* `/resync` — Re-check and update roles across mutual servers.
* `/check_updates [stream]` — Check for new git updates on a specific or default stream directly from Discord.



