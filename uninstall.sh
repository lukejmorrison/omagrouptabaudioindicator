#!/usr/bin/env bash
# Remove the group-tab speaker user service.
# Leaves the shell plugin in place. Remove that with:
#   omarchy plugin remove wizwam.group-audio
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$ROOT/group_audio.py"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="groupbar-audio.service"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

if [[ ! -f $UNIT_PATH ]]; then
  echo "no unit at $UNIT_PATH"
  exit 0
fi

execstart=$(grep '^ExecStart=' "$UNIT_PATH" || true)
case "$execstart" in
  "ExecStart=$SCRIPT"|*group_audio.py|*groupbar-audio)
    systemctl --user disable --now "$UNIT_NAME" || true
    rm -f "$UNIT_PATH"
    rmdir "$UNIT_DIR/graphical-session.target.wants" 2>/dev/null || true
    systemctl --user daemon-reload
    echo "removed $UNIT_NAME"
    ;;
  *)
    echo "refusing to remove $UNIT_PATH ($execstart)" >&2
    exit 1
    ;;
esac
