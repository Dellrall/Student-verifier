#!/usr/bin/env bash
# ==============================================================================
# TARVeri — Safe Backend Updater with Pre-Update DB Backup & Auto-Rollback
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

# Configuration
VENV_DIR="${PROJECT_ROOT}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
PYTEST_BIN="${VENV_DIR}/bin/pytest"
PIP_BIN="${VENV_DIR}/bin/pip"
BACKUP_DIR="${PROJECT_ROOT}/backups"
DB_PATH="${TARVERI_DB_PATH:-tarveri.db}"
SERVICE_NAME="tarveri"

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Verify virtual environment
if [ ! -d "${VENV_DIR}" ] || [ ! -x "${PYTHON_BIN}" ]; then
    log_error "Virtual environment not found at ${VENV_DIR}. Please set it up first."
    exit 1
fi

# Fetch remote status
log_info "Checking upstream for updates..."
git fetch --quiet

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
UPSTREAM_BRANCH="$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || echo "origin/${CURRENT_BRANCH}")"

LOCAL_HASH="$(git rev-parse HEAD)"
REMOTE_HASH="$(git rev-parse "${UPSTREAM_BRANCH}" 2>/dev/null || echo "${LOCAL_HASH}")"

if [ "${LOCAL_HASH}" = "${REMOTE_HASH}" ]; then
    log_success "TARVeri is already on the latest version (${LOCAL_HASH:0:7})."
    if [ "$1" = "--check" ]; then
        exit 0
    fi
fi

BEHIND_COUNT="$(git rev-list --count HEAD.."${UPSTREAM_BRANCH}" 2>/dev/null || echo "0")"

if [ "${BEHIND_COUNT}" -gt 0 ]; then
    log_warn "Upstream is ${BEHIND_COUNT} commit(s) ahead (${LOCAL_HASH:0:7} -> ${REMOTE_HASH:0:7})."
fi

if [ "$1" = "--check" ]; then
    if [ "${BEHIND_COUNT}" -gt 0 ]; then
        echo -e "\n🔔 ${YELLOW}Update available!${NC} Run './scripts/update.sh' on this server to apply."
    fi
    exit 0
fi

# ------------------------------------------------------------------------------
# STEP 1: Pre-Update Database Backup
# ------------------------------------------------------------------------------
mkdir -p "${BACKUP_DIR}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SNAPSHOT_PATH="${BACKUP_DIR}/tarveri_pre_update_${TIMESTAMP}.db"

log_info "[1/5] Creating pre-update database backup..."
if [ -f "${DB_PATH}" ]; then
    # Use SQLite vacuum / copy for snapshot
    ${PYTHON_BIN} -c "
import sqlite3, sys
try:
    src = sqlite3.connect('${DB_PATH}')
    dst = sqlite3.connect('${SNAPSHOT_PATH}')
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print('Snapshot created successfully.')
except Exception as e:
    print(f'Backup error: {e}', file=sys.stderr)
    sys.exit(1)
"
    log_success "Database snapshot saved to ${SNAPSHOT_PATH}"
else
    log_info "No existing database file at ${DB_PATH}. Skipping backup."
fi

# ------------------------------------------------------------------------------
# STEP 2: Pull Upstream Changes
# ------------------------------------------------------------------------------
log_info "[2/5] Pulling upstream changes from ${UPSTREAM_BRANCH}..."
PREV_COMMIT="${LOCAL_HASH}"

rollback() {
    log_error "Update failed! Initiating rollback..."
    git reset --hard "${PREV_COMMIT}" || true
    
    if [ -f "${SNAPSHOT_PATH}" ] && [ -f "${DB_PATH}" ]; then
        log_info "Restoring database from ${SNAPSHOT_PATH}..."
        cp -f "${SNAPSHOT_PATH}" "${DB_PATH}"
    fi
    
    log_warn "Rollback complete. System returned to commit ${PREV_COMMIT:0:7}."
    exit 1
}

# Trap unexpected errors to trigger rollback
trap rollback ERR

git pull --ff-only

NEW_COMMIT="$(git rev-parse HEAD)"
log_success "Successfully pulled code (now at ${NEW_COMMIT:0:7})."

# ------------------------------------------------------------------------------
# STEP 3: Sync Python Dependencies
# ------------------------------------------------------------------------------
log_info "[3/5] Updating dependencies..."
${PIP_BIN} install -r requirements.txt --upgrade --quiet

# ------------------------------------------------------------------------------
# STEP 4: Run Automated Pre-flight Tests
# ------------------------------------------------------------------------------
log_info "[4/5] Running automated pre-flight test suite..."
if [ -x "${PYTEST_BIN}" ]; then
    ${PYTEST_BIN} -q
    log_success "All pre-flight tests passed successfully!"
else
    log_warn "pytest not found in virtualenv. Running syntax compile check instead..."
    ${PYTHON_BIN} -m compileall -q tarveri tarveri_bot.py
fi

# Disable error trap now that tests passed
trap - ERR

# ------------------------------------------------------------------------------
# STEP 5: Restart Service
# ------------------------------------------------------------------------------
log_info "[5/5] Restarting TARVeri service..."

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    log_info "Restarting systemd service '${SERVICE_NAME}'..."
    systemctl restart "${SERVICE_NAME}"
    log_success "Service '${SERVICE_NAME}' restarted."
else
    log_info "No active systemd service '${SERVICE_NAME}' detected."
    log_info "If running manually, restart your bot process now:"
    echo -e "      ${GREEN}python tarveri_bot.py${NC}"
fi

echo -e "\n=============================================================================="
log_success "TARVeri update to ${NEW_COMMIT:0:7} completed cleanly!"
log_info "Pre-update backup preserved at: ${SNAPSHOT_PATH}"
echo -e "==============================================================================\n"
