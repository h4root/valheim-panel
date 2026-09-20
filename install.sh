#!/usr/bin/env bash
# Installs the Valheim dedicated server + this panel natively, without Docker.
# Tested target: Debian/Ubuntu/Linux Mint. Safe to re-run — it updates the
# game and restarts the service without touching your world or settings.
set -euo pipefail

APP_ID=892970
SERVICE=valheim-panel
ROOT="${VALHEIM_ROOT:-$HOME/valheim-server}"
PANEL_PORT="${PANEL_PORT:-3030}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[ -f "$SRC/panel/server.py" ] || die "run this from a clone of the repo: git clone https://github.com/h4root/valheim-panel.git && cd valheim-panel && ./install.sh"
[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root (the script asks for sudo when it needs it)"
command -v apt-get >/dev/null || die "this script expects apt (Debian/Ubuntu/Mint). Install steamcmd and python3 yourself, then see README."
command -v sudo >/dev/null || die "sudo is required"

step "Installing packages"
sudo apt-get update -qq
if ! sudo apt-get install -y -qq curl ca-certificates python3 lib32gcc-s1; then
    sudo dpkg --add-architecture i386
    sudo apt-get update -qq
    sudo apt-get install -y -qq curl ca-certificates python3 lib32gcc-s1
fi

step "Preparing $ROOT"
mkdir -p "$ROOT/steamcmd" "$ROOT/install" "$ROOT/config" "$ROOT/logs" "$ROOT/backups"

if [ ! -x "$ROOT/steamcmd/steamcmd.sh" ]; then
    step "Downloading SteamCMD"
    curl -sL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz \
        | tar zxf - -C "$ROOT/steamcmd"
fi

step "Downloading/updating the dedicated server (this takes a few minutes the first time)"
# SteamCMD exits non-zero on success often enough that its code is useless —
# the binary check below is what actually decides whether this worked.
"$ROOT/steamcmd/steamcmd.sh" \
    +force_install_dir "$ROOT/install" \
    +login anonymous \
    +app_update "$APP_ID" validate \
    +quit || true
[ -f "$ROOT/install/valheim_server.x86_64" ] || die "SteamCMD finished but the server binary is missing — check the output above"
chmod +x "$ROOT/install/valheim_server.x86_64"

if [ ! -f "$ROOT/password" ]; then
    step "Server password"
    while :; do
        read -rsp "Password for players joining (5+ characters): " pw; echo
        [ "${#pw}" -ge 5 ] || { echo "too short"; continue; }
        break
    done
    ( umask 077; printf '%s' "$pw" > "$ROOT/password" )
    unset pw
fi

if [ ! -f "$ROOT/panel.json" ]; then
    step "Server name"
    read -rp "Name shown in the server list [My Valheim Server]: " name
    name="${name:-My Valheim Server}"
    python3 - "$ROOT/panel.json" "$name" <<'PY'
import json, sys
path, name = sys.argv[1], sys.argv[2]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"server_name": name, "world_name": "Dedicated"}, f, indent=2, ensure_ascii=False)
PY
fi

step "Installing the $SERVICE service"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=Valheim Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
Environment=VALHEIM_ROOT=$ROOT
Environment=VALHEIM_INSTALL_DIR=$ROOT/install
Environment=PANEL_PORT=$PANEL_PORT
ExecStart=/usr/bin/python3 "$SRC/panel/server.py"
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable -q "$SERVICE"

restart=yes
if pgrep -f "/valheim_server\.x86_64" >/dev/null 2>&1; then
    echo
    echo "A game server is running right now. Restarting the panel stops it too"
    echo "(the world is saved first, but players get disconnected)."
    read -rp "Restart now? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || restart=no
fi

if [ "$restart" = yes ]; then
    sudo systemctl restart "$SERVICE"
else
    echo "Left the running server alone. Apply the update later with:"
    echo "  sudo systemctl restart $SERVICE"
fi

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done. Panel: http://${IP:-localhost}:$PANEL_PORT"
echo "Open it, press Start, and share the join code it shows."
echo
echo "  status:  systemctl status $SERVICE"
echo "  logs:    journalctl -u $SERVICE -f"
echo "  update:  re-run this script"

if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    echo
    echo "ufw is active — allow the panel through it:"
    echo "  sudo ufw allow $PANEL_PORT/tcp"
fi
