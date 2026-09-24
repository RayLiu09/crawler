#!/usr/bin/env bash
set -euo pipefail

config_root="${XDG_CONFIG_HOME:-$HOME/.config}/job-collection-agent"
systemd_unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/desktop-collection-agent.service"
autostart_entry="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/job-collection-agent.desktop"

systemctl --user stop desktop-collection-agent.service 2>/dev/null || true
rm -f -- "$systemd_unit" "$autostart_entry"
systemctl --user daemon-reload

echo "service files removed; retained $config_root for explicit credential/data recovery"
