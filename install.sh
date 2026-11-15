#!/usr/bin/env bash
# Install the user service that decides which group pill is playing audio.
# Does not edit shell.json or Hyprland. Enable the painter separately:
#   omarchy plugin enable wizwam.group-audio
#
# Usage:
#   ./install.sh
#   ./install.sh --reinstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$ROOT/group_audio.py"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="groupbar-audio.service"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

REINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --reinstall) REINSTALL=1 ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f $SCRIPT ]]; then
  echo "missing $SCRIPT" >&2
  exit 1
fi
chmod +x "$SCRIPT"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

mkdir -p "$UNIT_DIR" "${XDG_STATE_HOME:-$HOME/.local/state}/omarchy"

write_unit() {
  cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Speaker mark for the Hyprland group pill that is playing audio
After=graphical-session.target pipewire.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=$SCRIPT
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
PassEnvironment=HYPRLAND_INSTANCE_SIGNATURE
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
EOF
  echo "wrote $UNIT_PATH"
}

if [[ -f $UNIT_PATH && $REINSTALL -ne 1 ]]; then
  if grep -qxF "ExecStart=$SCRIPT" "$UNIT_PATH"; then
    echo "unit already points at $SCRIPT"
  else
    echo "unit exists with a different ExecStart; pass --reinstall to replace $UNIT_PATH" >&2
    grep '^ExecStart=' "$UNIT_PATH" >&2 || true
    exit 1
  fi
else
  write_unit
fi

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"

enabled=$(systemctl --user is-enabled "$UNIT_NAME")
active=$(systemctl --user is-active "$UNIT_NAME")
echo "enabled=$enabled"
echo "active=$active"

if [[ $enabled != enabled || $active != active ]]; then
  systemctl --user status "$UNIT_NAME" --no-pager || true
  exit 1
fi

if journalctl --user -u "$UNIT_NAME" -n 40 --no-pager | grep -q 'Traceback'; then
  echo "service traceback" >&2
  journalctl --user -u "$UNIT_NAME" -n 40 --no-pager >&2
  exit 1
fi

echo "ok"
