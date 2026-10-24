# Changelog

One section per component release, newest first; each `## <component> <version>` section becomes that tag's
GitHub Release notes. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions:
[SemVer](https://semver.org/).

## server — unreleased

### Added
- A language the file playing lacks ("ponlo en español", "switch to English audio", "Chinese subtitles" when
  neither the file nor your subtitle addons have them): the best other copy that has it takes over at the same
  moment, in TV Agent Player and Kodi; the TV says it's looking, and when no copy has it what's playing goes on.
  What AIOStreams read from each file's tracks (or its name says) is used first; the best few copies that say
  nothing have their tracks read from the file's Matroska header (one range read each, all at once).
- Audio and subtitles asked for by voice hold for the title (its next episodes, a copy switched to) for 12 hours,
  and with subtitles shown only for audio that isn't yours, switching the audio turns them on or off to match.
- Language settings by voice: "from now on, audio in Spanish", "a partir de ahora sin subtítulos", "always English
  subtitles", "subtítulos solo cuando no sea en español"; what's playing follows. Also understood by the LLM step.
- `audio_language = "original"`: each title in the language it was made in (its files' original track when
  AIOStreams says, else its first country), and streams with it first. `subtitles` (`"foreign"`, the default: only
  while the audio isn't `native_language`; `"always"`; `"off"`), also from the phone (`/v1/settings`, with
  `choices.audio_language` and `choices.subtitles_when`).

### Changed
- Subtitles follow the audio that actually plays: a Spanish-made title whose file has only English audio gets
  subtitles, and a Spanish dub gets none. Streams ranked for the audio wanted also read Torrentio-style flags and
  filename language words.
- Voice tests: `scripts/bench_voice.py` replays the commands you said to the TV (kept with `keep_recordings`)
  through speech to text and the decision steps, set up as the running agent has them, and judges each against
  what it should have done: action right, transcript right and word error rate, timings and cost per speech model
  (`--stt local,parakeet-v2,<OpenRouter model>,heard,text`; a hosted one is sent the language hint and names the
  agent sends it, your library's titles included). `--save` a run and `--compare` a later one to see regressions and
  fixes (exit 1 on a regression). Nothing is sent to the TV.
- `--seed` labels the kept recordings from the history: the action decided when it worked and wasn't marked
  "that's wrong", or what you taught it. Labels live in `~/.local/share/tv-agent/voice-tests/labels.jsonl`, never
  in the repo, editable by hand, each recording hard-linked beside them so it outlives the agent's last-300
  rotation and erasing the history (`voicetests.py`).
- `bench_voice.py --stt MODEL@es,MODEL@en,MODEL@none`: hosted models with another language hint (and its names) in
  the same run; `--transcripts run.json` decides on a saved run's transcripts (a rules change measured without
  speech to text's cost or wobble); any label field can be `<field>_any` ("episode_name_any", "seconds_any").

### Changed
- Spanish and misheard commands from 190 of your recorded ones (2026-10-24): "Pon" heard as "Pong", "Paul",
  "Ponga" ("Paul Dune", "Pong: Rebel Moon") is "pon"; "Paws", "Pulse", "Pose", "Paul" on their own are "pause";
  "Este al minuto 20" (vete), "minuto 30", "vete a una hora", "vete a la primera hora", "vete a los 20 minutos",
  "vete al principio (del episodio)", "pasa la intro", "pásate la introducción", "volumen 25" without "a", "put
  volume 50", "let's skip the intro", "play the moment where…" (a scene, not a title called "The Moment"), "the
  moment in this episode where…", "Open X in Twitch". The LLM: going to an episode ("¿puedes ir al episodio en el
  que…?") is playing it, not a scene search, and it gives a season or episode number only when you said one (it
  invented S2E4 for Community's dark-elf episode). On the recordings, with AssemblyAI and a Spanish hint: 151 →
  160 of 190 right on the same transcripts.
- When the words so far can't end a command ("… throws the crystal at", "¿puedes ir al momento donde disparan al
  señor", "cambia los subtítulos a"), the agent waits for up to 1.5 s of quiet instead of 0.72 s: you pause before a
  name, and 12 of the 190 commands were cut short there.
- Your Twitch favourites go to the speech model right after your own names, ahead of what you watched before:
  those titles filled all 100 places, so "Ibai" was never sent (heard as "eBay", "eBuy").

### Fixed
- No voice, no transcription: when the remote's mic sends near-silence (it did for 15 minutes on 2026-10-24, the
  film faintly behind it), nothing is sent to speech to text, so a line of dialogue can't become a command (“Ya no
  me oigo”, heard in that near-silence, turned the volume up).

## panel — unreleased

### Added
- Settings › Languages: the audio can be the original language (the one each title was made in), and **Subtitles
  show**: only when the audio isn't your native language (as before), always, or off.
- Activity tab: right-click a spoken command, **Add to voice tests…**: what it should do (what it did, or was
  taught) and the words said, checked by you. Erasing the history says that voice tests keep their own audio.

## server 0.9.0 — 2026-10-24

### Added
- Pictures for TV Agent Player's held ← and → (player 0.5.0, its `thumb_req`), made on this computer: the TV can't
  decode a second 4K frame while the film plays. The file's Matroska index is read once, 10 s after it opens; each
  picture is then one range read of the keyframe at or before the target, decoded and tone-mapped by ffmpeg from a
  pipe (libplacebo, so Dolby Vision profile 5 looks right too; zscale if libplacebo can't run) into a ~3 KB JPEG,
  and kept per keyframe. On the TV a picture showed 0.45 s after the player asked (median), with ~0.35 s of ffmpeg
  CPU each here. The link never goes on ffmpeg's command line. Needs `ffmpeg` (with libplacebo for HDR at its best);
  files that aren't Matroska get no pictures.

## android 0.9.1 — 2026-10-24

### Fixed
- Pressing the mic button again while the pill listened did nothing. About half a second after the first press the
  TV opens the remote's own mic for the pill (Google's remote service), and while it's open this remote (RC833)
  ignores its mic button: no key, nothing else the TV or an app can see. OK now stops listening and sends at once;
  the mic button still does the same in the rare case the remote's mic didn't open, and Back still cancels.

## server 0.8.1 — 2026-10-24

### Fixed
- A test build on the TV ("0.2.0-test", "0.9.0-dev") is compared as the version it is, just before its release:
  TV Agent Player's 0.2.0-test had been offered 0.1.1 as an update (and `updates = "auto"` could have tried it).
  `GET /v1/updates` says `ahead` for an app newer than its latest release.

## panel 0.8.0 — 2026-10-24

### Changed
- Settings › Updates has a row and a button for each part: the voice agent, the panel, the TV app and TV Agent
  Player. The button says what it would do now: *Update to X* when a release is out, *Restart to use X* when this
  computer has newer code than the voice agent or the panel running (after an update, or a pull), else a greyed
  *Up to date*. "This computer" is split into the voice agent's and the panel's rows; a test build on the TV newer
  than the latest release is said so. Needs server 0.8.1 for that.

## server 0.8.0 — 2026-10-24

### Added
- Each language hint can have its own names for the hosted speech model (`stt_vocabularies`, e.g. `{es = […],
  en = […]}`): Spanish, English and no hint each send their own list, since the names' language leans how
  borderline words come out. A hint without a list of its own, and no hint, use `stt_vocabulary` as before, so
  nothing changes until you give a language its own. `POST /v1/pipeline` sets one language at a time (`null`
  removes it); `GET` adds `stt_vocabularies`, `stt_vocabulary_language` (the hint in use) and
  `stt_vocabulary_sent_by_language` (what each hint would send).

### Changed
- Cinemeta's names for your library's titles are always read (in the background, once per title), so the English
  list is ready whichever hint is on.

## panel 0.7.0 — 2026-10-24

### Changed
- Decisions › Speech to text: the vocabulary field shows and saves the names of the language hint in use, and says
  whose they are. **Show all…** has a list per hint (Spanish, English, no hint; the one in use first), each with
  the library's titles as that hint sends them; a hint without its own list shows the no-hint names until you
  change them. Needs server 0.8.0.

## server 0.7.0 — 2026-10-24

### Added
- The activity history's `trace` has the whole command: after the decision's steps, what the action did, in order,
  each with what it found, its time (`ms`) and when it ended (`at`, from the command's start). Finding the title:
  your library, each title search with its sources and candidates (scores after popularity), the LLM's
  sound-alikes, each choice and how it was made (d1 with its model and confidence, popularity, or a clear lead),
  season checks, the pick. Starting it: the streams (how many, and whether they were looked up ahead), the link
  check, the player coming up, the load and the first frame. A d1 that picked the title shows there; until now only
  a d1 that decided the command did. Never a link.
- `POST /v1/decide` with `"title": true` also finds a play request's title as a start would (nothing is looked up
  for streams, nothing reaches the TV), with its steps in the trace and the title found: the panel's Explain.

### Changed
- `details.title` is the summary (said, picked, imdb, by, ms); the candidates and choices are in the trace.

## panel 0.6.0 — 2026-10-24

### Added
- Decisions: a row under the decision steps shows what happens after the decision: how a title is found (your
  library, the title search, the LLM's sound-alikes, the pick, the title) and how it starts (streams, link check,
  player up, load, first frame), each step lit when it ran with what it found and its time, and a line per step
  under the diagram. When d1 picked the title, a dashed line goes from its card to the pick, with its confidence.
  Explaining a typed sentence finds its title too (nothing is played; needs server 0.7.0).
- Activity: double-clicking a command (or "Explain in Decisions") shows what happened, from its recorded trace, not
  a new decision; "Decide it again now" in its menu does the old thing. Commands from before the trace was kept are
  decided again.

## server 0.6.0 — 2026-10-24

### Added
- TV Agent Player's loading and pause screens get what they show (the player's release that has them; its
  PROTOCOL.md, `loading` and `info`). As soon as the title is known, a `loading` command with the title, a subtitle ("2024 · 2 h 46 min", or
  "S4E1 · <episode name>"), what was heard and Cinemeta's artwork, then each step: "Finding streams…", "Checking
  the link…", "Starting…". A start that fails says why there for 3 s, then the player closes (or, with something
  else playing on, only the screen goes). `load` and `preload` carry `info` for the pause screen: the plot, the
  addon that found the stream (AIOStreams' own source, e.g. Comet), its description without links, the debrid
  service, size, release group, and the badges the file's header can't give the player (Atmos, DTS:X, HDR10+, IMAX,
  remux). A player without these screens answers "unknown command" once and isn't sent them again; it starts as
  before.
- Each start's record in the activity history (`details`), for debugging and fine-tuning later: how the title was
  found (what was said, the search's candidates with their scores, the LLM's sound-alikes, each choice with how it
  was made, d1's confidence and time, the time of each step), the stages (streams, the link check, the player's
  screen up, the load, and the first frame as the player measured it) and the stream played (addon and its source,
  name, file name, size, resolution, release, debrid, AIOStreams' score, how many there were). Never a link.
  `tv-agent stats` has `start:title` and `start:first_frame` too.

### Changed
- Faster starts. Measured without the TV from the decision to the streams `load` waits for (the link check ~0.7 s
  and the player's ~1.3 s to the first frame come after), medians of 3-6 runs:
  - A misheard title: the LLM's sound-alikes are looked up as soon as it answers, side by side with the search for
    what was heard, which stops waiting for its slow sources once one is found; and when the search for what was
    heard has the sound-alike by name, no d1. A misheard name Cinemeta has never been asked (2026-10-24: "paul the
    boys", 7.2 s to the title): 3.5 s → 1.5 s to the title, 9.5 s → 2.2 s to the streams. "doom part two": 1.6 s →
    0.6 s to the title.
  - No d1 when the leader is certain once popularity counts: said exactly and 15 ahead (the Oppenheimer film
    against the 1980 series; d1 had picked it at 0.95-0.97). For a closer call, the leader's streams are looked up
    while d1 decides. "shogun": 1.15 s → 0.77 s to the title.
  - Continue Watching's first two items have their streams looked up when the mic opens (kept 30 minutes): carrying
    on waits 0 s for them instead of 2.2 s (1-8 s).
  - The stream lookup goes on without a slow addon once another has answered with a stream that fits your rules
    (the best resolution they allow, in the right language): it gets 0.5 s more. An addon that hung held a start for
    its 8 s timeout, twice.
  - A file known to be another episode under its number is looked up at the same time as the episode's own streams
    instead of after them. The episode check starts 5 s after the file opens and reads the link already checked,
    instead of resolving it again during the start.
  - AIOStreams' own answer (2-8 s, every time) is now most of a start.

### Fixed
- Back on the TV's pill cancels (TV app 0.9.0): the app ends its audio with 0xFFFF in place of a frame's length,
  and the agent throws it away, even just after deciding, as long as nothing was done yet; it answers
  `{"cancelled": true}`. Before, the app closed the connection, which the agent took as the end of what you said, so
  it acted on it anyway.
- "Pon Rebel Mung" played "Rebelde (2022)": the LLM's "Rebel Moon" was found (Part One, by d1) and thrown away
  because its full name has a subtitle, so the search's pick for the misheard words won, after three d1 calls in a
  row. A sound-alike now counts by its name before a subtitle: Rebel Moon - Part One, 6 times in 6 (before: Part Two
  5 times, Rebelde once).
- "Paul the Boys, season four, episode one" played The Boys only by luck (Polo Boys or Pluribus 3 times in 6): a
  sound-alike of a series asked for by season must have that season, a title heard whole in what was said counts
  ("The Boys" sounds less like "paul the boys" than Polo Boys), the LLM's own order is kept, and d1 sees the whole
  sentence. The Boys 6 times in 6.

## android 0.9.0 — 2026-10-24

Back's cancel needs an agent newer than server 0.5.0 (main from 2026-10-24); an older one still acts on what it
had heard.

### Added
- The pill shows it's hearing you: seven bars move with your voice (a slow ripple in silence) where the level bar
  was. When listening ends (the agent decided in your pause, or you pressed the mic button again) they gather into a
  dot, then three dots while the agent works ("One moment…"), then a tick for done or a cross for not done, with
  the reply. Drawn at 30 fps at most and only while it moves; over 4K Dolby Vision in TV Agent Player no video frame
  was dropped (5 pills in 43 s, the same 2:3 cadence as without).
- Back while it's listening cancels: the bars drop away, "Cancelled", and nothing is done. It used to close the
  pill and the agent still acted on what it had heard, since it takes a closed connection as the end of what you
  said. Once the agent has decided, Back only closes the pill.

## server 0.5.0 — 2026-10-24

### Added
- The activity history says more about each command, for debugging and for training on later: the agent's
  version, which speech model heard it and where (`stt_model`, `stt_where`), the language hint and how many names
  went with it (`stt_language`, `stt_vocabulary`), the decision's `trace` (each step tried, with the model that
  answered, the LLM's or d1's) and what the action reports about itself (`details`). The log's "decided … after the
  last word" line names the speech model, its time and what failed before it.
- Phones are offered this computer's Tailscale address automatically (after the LAN address and `phone_hosts`), as
  TV Agent Player already was, and the listeners follow it when Tailscale starts, stops or moves (looked at every
  minute). `GET /v1/clients/me` returns the addresses the agent listens on for phones now, in the order to try, so
  the phone app keeps its list current when this computer's addresses change, without pairing again.
- `GET /v1/tv/status` lists the TV's own apps and players that run (TV Agent Player, Kodi, Just Player, the TV app):
  pid, CPU share over the same half second as the TV's, and memory.
- `GET /v1/clients` says which addresses the phones' listeners are on (`addresses`) and the agent's clock (`now`);
  `GET /v1/updates` says whether the TV app and the player are installed (`installed`).
- TV Agent Player's releases (boujuan/tv-agent-player, tags vX.Y.Z) are checked with the rest: `GET /v1/updates` has
  `components.tvplayer` (its version on the TV against the latest release), `POST /v1/updates/tvplayer` installs it
  over ADB with GitHub's SHA-256, and `updates = "auto"` does so by itself; never while the player plays, and never
  onto a TV that doesn't have it.
- **TV Agent Player** (`player = "tvplayer"`; [boujuan/tv-agent-player](https://github.com/boujuan/tv-agent-player)),
  a Media3 player of its own on the TV, driven over a TLS connection it opens to the agent (`player_port` 8770, the
  phones' certificate, its own `player_token`; `tv/tvplayer.py`). It's brought up during the stream lookup, loads
  each title in place, plays with your track rule applied before the first frame, jumps exactly in place (Dolby
  Vision too), reports state, position, tracks and the position it closed at instead of being polled, and draws
  add-on subtitles without rebuffering. In binge mode the next episode is opened in the background 2.5 minutes
  before the end and starts at once. A file that fails soon after opening gives way to the next copy where it was.
  While you speak, its own volume is lowered (no volume bar). Transport, skips, jumps to a scene, "replay",
  subtitle and audio changes, the phone app's now-playing and track lists, `/v1/state` and the sleep timer work with
  it as with Kodi; with it on screen, transport skips asking the TV which app is in front (~1 s).
- `tv-agent tv install` takes TV Agent Player's APK too (allowed to start, compiled ahead of time, paired);
  `tv-agent tv pair` pairs it as well when it's installed. `GET /v1/tv/packages` lists it.
- TV Agent Player is told all the agent's addresses, the LAN's first, then `phone_hosts` and this computer's
  Tailscale address while Tailscale runs, and the agent listens for it on each: it reaches the agent with the
  computer away from home too (needs the player from its 0.1.0).

### Changed
- The names sent to the hosted speech model go most likely first, since only 100 fit: what's playing and Continue
  Watching, then your vocabulary, what you watched (newest first), Twitch favourites and the rest of the library
  (newest added first). They follow the language hint: titles as your Stremio library names them with a Spanish
  hint, Cinemeta's original names with an English one, both without a hint. Sending both with a Spanish hint made
  4 of the 91 kept commands come out worse ("Pause" as "Paws"); command words aren't sent, the model hears them
  well.

### Fixed
- Hosted speech-to-text works again with a long watch history: the names it's asked to listen for (your
  vocabulary, then your Twitch favourites and recent titles) are capped at 100, which AssemblyAI takes; at 101 or
  more it refused every request (HTTP 400) and the laptop's Parakeet heard everything instead, worse ("Pulse" for
  "Pause", "Doom" for "Dune"). The log and the Activity tab now give the HTTP status of a hosted failure.
- TV Agent Player: a file the TV can't read is explained on the TV at once (the player's new `LOAD_FAILED`
  event, from player 0.1.1), e.g. "The TV can't look up this file's server: check its internet (DNS)", instead
  of a silent wait while it retries.
- A start that never got going (no position, no length) writes no progress to Stremio: a failed open had written
  0:00 over the resume point.
- The agent follows this computer's address on the TV's network. It listened only on the address it started with,
  so when the laptop moved from Ethernet to Wi-Fi the TV app's microphone and TV Agent Player could no longer reach
  it, and nothing said so. The LAN address is looked at every 5 s; when it changes, the TV app's, the player's and
  the phones' listeners move to the new one, the TV app and TV Agent Player are paired again over ADB (once the TV
  answers), and the TV shows "Voice agent now at …". The agent also pairs them at its start when they were last told
  another address (`paired.json`, no tokens; written by `tv-agent tv pair` too). `lan_host`, `tv_hosts` and
  `phone_hosts` edited in `config.toml` are picked up the same way. A listener that can't be bound (its port taken)
  is retried every minute instead of stopping the agent.
- `tv-agent tv install` timed out pushing any APK to this TV: its adbd announces 1 MB writes but never acknowledges
  one that big. Pushes now go in writes of at most 64 KB (a 1.1 MB APK in well under a second). A command allowed to
  print nothing for a while (the player's compile, ~6 s) isn't taken for a dead connection.
- Kodi updates for the TV (`kodi_versions.py`): `GET /v1/kodi` (installed, the newest build for the TV's ABI on
  Kodi's mirrors: 22.0 RC 1 now, backups), `POST /v1/kodi/update` (the panel only). An update backs up Kodi's
  settings, libraries and add-ons on the TV first (`/sdcard/tv-agent/kodi-<version>.tar`, ~18 MB) and installs the
  APK only with the SHA-256 the mirror publishes. A Kodi from Google Play, signed with another key, is replaced only
  when confirmed, with that backup restored. `POST /v1/kodi/switch {"to": "21"}` goes back from 22 (it converts its
  databases one way): uninstalled, 21 installed, the backup from 21 restored.
- `check_episodes` is one of the live settings (`/v1/settings`), for the phone app.

### Fixed
- `GET /v1/kodi` failed (500) once Kodi 22 was installed: Kodi names its version "22.0-RC1", now compared as the
  mirror's builds are (so the final 22.0 is offered over the RC too).
- An update started Kodi while the TV was in standby, and Kodi 22 RC1 crashes when its window goes while it starts:
  the TV is woken first, and with it still asleep Kodi is left to finish updating the first time it's opened.
- A backup before a Kodi update is checked to have just been written (and not tiny) before anything is installed.

## panel 0.5.0 — 2026-10-24

### Added
- Overview › Processes lists the TV's apps and players while they run (TV Agent Player, Kodi, Just Player, the TV
  app), with their CPU and memory, under this computer's services.
- Settings › Phones: each phone's connection (in use now, or last seen, on the home network or over Tailscale), the
  addresses the agent listens on for phones, and what's wrong (an old app, Tailscale not listened on).
- Settings › Updates: TV Agent Player can be installed from there when it isn't on the TV; the phone app row ticks
  when every phone has the latest release and names the ones that don't.

### Changed
- Decisions › Speech to text › Show all: says the order the names are sent in, and counts against the hosted
  model's 100 (AssemblyAI refuses more).
- Settings › Updates no longer has a Kodi row (the agent can still update Kodi: `POST /v1/kodi/update`).
- Settings › Updates: a TV Agent Player row, with its version on the TV, the latest release and "Update the player".
- Settings › Playback › Player: **TV Agent Player** (and the tray menu, the overview, About).
- Settings › Updates has Kodi too: the version on the TV, the newest one (Kodi 22 Piers, a release candidate with
  fixes for seeking on MediaTek decoders like this TV's) and "Update Kodi", with its progress. Needs the agent from
  this version.
- Settings › Playback: "Check each episode is the one asked for, by its subtitles" (`check_episodes`).
- Decisions › Speech to text › **Show all (N)…**: the whole vocabulary sent with each command in a window of its own,
  as the phone app has it: your names (find, add, remove), then Twitch favourites and your library's titles, the
  library switch at the top and the total against the hosted model's 200. Works with the voice agent stopped too
  (written to config.toml).
- Decisions: the "How a command is decided" diagram folds to its header (▲: the editors get the whole height) or
  takes the whole height (▼: the editors are hidden); click it again for both. Remembered.

### Fixed
- The mouse wheel scrolls the page instead of changing the number box or list under the pointer; only one clicked
  into first takes it. Scrolling through Settings had changed the TV app's port (8765 → 8761) and Save wrote it, so
  the TV couldn't reach the agent.

## android 0.8.1 — 2026-10-24

### Fixed
- Back closes the pill without reaching the app underneath. The pill closed as Back went down, and the
  release went on to the app below, which took it as its own Back: TV Agent Player closed, Stremio went back a
  screen.

## server 0.4.0 — 2026-10-23

### Added
- Each episode's file is checked to be that episode (`episode_check.py`): streaming rips follow the service's
  numbering, so Netflix's rips of Community season 2, which skip the pulled "Advanced Dungeons & Dragons", are one
  ahead from "S02E14" on. 25 s of the file's own subtitles are compared with the episode's from your subtitle addons
  (asked by episode id) and, if they differ, with its neighbours'. It runs while the start gets ready (6-8 s the
  first time, nothing after: files are remembered in `episode-checks.json`); a file found to be another episode
  gives way to a right one at the same moment ("That file was another episode under this number…"), and is offered
  where it belongs instead (Netflix's "S02E14" for aired 15, keeping 4K Dolby Vision). The next episode is checked
  while the current one plays. `check_episodes = false` turns it off.
- Jumps to a moment use the episode's subtitles kept from that check, corrected by how far their times are from
  the file's.
- Updates from GitHub (`updates.py`): `GET /v1/updates` compares the latest release of the agent, the panel, the TV
  app and the phone app with what runs; `POST /v1/updates/tv` downloads the TV app's release, checks it against
  GitHub's SHA-256 and installs it over ADB (Android refuses one signed with another key). `updates` in config.toml:
  "notify" (default) checks every 6 hours, "auto" also installs the TV app's release when it isn't listening, "off"
  never asks GitHub. Both repositories are public, so no login is needed.
- Each phone's app version (its `X-App-Version` header, or `app_version` in its audio stream's first line), kept with
  the phone and shown to the panel.

### Fixed
- Playing a file while another played in Kodi could give sound without a picture, a jump that ran to the end of
  the episode, or a Kodi that took files without playing them. On this TV the old file's video decoder isn't let go
  in time ("InstanceGuard locked" in Kodi's log), Dolby Vision or not. Kodi's file is now stopped, and its player
  gone, before the next one opens (about 2 s more when switching).
- Jumps in a Dolby Vision file ("go to the part where…", "back 10 seconds", a time) reopen it at that time
  instead of seeking: this TV's Dolby Vision decoder can fail the flush a seek needs (black picture, or the time
  running to the end; a known problem on TCL sets with Kodi 21 and 22). The same for any file while Kodi is behind
  another screen. A few seconds per jump; Dolby Vision is kept.
- A file with both Dolby Vision and HDR10+ gets Dolby Vision only in Kodi (MediaTek decoders show such hybrids black
  or unstable); every other file keeps both.
- A picture lost anyway (Kodi logging decoder failures nonstop while the sound goes on) gets Kodi restarted and the
  file reopened where it was, once.
- A play that did nothing and went back to Home: Kodi's player can hang inside the TV's Dolby Vision decoder while
  opening a 4K file, and from then on takes every file without playing it until Kodi is restarted (its log stops
  mid-line). A file that stops at 0:00 within 45 s of opening, with nothing in Kodi's log since, now gets Kodi
  restarted and opened again, once ("Kodi was stuck: restarting it").
- `tv-agent tv install --help` (or an APK path that doesn't exist) printed a traceback instead of the usage.

## panel 0.4.0 — 2026-10-23

Needs server 0.4.0 (the update checks and the phones' app versions come from it).

### Added
- Settings › Updates: the latest releases against this computer, the TV app and the phones; Check now; install by
  themselves, check and tell, or don't check (`updates`). "Update the TV app" installs its release over ADB through
  the agent; "Update this computer" moves this checkout to a newer release tag (a fast-forward of a clean `main`,
  `uv sync`, the agent restarted), never over uncommitted or unpushed work. Checked soon after the panel starts and
  every 6 hours; news shows in the status bar and as a notification.
- Settings › Phones shows each phone's app version, and when a newer one is out.

## android 0.8.0 — 2026-10-23

### Fixed
- The app no longer asks for audio focus while it listens. Asking as a voice assistant made the player pause when
  the pill opened and play again when it closed, which undid "pause" (and paused YouTube before "play"). tv-agent
  already lowers the volume while you speak.

## panel 0.3.0 — 2026-10-23

Needs server 0.3.0 (the language hint, the vocabulary and paired phones come from it).

### Added
- Decisions › Speech to text: the hosted model's language hint (your native language by default) and its
  vocabulary, the names it should hear as written (one per line, plus your library's titles and Twitch favourites
  if ticked), live at once.
- The hosted models are marked: * uses the language hint, ** the hint and the vocabulary (measured: the hint changed
  the answers of AssemblyAI, Voxtral, Deepgram, ElevenLabs and Whisper; only AssemblyAI takes a vocabulary).
- Settings › Phones: the paired phones (last seen, from where), rename, revoke, and **Pair a phone…** with a QR code
  (`qrencode`) or a code to type. Activity shows commands from a phone as 📱 Phone.

### Fixed
- Run checks with the voice agent stopped said the rules "couldn't run" (connection refused): the saved rules are
  tested here instead now, and the report says so. Decisions says the agent is stopped without an error.
- A dialog opened from a finished background task (pairing a phone, Run checks, Diagnose) held back every other
  result until it closed: the pairing dialog never saw the phone pair or the code expire. Each result now arrives
  as its own event.
- Decisions › Speech to text loaded this laptop's model without asking whenever a choice other than Hosted was
  picked, so choosing Hosted again kept offering to unload it. Now it asks both ways: unload when Hosted doesn't use
  it, load (now, or when first needed) when a choice does.

## server 0.3.0 — 2026-10-23

The default rules know the new `recommend` and `find_scene`. A `rules.json` you saved keeps working but doesn't get
them (the LLM still does both): Reset to defaults in the panel's Decisions tab, then redo your changes.

### Added
- Paired phones, for the TV Agent phone app (`clients.py`): pairing with a one-time code from the panel (QR code),
  a token per phone kept only as its SHA-256 (`clients.json`, mode 600), revoked at once; an HTTPS listener
  (`phone_port`, 8768) with the agent's own certificate, which the app pins, passing the panel's endpoints through
  for a phone's token (never the stream link, the rules editor or the list of phones); the TV app's audio stream
  over TLS (`phone_stream_port`, 8769). `phones`, `phone_port`, `phone_stream_port`, `phone_hosts` in config.toml.
  The TV's listeners and checks are unchanged.
- More of the local API (`remote.py`): what's playing with Kodi's tracks and the intro/recap/credits timestamps,
  choosing a track, the remote's d-pad, OK and number keys (and its gear and input buttons: the TV's settings menu and inputs panel;
  the launcher ignores MENU, INFO and SETTINGS), typing into the TV, the volume, the app list, the
  "what was I watching / what's new / new films" lists returned instead of shown, the activity history and kept
  recordings, and the playback and language settings, live.
- Commands from a phone are `phone` in the activity history.
- Suggestions by voice: "recommend something like Dark", "what should we watch?", "put on a comedy under 90
  minutes", "recomiéndame una serie de miedo", "algo como La casa de papel". The LLM suggests titles that fit, with
  what you watched lately as taste; each one must be found by the title search (Cinemeta and your metadata addon,
  so Spanish names work) and fit a length limit you gave (checked against its runtime), and what you've seen or
  are watching is left out. They're shown as a numbered list on the TV with posters, ratings and a reason; "number 2"
  plays it (a series where you were, or from its first episode). Rules, the LLM and the decision model all know the
  new `recommend` action. Needs the OpenRouter key; about $0.0004 and 3-7 s per request.
- Jump to a moment described in words: "go to the part where Mr. Burns gets shot", "vete al minuto en el que…".
  The subtitles of what's playing (your subtitle addons, English first) go to the LLM with their times; it says when
  the moment is (wordless ones placed between the lines around them), and the player jumps a moment before. On "Who
  Shot Mr. Burns?" it found 20:36, just before the shot; a moment that isn't there finds nothing. ~1-3 s, ~$0.0015.
- Episodes by name or by what happens in them: "Abre Community, capítulo Advanced Dungeons & Dragons", "the
  Simpsons episode where Mr. Burns is shot". The LLM fills a new `episode_name`; the agent matches episode names
  itself, and for a description lets the LLM pick from the episodes whose name and summary share its words (Spanish
  descriptions work; misremembered details too). The rules hand such sentences to the LLM instead of searching for
  the whole description as a title.
- Hosted speech-to-text takes a language hint (`stt_language`: your native language by default; a Spanish hint
  keeps English commands intact, an English one garbles Spanish) and a vocabulary (`stt_vocabulary`, plus your
  library's titles and Twitch favourites with `stt_vocabulary_library`), passed to AssemblyAI as its keyterms
  ("Better Console" → "Better Call Saul", "este remedio" → "Stremio"). AssemblyAI Universal-3.6 Pro is listed
  first: on 30 real commands it heard 27 right with the hint and 30 with the vocabulary, ~0.26 s, ~$0.0002 each,
  and nothing on silence (Voxtral makes up sentences there).
- `GET /v1/pipeline` shows them, and every name sent (`stt_vocabulary_sent`: yours, then Twitch favourites and
  library titles); `POST` changes them. Your library's titles are read when the agent starts, so the
  first command after a restart has them too.

### Changed
- Play, pause and stop go to the app on screen: "play" while YouTube was in front brought Kodi up and resumed it.
  Kodi is only woken when it, or the home screen, is in front.
- A lone "please" (a misheard "pause" or "play") pauses what plays and plays what's paused (`toggle`), instead of
  always pausing.
- "local-first" speech-to-text also asks the hosted model when this laptop's model writes in another script
  ("Пауса", "Ютуб"): Parakeet guesses the language every time and can't be limited.
- Stremio is also "estremio", "streamio".
- `POST /v1/pipeline` with `stt_loaded` leaves the local model as asked when `stt_where` changes in the same request
  (it was loaded anyway for any choice but "hosted").
- Paired phones, found in review before release:
  - a phone opening a list replaced the list on the TV, so "number 2" or the remote's keys then picked from the
    phone's list; phones now keep their own;
  - pairing tries are limited per address, and only while a pairing is open: anyone on the network could keep the
    phone out with junk tries, and five wrong codes closed it for everyone (now: five per address, 50 in all);
  - a phone can't set `d1_model` or `stt_model` (paths on this laptop: the answer told whether a file exists);
  - junk on the phones' audio port is answered "unauthorized" instead of logging an error.
- Rules: a "title" over 12 words is a description, for the LLM.

### Fixed
- A film's runtime given as "1h 39min" was read as 39 minutes (the length used when Kodi doesn't report one).
- Stopping or restarting the agent after a phone had been connected took over 10 s, so systemd killed it: a phone
  that had gone to sleep never answered the closing of its connection. Open connections now get 2 s.

## android 0.7.0 — 2026-10-23

Upgrading from 0.3.0, which was signed with a debug key: uninstall it once (`tv-agent tv shell pm uninstall
com.boujuan.tvagent`), then `tv-agent tv install tv-agent-android-<version>.apk`, which pairs again. Use it with
server 0.2.0: the lists, the help screen and the streamed microphone come from it (with 0.1.0 the app sends the
whole recording instead).

### Added
- "What can you do?" ("¿qué puedes hacer?"): every command as a pattern with its ‹placeholders› set apart, by
  section, beside what it does, in the language you asked in; a scrolling list on the TV (0.6.0, patterns 0.7.0).
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

## server 0.2.0 — 2026-10-22

Rules version 2. A `rules.json` you saved before keeps working but doesn't get these: Reset to defaults in the
panel's Decisions tab, then redo your changes. Saving rules identical to the defaults now removes `rules.json`, so
they keep following the defaults as these improve.

### Changed
- Rules reviewed: apps are matched by their spoken name more strictly ("start it" no longer opens Twitch, "go to
  the movies" Movistar+); "put on YouTube" / "pon Twitch" open the app instead of searching for "put"; fillers
  ("ok tv", "oye", "please") go before the clean-up fixes, and "so", "now", "right", "and then" are removed only
  before a command, so "Now You See Me" keeps its first word; "go to sleep in 20 minutes" is a timer, not a seek;
  "skip ahead" / "skip the intro" aren't "next episode"; "volume up to 40", "turn it up to 30", "increase the
  volume", "too loud", "go back 10", "fast forward 90", "pick two", "first one", "set sleep timer 30", "cancel
  timer". Spanish: "pon el capítulo cinco", "la temporada dos de The Bear", "capítulo siguiente", "pon el
  anterior", "pon el audio en inglés", "súbelo", "bájalo un poco", "volumen a tope", "pon el segundo", "el número
  tres", "una hora y media" (90 minutes, was 60), "vuelve a empezar", "sigue viendo", "salta treinta segundos",
  "pon a ibai en twitch", and "pon X y apaga la tele cuando acabe" as two commands.
- New apps: the TV's Settings ("abre ajustes"), YouTube Music, RTVE Play and TDTChannels. New decision-model
  intents for "that's wrong", auto skip on/off and new films.
- When no model answers (LLM and decision model off or unreachable) and the offline intent is play_title, the
  sentence is played only if a search finds that exact title; otherwise "Didn't get that: …".
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
- The llama-d1 unit quotes its llama-server and model paths (a path with a space or a % broke it); rewrite it once
  with `tv-agent setup services`. `tv-agent setup kodi` keeps a piped password's own spaces.
- `GET /v1/tv/status`'s "app in front" came from the focused window, empty while the screen is off; it's the
  resumed activity now, with the screen state.
- Progress could go unsaved when something new started right after a stop: the previous session's follower was
  cancelled in the middle of its last write. It's now told to stop and finishes the write.
- Found by review before release: watched marks use Stremio's own episode order (stremio-core: specials last), and
  marks that would read differently in another order are left alone instead of rewritten; a start whose links
  fail leaves what's playing alone (its progress, binge mode and sleep timer used to stop); the end of an episode starts nothing when something else was started meanwhile, and a new command
  replaces a binge start in progress (a volume, binge, timer or auto-skip command no longer cancels a start); the
  previous title's track fixes stop when a new one opens; addon subtitles are asked for the stream actually
  chosen; library edits start from a fresh copy; the history file is changed under its lock; error tracebacks no
  longer carry addon links; a sleep timer turns the TV off even when stopping the player fails.
- An addon's stream lookup is tried once more a second later after a server error or rate limit (AIOStreams
  answered two lookups with an error once, then was fine).
- Rules that took too much: "start this episode over", "put the volume at like twenty five", a sentence that
  merely mentions an episode ("…once this episode is done") and described titles ("that film about the atomic
  bomb", "la de los atracadores") were played as titles; now the right rule or the models decide them. "A couple
  of minutes" / "un par de minutos" are read; more Spanish forms ("ponme los subtítulos…", "pon el que viene",
  "llévame a la mitad", "empieza el capítulo otra vez", "la cuarta temporada", "apaga la tele cuando acabe").
- A title spelled differently by speech-to-text but said the same finds your library's ("the bare" played "Jamie
  Oliver: The Naked Chef Bares All" instead of your The Bear).
- With d1 unreachable, an unmatched sentence went to a title search; the LLM decides it first when set up.
- "Skip" followed by anything but a duration ("skip purpose all") jumped forward 30 s; "sops off" turned the TV off
  after the episode (the LLM's guess; now the rules read it as subtitles off). A "put the bear show on" with "on"
  in the language field of the LLM's answer was dropped.
- `POST /v1/action` took no raw JSON body; "Finding SevenceE2…" lacked a space.
- `tv-agent eval` asks d1 where the decision steps do (`d1_where`), not always the local one. An empty clean-up
  regex is refused.
- The tv-agent service pulled llama-d1 in (`Wants=`), so each start or restart of the agent, and each boot,
  started the local d1 again after you'd stopped or disabled it; it's only ordered after it now. `tv-agent setup
  services --enable` enables llama-d1 only when the decision steps use the local d1. Rewrite the units once:
  `tv-agent setup services`.

### Added
- Hosted speech-to-text through OpenRouter (`stt_where` "hosted", or "hosted-first" with this laptop's model as
  backup; `stt_hosted_model`, Voxtral Mini Transcribe 3 by default; Deepgram Nova-3, AssemblyAI, ElevenLabs Scribe,
  GPT transcribe, MAI-Transcribe and others listed). It gets the recording when you pause (~0.4-1 s more than the
  local model). The local model loads only when it's needed and can be unloaded live (`stt_loaded` in
  `POST /v1/pipeline`).
- `keep_recordings`: each spoken command's audio kept (the last 300, mode 600) and linked from its history entry;
  `POST /v1/transcribe` transcribes one again, locally or with any hosted model.
- `stt_where = "local-first"`: this laptop's model, and the hosted one when it fails or hears nothing in half a
  second or more of audio.
- `GET /v1/tv/packages` (the TV app, Kodi, Just Player: installed, version, enabled; the app's APK SHA-256, to
  match a release's) and `GET /v1/about` (version, Python, start time, models in use), for the panel.
- History entries carry their timings and costs: speech-to-text (where, ms, cost), deciding (ms, the models'
  cost), time from your last word, audio length.
- The TV over the local API: `GET /v1/tv/status` (model, Android, the TV app's version, network and Wi-Fi signal,
  CPU, memory, busiest processes, app in front, the laptop's link and ping), `POST /v1/tv/speedtest`,
  `/v1/tv/diagnose` (each ADB step with what to do), `/v1/tv/reconnect`, `/v1/tv/key` (a fixed list of keys),
  `GET /v1/tv/screenshot`, `GET /v1/now-playing/link` (what the agent plays, local only, never logged) and
  `POST /v1/tv/play-link` (any http(s) video link, in Kodi or Just Player).
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
- The decision steps (fixed phrases, pattern rules, the LLM, d1) run in an order you set and each can be switched
  off (`decide_steps`, `decide_off`; the panel's Decisions tab changes them live, `GET/POST /v1/pipeline`). A step
  that can't be reached passes to the next; the LLM's "not a command" is final; d1 below its confidence passes
  on. When no model answers, the rules' offline intent applies (a title to play, or nothing).
- d1 can be the local one, a hosted decision model, or either first with the other as backup (`d1_where`). The
  hosted one (`hosted_d1_model`) is "d1" or "d1-ultra" through Liquid's API (`tv-agent setup liquid`), or any of
  OpenRouter's decision models by id (`liquid/d1`, `typesafe/jev-1.14`, `cloudflare/clef`…, with the OpenRouter
  key; they all take the same request).
- An LLM step through OpenRouter (`tv-agent setup openrouter`; `llm_model`, by default
  deepseek/deepseek-v4.1-flash-1017 with reasoning off, the most accurate and fastest of 9 models measured; see
  docs/research-2026-10-22.md §2.9). One action in a fixed JSON shape covering every command; it gets up to 10 of
  your library's titles that sound like what you said, knows that speech-to-text mishears command words, and names
  described titles itself ("the series with the chemistry teacher" is Breaking Bad); its answer is checked like
  the rules', and a title it names still has to be found. Temperature and reasoning adapt to what the model takes
  (a model that can't turn reasoning off uses its lowest). Fastest provider by default; `llm_private` keeps to
  zero-data-retention providers.
- Misheard titles: a name the search doesn't find exactly also goes to the LLM, for the real titles it sounds
  like, and the closest-sounding one that your library or Cinemeta has is played ("ceberance", "cebruns",
  "sevence": Severance; "suxession": Succession).
- Two commands in one: "play Severance episode 2 and go to the middle", "pon La casa de papel y salta la intro",
  "turn it down and put on The Bear" run one after the other (the second waits until playback starts; "skip the
  intro" then skips it when it comes). Split only when one side is a control command the rules know and the other
  is understood on its own, so "Pride and Prejudice", "treinta y cinco" and "an hour and a half" stay one command.
- "What am I watching?" (the title, how far in, when it ends) and "where am I in Severance?" / "what's the last
  episode of X I watched?" (your last episode and how far in, from your Stremio library), in Spanish too.
- "Subtitles" on its own switches them over (off if they're on), like the remote's button; "auto skip on/off" for
  the LLM; "volume to the max / the minimum".
- Mishearings of switches, from real use: "subtitles of" / "sops off" (off), "subtitles all" (switched over),
  "beach mode oh" / "pinch mode on" (binge mode on); "turn the subtitles on" and any "turn X on/off" in either word
  order.
- "What can you do?" / "help": every command on the TV, as patterns with ‹placeholders›, by section, in the
  language it was asked in (`tv_agent/commands.py`, also the panel's Commands tab). A test expands every pattern
  into the sentences it stands for, English and Spanish (130), and checks each gets its action from the rules
  alone.
- `scripts/bench_fallbacks.py`: accuracy, delay and cost of each decision path and of several LLMs side by side
  (`--models`, `--hosted d1,d1-ultra`, `--local`), on `eval/hard.jsonl` (80 hard commands, English and Spanish),
  `eval/hard_holdout.jsonl` (40 nothing was tuned on) and `eval/voice.jsonl` (33 real commands said to the TV).
- `max_mbps = 0`: no bitrate limit.
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

## panel 0.2.0 — 2026-10-22

Needs server 0.2.0 (its new endpoints: speech-to-text, recordings, the TV, About).

### Fixed
- Settings reloaded every time the tab was shown, throwing away edits not saved yet (switch tabs and they were
  gone); unsaved edits now stay until Save or Revert.
- Saving the rules in the Decisions tab trimmed the spaces of clean-up replacements ("pon " → "play " became
  "play"), so "pon the bear" read "playthe bear"; they're kept exactly as typed now. A rules.json saved that way
  needs Reset to defaults (or its replacements' trailing spaces put back).
- Saving Settings no longer drops what the replaced settings (`hosted_d1`, `llm_fallback`, `llm_first`) meant: their
  step order, steps off and d1 choice are written first.
- Overview: with the local d1 unused (its step off, or hosted only), "Start all" leaves llama-d1 stopped and voice
  control counts as on without it.

### Added
- Beside the tabs, on every tab: Save and Revert for whatever Settings and Decisions have unsaved (a ● on their
  tab says which), Run checks (tv-agent doctor and the rules as edited on the labelled sentences, in one report),
  Files (export / import everything, open the settings file, the rules or their folder, reset the rules) and ⓘ
  About. Every tab's messages show in the status bar at the bottom. Speech, model and step choices still apply at
  once and say so there.
- About (ⓘ, or the tray menu): each part's version in this copy, running or on the TV, and the latest release on
  GitHub; whether this copy's folders are identical to their release tags (git tree ids) and whether the TV app's
  APK is a release's (its SHA-256 against the release's digest) or a local build; the pinned downloads with
  their SHA-256; the models in use; author, GitHub link and licence.
- Decisions: the diagram shows the steps as they run now (fixed phrases, pattern rules, the LLM, the decision
  model between "Clean-up" and "Action"); drag a step (or ▲▼) into another order and tick it on or off, live at
  once. Choosing a local decision model while its server (llama-d1) is stopped offers to start it; hosted only, or
  the step off, offers to stop it. The editors follow a command's path: Speech to text, Clean-up, Fixed phrases,
  Pattern rules, LLM, Decision model, Apps & hooks. The tester explains two-command sentences, skipped and
  unreachable steps and where the decision model answered from; "How a pattern rule works…" opens beside the
  table's buttons. Drag the divider up to give the editors room (the diagram scrolls; the panel remembers it).
- Decisions › LLM and › d1 model: a typed model id is looked up in OpenRouter's catalog: whether it's there, and
  takes the strict JSON answers the LLM step needs (or is one of its decision models).
- Commands: everything you can say as patterns with ‹placeholders›, by section, in English or Spanish, with a
  filter; "try" fills one in with an example you can edit, then explains it (nothing sent) or does it on the TV.
- Settings covers every setting now: an API keys card (each key's state, and buttons to set the Liquid and
  OpenRouter keys: checked, then saved; the panel keeps nothing), "No limit" for the bitrate, binge mode and auto
  skip under Playback, and an Advanced section (addresses, ports, the Kodi user and password, pairing the TV app
  again). The tab reloads when shown, so a binge mode changed by voice isn't overwritten.
- Settings: under the player, whether Kodi or Just Player is installed on the TV (and its version, or disabled).
- Export all / Import all (Files): settings, your rules, taught sentences and the panel's preferences in one JSON
  file (no keys or passwords); importing checks it, then restarts the voice agent. Open the rules file on the
  built-in rules: read-only, or as your own rules.json.
- Decisions › Speech to text (click its box in the diagram): on this laptop, hosted, or either one with the other
  as backup; the local model (download, or another model's folder) and the hosted one by name; choosing hosted
  offers to unload the local model, choosing this laptop loads it; "Keep recordings" for debugging.
- Decisions › Decision model (was "d1 model"): where it runs (the same four choices, moved here from the diagram), a
  local model (any GGUF file) and a hosted one ("d1" recommended);
  the key each needs is shown with ✔ or ⚠ and a link to it in Settings (also in the LLM and Speech to text tabs);
  what "when no model answers" and "candidates for a title choice" do; the whole tab scrolls, with every intent
  shown. Apps & hooks scrolls the same way, with every app shown.
- Overview › TV: model, Android and uptime, screen off; Wi-Fi or Ethernet with signal and link speed, the laptop's
  link and ping, a speed test; TV CPU and memory with history, the busiest processes and the app in front; the TV
  app's version against this copy's, with Install / Update / Uninstall / Pair again; Diagnose (each ADB step with
  what to do) and Reconnect; shortcut keys, Kodi / Stremio, a screenshot; what the agent plays with its stream
  link (open or copy) and a field to send any video link to the TV.
- Activity: a Response times chart of the last 48 commands (speech-to-text, deciding, waiting for your pause;
  doing it, green or amber), with each one's numbers on hover, and typical / slowest-5% times by step, model and
  action, the cloud cost today, how many were understood. Commands with a recording (🔊) can be played or
  transcribed again with another model from the right-click menu.
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
