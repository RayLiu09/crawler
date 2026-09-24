#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: install.sh --executable ABSOLUTE_PATH --config ABSOLUTE_PATH" >&2
}

executable_path=""
source_config_path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --executable) executable_path="${2:-}"; shift 2 ;;
    --config) source_config_path="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

if [[ "$(id -u)" -eq 0 ]]; then
  echo "install as the dedicated interactive desktop user, not root" >&2
  exit 1
fi
if [[ "$executable_path" != /* || ! -x "$executable_path" ]]; then
  echo "--executable must be an absolute executable path" >&2
  exit 1
fi
if [[ "$source_config_path" != /* || ! -f "$source_config_path" ]]; then
  echo "--config must be an absolute regular file path" >&2
  exit 1
fi
if [[ "$executable_path$source_config_path$HOME" =~ [\\\"] ]]; then
  echo "installation paths cannot contain backslash or double-quote characters" >&2
  exit 1
fi
if grep -Eqi '^[[:space:]]*AGENT_DEVICE_SECRET[[:space:]]*=' "$source_config_path"; then
  echo "plaintext AGENT_DEVICE_SECRET is forbidden for managed installation" >&2
  exit 1
fi
if ! grep -Eqi '^[[:space:]]*AGENT_DEVICE_SECRET_KEYRING_SERVICE[[:space:]]*=' "$source_config_path" || \
   ! grep -Eqi '^[[:space:]]*AGENT_DEVICE_SECRET_KEYRING_USERNAME[[:space:]]*=' "$source_config_path"; then
  echo "managed installation requires both keyring locator fields" >&2
  exit 1
fi

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
config_root="${XDG_CONFIG_HOME:-$HOME/.config}/job-collection-agent"
systemd_root="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
autostart_root="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
agent_config="$config_root/agent.env"
launcher="$config_root/launch-agent.sh"
runbook="$config_root/RUNBOOK.md"

install -d -m 0700 "$config_root" "$systemd_root" "$autostart_root"
install -m 0600 "$source_config_path" "$agent_config"
install -m 0600 "$script_directory/../RUNBOOK.md" "$runbook"
install -m 0700 "$script_directory/launch-agent.sh.in" "$launcher"

escape_sed() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
sed \
  -e "s|@EXECUTABLE@|$(escape_sed "$executable_path")|g" \
  -e "s|@CONFIG_FILE@|$(escape_sed "$agent_config")|g" \
  -e "s|@RUNBOOK@|$(escape_sed "$runbook")|g" \
  "$script_directory/desktop-collection-agent.service.in" \
  > "$systemd_root/desktop-collection-agent.service"
chmod 0600 "$systemd_root/desktop-collection-agent.service"

sed \
  -e "s|@LAUNCHER@|$(escape_sed "$launcher")|g" \
  "$script_directory/job-collection-agent.desktop.in" \
  > "$autostart_root/job-collection-agent.desktop"
chmod 0600 "$autostart_root/job-collection-agent.desktop"

systemctl --user daemon-reload
"$launcher"
echo "desktop Agent installed for the current graphical user"
