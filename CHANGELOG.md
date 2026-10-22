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

## server — unreleased

Rules version 2. A `rules.json` you saved before keeps working but doesn't get these: Reset to defaults in the
panel's Decisions tab, then redo your changes.

### Changed
- Every action has its own d1 intent (pause, resume, stop, skip forward/back, seek to, restart, previous episode,
  set volume, louder, quieter, mute, unmute…), each described with one example. d1 only sees the descriptions,
  and "previous episode", "go back to the start" or "volume twenty" used to have no matching option. On the 107
  original sentences d1 on its own went from 84 to 100 right; a d1 answer now takes ~0.65 s instead of ~0.2 s
  (longer prompt), but the new rules leave it only ~4% of commands.
- New pattern rules: "play / put on / watch / start + a title" and a title said with a season or episode, so the
  commonest commands are decided in under 0.1 ms instead of ~0.2 s, and titles made of command words ("Up",
  "Stop Making Sense", "Pause") are played instead of obeyed.
- More clean-up ("okay", "and then", "thanks", "un pause"), more fixed phrases, "a bit louder", "the previous one",
  "rewind to the beginning"; "skip back …" and "skip to …" no longer read as skipping forward; "the tenth episode
  of season five" and "Next of Kin" are read correctly.

- Stream choice: the right audio language first, then resolution, then AIOStreams' score for the release (its ★),
  then already in your debrid library, then bitrate. The facts come from AIOStreams' structured `streamData`
  instead of being guessed from its names. The link check no longer mistakes AIOStreams' "can't play" redirect
  videos for a working file.

### Added
- `eval/commands.jsonl`: 654 generated, labelled commands (476 for tuning, 178 held out) from
  `eval/build_commands.py`; `tv-agent eval --file … --split dev|test` scores them by category. Current rules:
  654/654, with d1 needed for ~4% of them.

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
