# Changelog

One section per component release, newest first; each `## <component> <version>` section becomes that tag's
GitHub Release notes. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions:
[SemVer](https://semver.org/).

## android — unreleased

Upgrading from 0.3.0, which was signed with a debug key: uninstall it once (`tv-agent tv shell pm uninstall
com.boujuan.tvagent`), then `tv-agent tv install tv-agent-android-<version>.apk`, which pairs again.

### Changed
- Releases are signed with the project's release key (certificate SHA-256
  `bd934e07d6784766bbfb3948bf3467ef9b2cb0c9c1146fdf9528dc76c8eb1e08`), so each release installs over the previous
  one. Local builds use it when `~/.gradle/gradle.properties` has it and the debug key otherwise;
  `build-and-install.sh` prints which.

## server 0.1.0 — 2026-10-22

First release.

### Added
- Daemon (`tv-agent serve`): LAN endpoint for the TV app (address- and token-checked) and a localhost API for the
  panel, the CLI and MCP.
- Speech-to-text with Parakeet TDT 0.6B v2 (sherpa-onnx, CPU).
- Decision rules as data (`~/.config/tv-agent/rules.json`): clean-up, fixed phrases, ordered pattern rules with
  converters, d1-3B intents (built-in and custom), apps and hooks. Checked before use, applied live, traced per
  decision. 107 labelled sentences (`tv-agent eval`).
- Liquid AI d1-3B (llama.cpp `/v1/systemone`) for intents and for choosing between candidate titles.
- TV control over ADB (one persistent connection, every argument quoted): apps, deep links, media keys, volume.
- Stremio: passwordless link login, library and Continue Watching, Cinemeta, your addons' streams ranked in code
  (cached debrid links, resolution and bitrate caps), playback in Just Player or Kodi, progress written back.
- Audio/subtitle rule per title (native-language titles without subtitles; otherwise chosen audio and an ordered
  subtitle list, including Chinese Simplified / Traditional).
- Activity history (`activity.jsonl`), session restore after restarts, MCP server (`tv-agent-mcp`, no shell tool).
- `tv-agent setup token|stremio|kodi|services`, `tv-agent tv host|shell|install|pair`, `doctor`, `say`, `do`,
  `streams`, `eval`, `logout`.

## panel 0.1.0 — 2026-10-22

First release.

### Added
- Qt desktop app with a tray icon (status and resource use on hover; open, speak, pause/resume, player, start/stop,
  quit), single instance, close-to-tray and start-at-login options.
- Overview: services with start/stop/restart and start-at-boot, live CPU / memory / GPU graphs, processes.
- Activity: typed and spoken commands (the computer's microphone via PipeWire), history with export, import, erase.
- Decisions: the decision tree as a diagram with a path tester, and editors for every rule; tests on unsaved edits;
  save applies live.
- Settings for the agent and for the panel itself.

## android 0.3.0 — 2026-10-22

First release (0.1 and 0.2 were development builds).

### Added
- "Listening…" pill opened by the remote's mic button: records with end-of-speech detection, sends to tv-agent,
  shows the transcript and the reply.
- Pairing at runtime (`tv-agent tv pair`): the APK holds no address or token.
- On-screen messages from the agent; the Phase 0 microphone probe.
