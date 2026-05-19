#!/usr/bin/env bash
# ============================================================
# Pi Dashboard — install, configure, and start
# Run as root:  sudo bash INSTALL.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/pi-dashboard"
VENV_DIR="$INSTALL_DIR/venv"
DEST="$INSTALL_DIR/pi-dashboard.py"
SERVICE="/etc/systemd/system/pi-dashboard.service"
PORT=8585

echo "=== Pi Dashboard Installer ==="

# --- must be root ---
if [[ $EUID -ne 0 ]]; then
  echo "Error: run this script with sudo"
  exit 1
fi

# --- ensure python3-venv is available ---
echo "[1/6] Ensuring python3-venv is installed..."
apt-get install -y -qq python3-venv > /dev/null 2>&1 || true
echo "      python3-venv ready ✓"

# --- create install dir + venv ---
echo "[2/6] Creating virtual environment at $VENV_DIR..."
mkdir -p "$INSTALL_DIR"
python3 -m venv "$VENV_DIR"
echo "      venv created ✓"

# --- install python dependency ---
echo "[3/6] Installing bottle in venv..."
"$VENV_DIR/bin/pip" install --quiet bottle
echo "      bottle installed ✓"

# --- copy dashboard script ---
echo "[4/6] Copying pi-dashboard.py → $DEST"
cp "$SCRIPT_DIR/pi-dashboard.py" "$DEST"
chmod +x "$DEST"

# --- install systemd service (uses venv python) ---
echo "[5/6] Installing systemd service → $SERVICE"
cat > "$SERVICE" <<EOF
[Unit]
Description=Pi Monitoring Dashboard
After=network.target

[Service]
Type=simple
User=root
ExecStart=$VENV_DIR/bin/python $DEST
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable pi-dashboard
systemctl restart pi-dashboard

# --- verify ---
echo "[6/6] Verifying..."
sleep 2
if systemctl is-active --quiet pi-dashboard; then
  IP=$(hostname -I | awk '{print $1}')
  echo ""
  echo "=== Pi Dashboard is running ==="
  echo "    Web UI:  http://${IP}:${PORT}"
  echo "    API:     http://${IP}:${PORT}/api/stats"
  echo "    Status:  sudo systemctl status pi-dashboard"
  echo "    Logs:    sudo journalctl -u pi-dashboard -f"
  echo ""
else
  echo ""
  echo "ERROR: Service failed to start. Check logs:"
  echo "  sudo journalctl -u pi-dashboard --no-pager -l"
  exit 1
fi
