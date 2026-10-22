#!/usr/bin/env bash
# Print the CHANGELOG.md section "## <component> <version> …" (up to the next "## "), for a GitHub Release.
set -euo pipefail
component="$1" version="$2"
awk -v head="## $component $version" '
  index($0, head) == 1 { on = 1; next }
  on && /^## / { exit }
  on { print }
' "$(dirname "$0")/../../CHANGELOG.md" | sed -e '/./,$!d'
