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
./scripts/update.sh beta
```

*(To check if updates are available without applying: `./scripts/update.sh --check` or `./scripts/update.sh --check main`)*

## Usage

### Students & Members
* **`/verify [student_id] [expiry_date]`** — Opens a private modal popup (or verifies directly via slash arguments).
  * *Zero-Effort Card Expiry*: Leaving the expiry date blank automatically estimates card validity based on intake year and study level (`F`: +1y, `D`: +2y, `R`: +3y, `P`: +2y).
  * *Smart Academic Transition*: Progressing to Degree or Masters? Simply enter your new Student ID to atomically update your study level and roles with full audit history.
  * *Real-Time Lifecycle Detection*: If your intake year or card expiry date is in the past, TARVeri automatically attaches an interactive resolution menu (🎓 Graduated Alumni / 📚 Further Studies / ⏳ Extend Expiry).
* **Direct Messages (DMs)** — Send your student ID (e.g. `24WMR12345` or `24WMR12345 10/27`) directly to the bot for private verification.
* **`/graduate [year] [programme]`** — Instant alumni claim for verified students. Discovers existing server `Alumni` roles or provisions the official `#D4AF37` role, updating your Digital Campus Card to Alumni status.
* **`/card [member] [hidden]`** — Generate and share high-DPI digital student/guest/alumni campus ID cards rendered with glassmorphism design, verification checkmarks, and achievement badges (`public` by default, or `hidden: True`).
* **Context Menu App**: Right-click (or long-press) any member $\to$ **Apps** $\to$ **"View Campus Card"**.
* **`/referral generate [ttl_hours]`** — Verified students generate a single-use guest referral code for friends (max 3 active).
* **`/referral list`** — View active and past generated referral codes.

### 🎓 Academic Lifecycle & Graduation Watchdog Engine
* **Automated Expiry Sweeps (`GraduationWatchdogService`)**: Periodic background checks (every 24h) monitor card validity and send polite lifecycle resolution DMs with a 7-day cooldown.
* **Active-Chat Graduation Prompt**: When a student with an expired card participates in server channels, the bot delivers the interactive lifecycle resolution UI to guide their status update.
* **Sliding Century Windowing**: Dynamically handles historical and future intake years (`1969 <= year <= datetime.now().year + 5`) with zero hardcoded time-locks.

### Guests & Non-TARUMT Outsiders
* **Referral Entry**: Outsiders with a referral code click **"Enter Referral Code"** on the gateway panel or use the modal to enter the code.
* **Direct Application**: Outsiders without a code click **"Apply as Guest"** to submit their name and reason for joining.
* **Double Verification Process**:
  1. **Step 1 (Voucher)**: The referring student submits their vouch statement/context.
  2. **Step 2 (Admin Team)**: Server admins review the context and click **`[Approve Guest]`** or **`[Reject / Veto]`**.
* **Alphanumeric Ticket Tracking**: Private review threads and audit records use alphanumeric sequence numbers (`#A0001`, `#A0002` ... `#Z9999` $\to$ `#AA0001`).
* **Intelligent Staff Tagging & 1-Hour Escalation**: The bot tags a batch of 2 admins (active/online moderators first, then highest authority). If 1 hour passes without admin response, it automatically escalates by tagging the next 2 admins.
* **Audit Trail**: All reason notes, comments, voucher IDs, and admin verdicts are stored with timestamps in the database.
* **Automatic Revocation**: Guest access and active tickets are automatically revoked if a member leaves, is kicked, or is banned from the server.

### 🛡️ Self-Healing & Auto-Recovery Engine
* **Database Auto-Healing**: Executes `PRAGMA integrity_check` on connection startup and truncates SQLite WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) on startup/shutdown.
* **Channel Drift & Deleted Channel Recovery**: If configured review, help, or welcome channels are deleted, TARVeri clears stale database IDs and falls back smoothly to keyword-matched channels (`review`, `approval`, `ticket`, `help`, `welcome`).
* **Dynamic Role Auto-Creation & Fuzzy Discovery**: If faculty, campus, study level, or guest roles are deleted from Discord, the bot automatically recreates them with official palette colors without failing verifications.
* **Duplicate Role Cleanup & Migration**: Scans servers for duplicate faculty roles, migrates members to the primary role, and deletes bot-created duplicates while strictly protecting admin-created roles.
* **SRC & Council Role Protection**: Functional student council roles (`FAFB SRC`, `FOCS SRC`, etc.) are protected from deduplication and automatically recreated if missing.
* **Downtime Manual Grant Detection**: If an admin manually grants the `Guest(Approved)` role during maintenance, open tickets are automatically transitioned to `APPROVED` and review threads archived.
* **Returning Student & Alumni Role Restoration**: Automatically restores missing faculty and alumni roles for verified members who rejoined during maintenance.
* **Network & Power Outage Watchdog**: Probes external gateway connectivity with a 5-minute debounce window and cleanly checkpoints SQLite on UPS power failure signals (`SIGPWR`).

### Automated Server Assistance
* **Interactive Gateway Panel**: Admins can post a persistent 3-button verification panel (`/admin panel`) in the welcome channel.
* **New Member Onboarding**: When a new unverified student joins the server, the bot tags them in the welcome channel with permanent verification instructions.
* **Smart Role Help Tips**: When an unverified user asks questions like *"How to get role"* or *"nak verify"* in support channels, the bot replies with permanent tips explaining how to verify.

### 🛡️ Administrator Control Center (`/admin`)
* **`/admin dashboard`** — Opens the rich interactive **TARVeri Administrator Control Center** UI (with category navigation dropdowns, diagnostics execution, channel/role pickers, unverify/revoke modals, and one-click backups).
* **`/admin stats`** — View student verification numbers, alumni metrics, faculty distribution percentages, and server health.
* **`/admin diagnose`** — Run role hierarchy diagnostics, duplicate role reconciliation, and auto-heal missing faculty/alumni/SRC roles.
* **`/admin backfill_roles [default_campus] [default_level] [all_servers]`** — Batch sync and assign missing branch campus and study level roles to all verified members.
* **`/admin unverify @user [reason]`** — Unlink a student ID and remove their faculty/alumni roles across mutual servers.
* **`/admin alumni_revoke @user [reason]`** — Revoke Alumni status and remove the `TARUMT Alumni` role across mutual servers.
* **`/admin set_channel [type] [channel]`** — Configure or reset the server's `welcome`, `help`, or guest `review` channels in a single command.
* **`/admin set_role [type] [role/name]`** — Configure or reset the server's `guest` or `admin` reviewer roles.
* **`/admin panel [channel]`** — Post the persistent 3-button verification gateway panel (Student Verify / Referral Code / Guest Apply).
* **`/admin tickets [status] [limit]`** — Query guest review tickets with clickable links to threads and verdict notes.
* **`/admin backup [action]`** — Create immediate snapshots, list historical backups, or restore previous settings.
* **`/admin logs [action]`** — Inspect active daily logs, list 10-day `.tar.gz` archives, or tail recent log lines.
* **`/admin audit [limit] [event_type]`** — Inspect database audit logs with optional event type filtering.
* **`/admin resync`** — Re-check and synchronize roles across mutual servers.
* **`/admin updates [stream]`** — Check for new git updates on a specific or default stream directly from Discord.
* **`/admin sync_commands`** — Clean duplicate slash commands and force sync with Discord.




