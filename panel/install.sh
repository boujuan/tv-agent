#!/usr/bin/env bash
# Add the TV Agent panel to the application menu, and with --desktop also to the Desktop, for this copy of the
# panel (a packaged install ships its own entry instead).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EXEC="${EXEC:-/usr/bin/python3 $HERE/tv_agent_panel.py}"
entry="$(sed -e "s|@EXEC@|$EXEC|" -e "s|@ICON@|$HERE/tv-agent.svg|" "$HERE/tv-agent-panel.desktop.in")"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
printf '%s\n' "$entry" > "$apps/tv-agent-panel.desktop"
echo "added $apps/tv-agent-panel.desktop"
if [ "${1:-}" = --desktop ]; then
  desk="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  printf '%s\n' "$entry" > "$desk/tv-agent-panel.desktop"
  chmod +x "$desk/tv-agent-panel.desktop"           # Plasma only runs executable desktop files
  echo "added $desk/tv-agent-panel.desktop"
fi
update-desktop-database "$apps" 2>/dev/null || true
