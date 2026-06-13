#!/usr/bin/env bash
# sim-monitor installer for Raspberry Pi OS (Debian 12+/Trixie).
# Run from a checkout of the repo:  sudo ./install.sh
# Idempotent: re-run after `git pull` to upgrade (configs are never overwritten).
set -euo pipefail

APP_DIR=/opt/sim-monitor
ETC_DIR=/etc/sim-monitor
VAR_DIR=/var/lib/sim-monitor
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { echo -e "\033[1;32m==>\033[0m $*"; }

if [[ $EUID -ne 0 ]]; then
    echo "error: run as root (sudo ./install.sh)" >&2
    exit 1
fi

say "Installing OS packages (python3-venv, NetworkManager, ModemManager)"
apt-get update -qq
apt-get install -y -qq python3 python3-venv network-manager modemmanager > /dev/null

say "Creating directories"
mkdir -p "$APP_DIR" "$ETC_DIR/profiles.d" "$VAR_DIR"

# Record where we were installed from so the web UI's "Update" action can
# pull + reinstall this same checkout without anyone SSHing in.
echo "$REPO_DIR" > "$ETC_DIR/install-source"

say "Setting up Python venv in $APP_DIR/venv"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet --upgrade "$REPO_DIR"

if [[ ! -f "$ETC_DIR/config.yaml" ]]; then
    say "Writing default $ETC_DIR/config.yaml"
    cat > "$ETC_DIR/config.yaml" <<'EOF'
# sim-monitor configuration. See config/config.example.yaml in the repo for
# all options. This file may contain secrets via profiles -- keep it on-device.
web:
  host: 0.0.0.0          # LAN only; do not port-forward (no auth)
  port: 8080
daemon:
  tick_seconds: 5
  connect_timeout_seconds: 90
  registration_timeout_seconds: 300   # roaming SIMs may scan carriers for minutes
modem:
  at_port: auto          # auto = /dev/sim-monitor-at udev symlink, then VID hints
  baud: 115200
log_level: INFO
db_path: /var/lib/sim-monitor/sim-monitor.db
profiles_dir: /etc/sim-monitor/profiles.d
simulate: false
EOF
else
    say "Keeping existing $ETC_DIR/config.yaml"
fi

if [[ ! -f "$ETC_DIR/profiles.d/00-hologram-default.yaml" ]]; then
    say "Installing default Hologram profile"
    cp "$REPO_DIR/config/profiles.d/00-hologram-default.yaml" "$ETC_DIR/profiles.d/"
else
    say "Keeping existing default profile"
fi

say "Installing udev rules (reserves the modem's spare AT port)"
cp "$REPO_DIR/deploy/udev/77-sim-monitor.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty || true

say "Installing systemd unit"
cp "$REPO_DIR/deploy/sim-monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sim-monitor.service > /dev/null
systemctl restart sim-monitor.service

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "Done."
echo
echo "  Service:   systemctl status sim-monitor"
echo "  Logs:      journalctl -u sim-monitor -f"
echo "  Web UI:    http://${IP_ADDR:-<pi-address>}:8080"
echo "  Profiles:  $ETC_DIR/profiles.d/ (or edit via the web UI)"
echo
echo "If the modem was already plugged in, unplug/replug it once so the new"
echo "udev rules apply to its serial ports."
