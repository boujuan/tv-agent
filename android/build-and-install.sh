#!/usr/bin/env bash
# Build the TV app and install it on the TV with tv-agent: push, grant the microphone, allow TCL's auto-start,
# and pair it (the agent's address and token go to the app at runtime; nothing secret is built into the APK).
#
#   android/build-and-install.sh            build and install
#   android/build-and-install.sh --build    only build (app/build/outputs/apk/release/app-release.apk)
#
# Needs JDK 17 and the Android SDK (JAVA_HOME / ANDROID_HOME, defaults below) and tv-agent on PATH or in
# ../server/.venv (TV_AGENT overrides).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export JAVA_HOME="${JAVA_HOME:-$HOME/.local/share/jdk-17}" ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
export PATH="$JAVA_HOME/bin:$PATH"
cd "$HERE"
[ -f local.properties ] || echo "sdk.dir=$ANDROID_HOME" > local.properties
APK=app/build/outputs/apk/release/app-release.apk
rm -f "$APK"
./gradlew assembleRelease -q --console=plain 2>&1 | grep -v "SDK XML" || true
[ -f "$APK" ] || { echo "build failed" >&2; exit 1; }
echo "built $APK"
[ "${1:-}" = --build ] && exit 0
TV_AGENT="${TV_AGENT:-$(command -v tv-agent || echo "$HERE/../server/.venv/bin/tv-agent")}"
"$TV_AGENT" tv install "$APK"
echo "Undo: $TV_AGENT tv shell pm uninstall com.boujuan.tvagent"
