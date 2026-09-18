#!/bin/sh
set -e

INSTALL_DIR="${VALHEIM_INSTALL_DIR:-/server/install}"
mkdir -p "$INSTALL_DIR"

/opt/steamcmd/steamcmd.sh \
    +force_install_dir "$INSTALL_DIR" \
    +login anonymous \
    +app_update 892970 validate \
    +quit

exec python3 /panel/server.py
