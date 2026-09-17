#!/usr/bin/env bash
# ==============================================================================
# TARVeri — Universal Systemd Service Installer & Generator
# Automatically configures and installs systemd units for ANY user, path, or machine.
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Terminal Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Default values
MODE="user" # "user" or "system"
INSTALL_LITESTREAM=false
ENABLE_NOW=false
SERVICE_NAME="tarveri"
LITESTREAM_SERVICE_NAME="litestream"
CURRENT_USER="$(id -un)"

show_help() {
    echo -e "${CYAN}TARVeri Systemd Service Installer${NC}"
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --user                 Install as rootless user service in ~/.config/systemd/user/ (Default, no sudo required)"
    echo "  --system               Install as system-wide service in /etc/systemd/system/ (Requires sudo)"
    echo "  --with-litestream      Also install and configure the companion Litestream replication service"
    echo "  --enable-now           Automatically enable and start the services immediately"
    echo "  --name <name>          Custom service name (Default: tarveri)"
    echo "  --help, -h             Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --user --enable-now                       # Recommended for personal / VPS deployment"
    echo "  $0 --user --with-litestream --enable-now     # Install bot + cloud replication"
    echo "  $0 --system --with-litestream                # System-wide installation"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            MODE="user"
            shift
            ;;
        --system)
            MODE="system"
            shift
            ;;
        --with-litestream|--litestream)
            INSTALL_LITESTREAM=true
            shift
            ;;
        --enable-now|--start)
            ENABLE_NOW=true
            shift
            ;;
        --name)
            SERVICE_NAME="$2"
            shift 2
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

echo "=============================================================================="
echo " 🛠️  TARVeri Universal Service Configuration Generator"
echo "=============================================================================="

# 1. Detect Python executable
PYTHON_BIN=""
if [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
    PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    log_error "Python 3 executable not found. Please set up your virtual environment in ${PROJECT_ROOT}/.venv first."
    exit 1
fi

# 2. Detect Litestream executable if requested
LITESTREAM_BIN=""
if [ "${INSTALL_LITESTREAM}" = true ]; then
    if command -v litestream >/dev/null 2>&1; then
        LITESTREAM_BIN="$(command -v litestream)"
    elif [ -x "/usr/local/bin/litestream" ]; then
        LITESTREAM_BIN="/usr/local/bin/litestream"
    elif [ -x "/usr/bin/litestream" ]; then
        LITESTREAM_BIN="/usr/bin/litestream"
    else
        log_warn "Litestream binary not found in PATH or standard directories. Defaulting to /usr/local/bin/litestream."
        LITESTREAM_BIN="/usr/local/bin/litestream"
    fi
fi

# 3. Detect Entry point and env files
ENTRY_POINT="${PROJECT_ROOT}/tarveri_bot.py"
ENV_FILE="${PROJECT_ROOT}/.env"
LITESTREAM_CONFIG="${PROJECT_ROOT}/litestream.yml"

log_info "Detected Project Directory : ${PROJECT_ROOT}"
log_info "Detected Python Binary     : ${PYTHON_BIN}"
log_info "Detected Active User       : ${CURRENT_USER}"
log_info "Installation Target Mode   : ${MODE^^}"

# 4. Generate TARVeri Unit Content
if [ "${MODE}" = "user" ]; then
    UNIT_DIR="${HOME}/.config/systemd/user"
    mkdir -p "${UNIT_DIR}"
    TARVERI_UNIT_PATH="${UNIT_DIR}/${SERVICE_NAME}.service"
    LITESTREAM_UNIT_PATH="${UNIT_DIR}/${LITESTREAM_SERVICE_NAME}.service"

    cat <<EOF > "${TARVERI_UNIT_PATH}"
[Unit]
Description=TARVeri Discord Student Verification Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${PYTHON_BIN} ${ENTRY_POINT}
Restart=always
RestartSec=5
TimeoutStopSec=15
KillSignal=SIGINT

# Environment Configuration
EnvironmentFile=${ENV_FILE}

# Logging
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

    log_success "Generated User Service: ${TARVERI_UNIT_PATH}"

    if [ "${INSTALL_LITESTREAM}" = true ]; then
        cat <<EOF > "${LITESTREAM_UNIT_PATH}"
[Unit]
Description=Litestream SQLite Continuous Cloud Replication
After=network.target

[Service]
Type=simple
WorkingDirectory=${PROJECT_ROOT}
EnvironmentFile=${ENV_FILE}
ExecStart=${LITESTREAM_BIN} replicate -config ${LITESTREAM_CONFIG}
Restart=always
RestartSec=5

# Logging
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
        log_success "Generated Litestream User Service: ${LITESTREAM_UNIT_PATH}"
    fi

    # Reload user systemd
    systemctl --user daemon-reload
    log_info "Reloaded systemd user daemon."

    if [ "${ENABLE_NOW}" = true ]; then
        log_info "Enabling and starting service '${SERVICE_NAME}'..."
        systemctl --user enable --now "${SERVICE_NAME}"
        if [ "${INSTALL_LITESTREAM}" = true ]; then
            log_info "Enabling and starting service '${LITESTREAM_SERVICE_NAME}'..."
            systemctl --user enable --now "${LITESTREAM_SERVICE_NAME}"
        fi
        log_success "Services started successfully!"
    fi

    echo ""
    echo -e "${GREEN}✅ Installation Complete!${NC}"
    echo "Useful management commands:"
    echo "  systemctl --user status ${SERVICE_NAME}        # Check bot status"
    echo "  journalctl --user -u ${SERVICE_NAME} -f        # View live bot logs"
    if [ "${INSTALL_LITESTREAM}" = true ]; then
        echo "  systemctl --user status ${LITESTREAM_SERVICE_NAME}  # Check replication status"
        echo "  journalctl --user -u ${LITESTREAM_SERVICE_NAME} -f  # View replication logs"
    fi
    echo ""
    echo "💡 Note: To allow user services to run 24/7 after SSH logout, run:"
    echo "  loginctl enable-linger ${CURRENT_USER}"

else
    # System-wide mode (/etc/systemd/system)
    UNIT_DIR="/etc/systemd/system"
    TARVERI_UNIT_PATH="${UNIT_DIR}/${SERVICE_NAME}.service"
    LITESTREAM_UNIT_PATH="${UNIT_DIR}/${LITESTREAM_SERVICE_NAME}.service"

    TEMP_TARVERI="/tmp/${SERVICE_NAME}.service"
    TEMP_LITESTREAM="/tmp/${LITESTREAM_SERVICE_NAME}.service"

    cat <<EOF > "${TEMP_TARVERI}"
[Unit]
Description=TARVeri Discord Student Verification Bot
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_ROOT}
ExecStart=${PYTHON_BIN} ${ENTRY_POINT}
Restart=always
RestartSec=5
TimeoutStopSec=15
KillSignal=SIGINT

# Environment Configuration
EnvironmentFile=${ENV_FILE}

# Hardening
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

# Logging
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    log_info "Installing system service to ${TARVERI_UNIT_PATH} (requires sudo)..."
    sudo cp "${TEMP_TARVERI}" "${TARVERI_UNIT_PATH}"
    rm -f "${TEMP_TARVERI}"
    log_success "Installed System Service: ${TARVERI_UNIT_PATH}"

    if [ "${INSTALL_LITESTREAM}" = true ]; then
        cat <<EOF > "${TEMP_LITESTREAM}"
[Unit]
Description=Litestream SQLite Continuous Cloud Replication
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_ROOT}
EnvironmentFile=${ENV_FILE}
ExecStart=${LITESTREAM_BIN} replicate -config ${LITESTREAM_CONFIG}
Restart=always
RestartSec=5

# Hardening
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

# Logging
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
        sudo cp "${TEMP_LITESTREAM}" "${LITESTREAM_UNIT_PATH}"
        rm -f "${TEMP_LITESTREAM}"
        log_success "Installed System Service: ${LITESTREAM_UNIT_PATH}"
    fi

    sudo systemctl daemon-reload
    log_info "Reloaded systemd daemon."

    if [ "${ENABLE_NOW}" = true ]; then
        log_info "Enabling and starting service '${SERVICE_NAME}'..."
        sudo systemctl enable --now "${SERVICE_NAME}"
        if [ "${INSTALL_LITESTREAM}" = true ]; then
            log_info "Enabling and starting service '${LITESTREAM_SERVICE_NAME}'..."
            sudo systemctl enable --now "${LITESTREAM_SERVICE_NAME}"
        fi
        log_success "Services started successfully!"
    fi

    echo ""
    echo -e "${GREEN}✅ Installation Complete!${NC}"
    echo "Useful management commands:"
    echo "  sudo systemctl status ${SERVICE_NAME}        # Check bot status"
    echo "  sudo journalctl -u ${SERVICE_NAME} -f        # View live bot logs"
    if [ "${INSTALL_LITESTREAM}" = true ]; then
        echo "  sudo systemctl status ${LITESTREAM_SERVICE_NAME}  # Check replication status"
        echo "  sudo journalctl -u ${LITESTREAM_SERVICE_NAME} -f  # View replication logs"
    fi
fi
