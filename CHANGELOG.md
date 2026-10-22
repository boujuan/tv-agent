# Changelog

One section per component release, newest first; each `## <component> <version>` section becomes that tag's
GitHub Release notes. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions:
[SemVer](https://semver.org/).

## android — unreleased

Upgrading from 0.3.0, which was signed with a debug key: uninstall it once (`tv-agent tv shell pm uninstall
com.boujuan.tvagent`), then `tv-agent tv install tv-agent-android-<version>.apk`, which pairs again.

### Added
- A numbered list on the TV for "what was I watching?", "what's new?" and "new films", with posters: say "number
  2", or pick with OK or a number key. It stays visible while you speak (the pill opens beside it, bottom-right)
  and closes when playback starts, on BACK, or after 90 s (`ChoicesActivity`, opened over adb only).
- Streams the remote's microphone to tv-agent while you speak (TCP, `stream_port`, sent at pairing) and shows
  what it has understood so far; tv-agent decides during your pause and tells the app to stop. Falls back to
  sending the recording at the end when the stream isn't available (an older agent, a dropped connection).

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

- Kodi is started while the stream is being looked up, when it isn't running yet.

- d1 acts only when it's at least 0.65 sure and its answer is twice as likely as the next ("one two three" was
  taken for "next episode" at 0.32).
- Kodi: before each file, its preferred audio and subtitle languages are set from your rule and the files'
  "default" flags ignored (a Blu-ray started on its Ukrainian dub); tracks are refined once playback has settled
  and kept for 8 s against Kodi's own late changes (it turned subtitles off again after the resume jump).
- Volume is changed with the volume keys: setting the level through Android is ignored on this TCL while Kodi
  plays.
- The TV is woken before anything is shown or played, and when the TV app connects: commands from the laptop and
  mic presses on the screensaver opened apps behind it, hidden, frozen and without the microphone.
- "Clay" at the start is read as "play" (the remote's mic opens a moment late).
- "stop" stops and goes back to Home. "play" or "resume" with nothing playing resumes Kodi when it's paused in
  the background, and otherwise continues what you were watching.
- Kodi is brought to the front when it was left running in the background (a film played behind Home).
- Audio: commentary and audio-description tracks are never picked, and Spanish prefers Castilian. Subtitles:
  plain before SDH / hearing-impaired, Castilian before Latin American.
- A title said has to match a library item's whole name (or the name before its subtitle): "dune parte dos" no
  longer plays your "Dune".
- d1 chooses between 33 intents (~0.88 s instead of ~0.74 s, for the ~4% of commands that reach it).
- Faster starts. Kodi is brought up at once and gets ready during the stream search; the stream lookup (3-6 s
  at AIOStreams) starts as soon as the video is known, alongside Cinemeta, and its answer is reused for 30
  minutes; the link check runs while Kodi gets ready and is done at the best link's answer; Kodi gets the file's
  final address, so its requests skip AIOStreams' redirect (Kodi's part went from ~4 s to ~2.5 s). Measured on
  a 1080p remux: 3.5 s from command to picture when the streams are known, ~5.9 s when AIOStreams has to look;
  a list pick took ~11 s before. Your addons and library are refreshed when the mic opens. `tv-agent stats`
  shows the stages (`start:streams`, `start:kodi`, `start:link`, `start:open`).
- "Stop" reads Kodi's exact position before stopping, so the saved progress and a "continue" right after resume
  there (the player is polled every 10 s).

### Fixed
- Progress could go unsaved when something new started right after a stop: the previous session's follower was
  cancelled in the middle of its last write. It's now told to stop and finishes the write.

### Added
- The TV app's audio stream (port 8767): decided ~0.25-0.3 s after your last word for complete commands
  ("pause", "next episode", "… season 3 episode 2"), ~0.45 s for "volume twenty", ~0.7 s for a title alone, which
  is often followed by a season. Earlier it was 0.7 s of quiet plus the upload and transcription of the whole
  clip.
- Subtitles from your Stremio subtitle addons in Kodi: asked like Stremio asks (the chosen file's hash, size and
  name), and added with `Player.AddSubtitle` for the languages of your list the file doesn't have, the one to show
  last. The file's own subtitles still come first for a language it has.
- `eval/commands.jsonl`: 832 generated, labelled commands (600 for tuning, 232 held out, 49 in Spanish) from
  `eval/build_commands.py`; `tv-agent eval --file … --split dev|test` scores them by category. Current rules:
  832/832, with d1 needed for ~4% of them.
- Binge mode, on by default ("binge mode on/off", `binge` in config.toml): when an episode ends, the next one
  starts by itself. It's found, its stream chosen and its link checked 20 s into the episode, so it starts in
  ~3-4 s (measured: The Office S3E2 → S3E3). After a film or a season's last episode, back to Home instead of
  Kodi's menus.
- Sleep timer: "stop after this episode", "turn the TV off after this one", "stop in 30 minutes", "turn off in an
  hour", "cancel the sleep timer". "After this one" wins over binge mode for that episode.
- Subtitles and audio on request in Kodi: "subtitles on/off", "Spanish subtitles", "subtitles in English",
  "original audio", "Spanish audio". A subtitle language the file lacks comes from your subtitle addons.
- "What did they say?": back 10 s, with subtitles for a moment when they were off.
- Lists on the TV: "what was I watching?" (Continue Watching), "what's new?" (the newest episode of each of your
  shows that you haven't got to), "new films" (your addons' "Latest" list); "number 2", "the second one" or the
  remote plays from it (`/v1/choose` for the TV app).
- "Play the latest episode of The Bear": the newest episode out.
- A choice of speech model (`stt_model`, panel: Settings): `parakeet-v2` (English, the default) or `parakeet-v3`
  (25 European languages, Spanish included, found by itself). `tv-agent setup stt [name]` lists them, downloads
  one (checked against the SHA-256 GitHub publishes for it) and uses it.
- Spanish commands: "pausa", "siguiente capítulo", "pon la casa de papel temporada dos capítulo tres", "sube el
  volumen", "¿qué hay de nuevo?", "salta la intro"… A clean-up layer turns them into the English the rules know
  (titles stay as said, Spanish numbers are read); 49 Spanish test sentences. They need a multilingual speech model
  to be heard as Spanish.
- Liquid's hosted d1 as a fallback when the local d1 doesn't answer (`hosted_d1`, `tv-agent setup liquid`).
- An LLM after d1, for commands nothing else understood (`llm_fallback`, `llm_model` =
  google/gemini-2.5-flash-lite through OpenRouter, `tv-agent setup openrouter`): one action in a strict JSON
  shape, zero-data-retention providers only; a title has to be said or be among up to 10 of your library's
  titles that sound like it; everything is checked like the rules' answers.
- "That's wrong" ("eso está mal") marks the last command in the history; the panel teaches it the right action,
  saved as that exact sentence (`learned.json`, checked before the rules, apart from `rules.json`). `tv-agent
  review` lists marked and not-understood commands, with the LLM's guess when one is set up.
- "Skip the intro", "skip the recap", "skip the credits" (the next episode in binge mode, else Home), from
  TheIntroDB's timestamps for a file of this length and IntroDB's; it only jumps when you're in that part, so
  timestamps from another cut can't send you somewhere wrong; a chapter named "Opening" or "Recap" counts too.
  "Auto skip on" (off by default, `auto_skip`) skips intros and recaps by themselves when their timestamps are
  sure: made on a file within 2 s of this one's length by 3 or more people at confidence ≥ 0.5, or IntroDB
  agreeing within 2 s (identical numbers don't count: likely copied). Both services get the episode's IMDb id and
  the file's length.
- Your Stremio library by voice: "add this to my library", "remove Friends from my library", "mark this as
  watched", "mark The Bear season 3 as watched", "mark it as unwatched". Episodes get Stremio's own watched marks,
  lined up with the episode list your Stremio uses (its first metadata addon); marks that can't be lined up are
  left alone. Episodes watched past 90% get their mark too.
- Title search also asks your metadata addons (AIOMetadata), which know Spanish and other names and leave out
  obscure namesakes: "el hoyo", "padre de familia", "las chicas del cable" and "dune parte dos" now play the right
  title (27/27 Spanish and 34/34 English test titles). A slow Cinemeta search (4-8 s when its cache misses) no
  longer holds a play up when another source has the exact name; Cinemeta's popularity settles films against
  series of the same name ("regreso al futuro" is the 1985 film).
- The volume halves while the pill listens (the remote's mic hears the TV) and comes back after: Kodi's own
  volume while Kodi plays, otherwise the TV's. Nothing is paused; "louder" said meanwhile starts from the level
  before.

## panel — unreleased

### Added
- Settings: a Voice card: the speech model (with "Download and use" for one not on this computer yet), binge
  mode, auto skip, and the cloud fallbacks (Liquid's hosted d1, an LLM) with whether their keys are set. The tab
  reloads when shown, so a binge mode changed by voice isn't overwritten.
- Activity: "Only wrong or not understood"; right-click a command to mark it wrong, teach it the right action
  (type what you meant, see how the agent takes it, save) or forget what it was taught.
- Decisions: explains sentences you taught, answers from Liquid's hosted d1 and the LLM step.

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
