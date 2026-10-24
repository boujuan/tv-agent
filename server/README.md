# tv-agent server

The daemon behind tv-agent: speech-to-text, the decision rules and d1-3B, TV control over ADB, Stremio → player
playback and progress sync. Also the `tv-agent` command line and the MCP server. Part of
[tv-agent](../README.md); the desktop panel and the TV app talk to it over its HTTP API.

## What you can say

| Kind | Examples | What happens |
|---|---|---|
| Films and series | "play The Bear season 3 episode 2", "put on Dune Part Two", "play Severance from the start", "play la casa de papel", "play el hoyo" | Your Stremio library first, then Cinemeta and your metadata addons side by side (AIOMetadata knows Spanish names: *el hoyo* is The Platform); Cinemeta's popularity, then d1, settle close calls. Plays from your addons; resumes where you stopped unless you say "from the start" |
| Continue / next | "continue The Bear", "keep watching", "carry on with the show", "next episode", "previous episode" | Stremio's Continue Watching; past 90% it goes on to the next episode. With the player open, "continue" just unpauses |
| Same show, other episode | "go to season 4 episode 2", "play episode five", "season two" | No title: the series that's on (the one the agent started, else your latest in Continue Watching) |
| Stremio page | "show me The Office in Stremio" | Opens the title (or episode) in Stremio without playing |
| YouTube, Twitch | "lofi hip hop on YouTube", "search YouTube for paella", "Ibai on Twitch" | Top YouTube result in TizenTube; Twitch live stream |
| Apps | "open Prime Video", "go back to YouTube", "open Movistar", "go home" | |
| Playback | "pause", "resume", "skip forward thirty seconds", "go back ten seconds", "go to twelve minutes", "go to the middle", "back to the beginning", "stop" | Kodi: exact, over its API. Just Player: media keys for skips, reopening the link at the time for jumps. Other apps: media keys only |
| Volume | "volume twenty", "volume to the max", "louder", "turn it down a bit", "mute", "unmute" | TV volume 0-100 |
| Binge mode | "binge mode on", "turn off binge mode" | On (default): when an episode ends the next one starts, already found and checked 20 s into the episode. After a film or a season's last episode: Home |
| Sleep timer | "stop after this episode", "turn the TV off after this one", "stop in 30 minutes", "turn off in an hour", "cancel the sleep timer" | Stops (then Home) or puts the TV to sleep; "after this one" wins over binge mode |
| Subtitles, audio | "subtitles on", "subtitles off", "subtitles" (switches them over), "Spanish subtitles", "original audio", "audio in English" | Kodi: the file's own track, else your subtitle addons for that release |
| Missed a line | "what did they say?", "say that again" | Back 10 s, subtitles on for a moment |
| Intro, recap, credits | "skip the intro", "skip the recap", "skip the credits"; "auto skip on" / "off" | Jumps to the end of the part you're in, from TheIntroDB / IntroDB timestamps (or a chapter named "Opening"); credits: the next episode in binge mode. Auto skip (off by default, `auto_skip`) skips intros and recaps by themselves when their timestamps are sure |
| Lists | "what was I watching?", "what's new?", "new films", then "number 2" / "the second one" | A numbered list on the TV (posters); the remote's OK or number keys pick too |
| A moment | "go to the part where they kiss", "ve a la parte en la que le disparan" | Found in the subtitles of what's playing (the LLM reads them with their times); the player jumps there |
| Suggestions | "recommend something like Dark", "what should we watch?", "put on a comedy under 90 minutes", "recomiéndame una serie de miedo", "algo como La casa de papel" | The LLM suggests titles that fit, with what you watched lately as taste; each is checked by the title search (and its length, when you gave a limit), and the ones you've seen are left out; a numbered list on the TV, "number 2" plays it |
| What's on | "what am I watching?", "where am I in Severance?" | The title, how far in and when it ends; your last episode of a title and how far in |
| Two at once | "play Severance episode 2 and go to the middle", "turn it down and put on The Bear" | One after the other; the second waits until playback starts |
| Help | "what can you do?", "help" | Every command as a pattern with its ‹placeholders›, by section, on the TV in the language you asked in (also the panel's Commands tab) |
| Newest episode | "play the latest episode of The Bear" | The newest episode out |
| Your library | "add this to my library", "remove Friends from my library", "mark this as watched", "mark The Bear season 3 as watched", "mark it as unwatched" | Written to your Stremio library, with Stremio's own per-episode watched marks |

Audio and subtitles: a title made in a country that speaks `native_language` plays in it without subtitles;
anything else plays in `audio_language` (when the file has it) with the first available of `subtitle_languages`
(English, Spanish, Chinese Simplified/Traditional and others). Dubbed-only releases (French-only, Latino-only) of
titles from elsewhere are marked down. Kodi applies this per file; Just Player only partly (see below).

Skipping asks TheIntroDB and IntroDB with the episode's IMDb id and the file's length, and only jumps when the part
you asked about contains where you are: timestamps made on another cut of the episode can't send you somewhere
wrong. Skipping by itself needs timestamps made on a file within 2 s of yours' length, from 3 or more people (or
both databases agreeing). Rules and coverage: [research §2.6](../docs/research-2026-10-22.md).

In Spanish too, with the multilingual speech model (`tv-agent setup stt parakeet-v3`): "pausa", "siguiente
capítulo", "pon la casa de papel temporada dos capítulo tres", "sube el volumen", "¿qué hay de nuevo?", "salta la
intro", "eso está mal". A clean-up layer turns the Spanish into the English the rules know; titles stay as said.

Anything it doesn't understand shows "Didn't get that: …" and does nothing. While the pill listens, the volume
is halved (Kodi's own volume when Kodi plays, else the TV's) and comes back afterwards; nothing is paused.

## Player: TV Agent Player, Kodi or Just Player

`player = "tvplayer"`, `"kodi"` (the default) or `"just"` in `~/.config/tv-agent/config.toml` (or the panel's
Settings). Whichever it is, it's brought up while the stream is still being looked up.

**TV Agent Player** ([boujuan/tv-agent-player](https://github.com/boujuan/tv-agent-player)) is a Media3 player
built for this TV and this agent: an app of its own on the TV that connects to the agent (`player_port`, 8770, TLS
with the phones' certificate, which it pins; its own `player_token`) and is driven over that connection. It's told
the agent's addresses in order: the LAN's, any `phone_hosts`, then this computer's Tailscale address while Tailscale
runs, so it still finds the agent when the computer is away from the TV's network (the TV on Tailscale too). Install it
with `tv-agent tv install tv-agent-player-<version>.apk` (it's also compiled ahead of time and paired); `tv-agent tv
pair` pairs it again. Measured on the TV (its DESIGN.md): the first frame 1.3 s after the command (Just Player 2.9 s,
Kodi 2.4 s warm), the next episode in binge mode 0.1 s (opened in the background 2.5 minutes before the end), jumps
of ±10-30 s 0.1 s from what's in memory, 105-204 MB while playing a WEB-DL (Kodi 277 MB) and nothing resident between
plays.

| | TV Agent Player | Kodi |
|---|---|---|
| A new title or episode | Loads in place in the open player: no relaunch, no home screen in between | Opened over the playing one, after its decoder is let go (2 s) |
| While it starts, and paused | A loading screen with the title, its artwork, what you said and the step it's at; on pause, the plot, the stream's source, debrid service, size, release and badges (player 0.2) | Kodi's own screens |
| Jumps, skips, intros | Exact, in place, also in Dolby Vision; ±N s at the nearest keyframe, fast commands added up | Exact, but a Dolby Vision file is reopened at the time (its decoder's flush fails on this TV) |
| Audio and subtitles | Your rule applied in the player's own first choice, before the first frame: commentary never, Simplified or Traditional Chinese by name, Castilian first, the file's main track; with subtitles off, a forced track in the audio's language still shows. Add-on subtitles drawn by the player, no rebuffering | Your rule set after the start, rechecked 8 s later; add-on subtitles loaded by Kodi |
| What it tells the agent | State, position every second, tracks, decoder and HDR, sound output, errors, the exact position it closed at: nothing is polled | Polled over its API |
| While you speak | Its own volume lowered: no volume bar, no key presses | Kodi's volume |
| A file that fails to start | The next copy from your addons takes over where it was | Kodi restarted if it's stuck |

| | Just Player 0.217 (`com.brouken.player`) | Kodi 21.2 |
|---|---|---|
| Start to moving picture (measured, same links) | 6.1 s cold / 6.1 s warm (4K DV/HDR); 6.4 / 6.1 s (1080p remux) | 7.1 / 5.1 s; 7.7 / 6.6 s |
| On screen | Just the video and a minimal control bar | Kodi's interface; you end up in its menus |
| Pause, resume, stop | Media keys | API |
| Skips | Media keys: +15 s forward, -5 s back per press | Exact, API |
| Jump to a time, "the middle" | Reopens the same link at that time (~6 s of buffering) | Exact, API |
| Audio language | The TV's language when the file has it; a native-language title with an English dub plays in English | The rule, per file |
| Subtitles | Android's system captions setting, set by the agent before each start and turned off again after; only the file's own | The rule, per file: the file's own, and for languages it lacks, the best match from your subtitle addons (SubSense, OpenSubtitles…) for that exact release |
| Progress to Stremio | From Android's media session; real length read with `ffprobe` (~2 s) | From the API |
| Extra addon headers | Not supported: those streams are skipped | Supported |

Also measured and set aside: Stremio's own player accepts an agent-chosen stream
(`stremio:///player/<stream>/<addon>/<cinemeta>/<type>/<id>/<video>`) and keeps Continue Watching itself, but took
15-20 s to start cold and only takes media keys.

## Voice: decided while you pause

The TV app streams the remote's microphone in 20 ms chunks from the moment the pill opens (`stream_port`, 8767,
on the same address and token as the TV's HTTP endpoint). tv-agent tracks speech and quiet as it arrives and,
in the quiet after you speak, transcribes and decides: at ~160 ms of quiet for a complete command that can't be
the start of a longer one ("pause", "next episode", "go home", "… season 3 episode 2"), at ~460 ms for other
understood commands ("volume twenty" could still become "twenty five"), at ~720 ms otherwise (a title alone, since
a season often follows). If you go on talking, the early result is dropped. Measured with the remote on
2026-10-22: 240-310 ms from your last word to the decision for complete commands (it was ~0.9 s). The pill shows
what it has understood so far. Pressing the mic again sends at once; without the stream the app falls back to
recording until 0.7 s of quiet and sending it all. Logic: `listen_stream.py`; protocol: `server.py`.

Speech-to-text can also be hosted (`stt_where = "hosted"`, or `"hosted-first"` with this laptop's model as backup,
or `"local-first"`, hosted only when this laptop's model fails, hears nothing or writes in another script): an
OpenRouter transcription model (`stt_hosted_model`) gets the recording at the same checkpoints, with a language hint
(`stt_language`, your native language by default) and, for AssemblyAI, a vocabulary of names to hear as written
(`stt_vocabulary`, plus your library's titles and Twitch favourites with `stt_vocabulary_library`; the first 100 go,
most likely first: what's playing and Continue Watching, yours, what you watched, Twitch, the rest of the library;
the titles as Stremio names them with a Spanish hint, Cinemeta's original names with an English one, both without a
hint). Parakeet guesses
the language of every command and can't be limited: on 30 real commands from a Spanish speaker it wrote 11 wrong
(Russian or French spellings of short Spanish commands, "Simpsons" as "sin"), where AssemblyAI Universal-3.6 Pro
with a Spanish hint and the vocabulary wrote none wrong, ~0.26 s and ~$0.0002 a command (docs/research §2.12). The
local model then isn't loaded (~1 GB less memory). `keep_recordings = true` keeps each spoken command's audio (the
last 300, in `~/.local/share/tv-agent/recordings/`, mode 600) for the panel's Activity tab to play or transcribe
again.

## How it decides

All of this is data in `~/.config/tv-agent/rules.json` (or `src/tv_agent/decide/default_rules.json` until you
save your own), editable in the panel's **Decisions** tab and used at once, without a restart:

1. **Clean-up:** fillers at the start ("hey tv", "um", "okay", "can you"), words removed anywhere ("please"),
   politeness at the end ("thanks"), and fixes for common mishearings ("got to" → "go to", "un pause" → "unpause",
   "subtitles of" → "subtitles off", "beach mode" → "binge mode"), including a layer that turns Spanish commands
   into the English the rules know. Always on.
2. Then the **decision steps**, in the order set in the Decisions tab (this is the default), each switchable:
   - **Fixed phrases** (instant): an exact sentence → its action ("pause", "next episode"), and the sentences you
     taught in the Activity tab.
   - **Pattern rules** (instant), tried in order: a regular expression with named parts → an action template
     whose fields are filled by converters (`seconds`, `time`, `number`, `level`, `title`, `play`, `app`…). A
     converter marked `!` makes the rule not match when its part can't be converted, so the next rule gets a try.
     The last ones catch "play / put on / watch / start + a title" and a title said with a season or episode, so
     the commonest commands never wait for a model; a title described instead of named is left to the models.
   - **The LLM** (with an OpenRouter key, ~0.4 s): one action in a fixed JSON shape, checked like the rules'; its
     "not a command" is final. It also names the real title behind a misheard one the search can't find.
   - **d1** (Liquid AI's decision model: this laptop's d1-3B, Liquid's hosted one, or either first) chooses among
     the intents by their descriptions; unless it's 0.65 sure and its answer is twice as likely as the next, the
     next step decides. d1 sees only the descriptions, so each says what it does with an example. Built-in intents
     pull titles and numbers out with code; your own intents do a fixed action or fill one from the whole
     sentence. d1 also picks between candidate titles when a name is ambiguous.

   A step that can't be reached passes to the next. When no model answered (off or unreachable), what's left is
   treated as the d1 tab's "when no model answers" intent (a title to play, or nothing).
3. **Two commands in one** ("play Severance episode 2 and go to the middle"): when one side of an "and" / "y" is a
   control command the rules know, the other side goes through the steps and both run, one after the other.
4. **Apps and hooks:** the only apps the agent may start, the names you can call them, and which app gets YouTube,
   Twitch and Stremio-page links (and the link format).

Saving checks everything first (regexes compile, actions and fields exist, hooks point at real apps) and keeps the
current rules if anything's wrong; a hand-edited `rules.json` is picked up on the next command the same way.
A `rules.json` saved before rules version 2 keeps working (its broad `transport` and `volume` intents are still
understood) but doesn't get the new phrases, patterns and intents: in the panel, **Reset to defaults**, then redo
your own changes.
Every decision records the path it took, which the panel draws.

Stream choice (`stremio/streams.py`): only direct debrid links, cached, ≤ `max_height`, ≤ `max_mbps`, no cam rips.
Of those: the file has the audio the title should play in (English, or Spanish for Spanish-made titles), then the
highest resolution, then AIOStreams' own score for the release (its ★: release-group tier, source, HDR, audio, your
ranked expressions), then already in your debrid library, then the higher bitrate. AIOStreams sends those facts as
structured `streamData` to an AIOStreams-compatible client, which the agent identifies as; other addons are read
from their text. A one-byte request checks the link, following redirects by hand so that AIOStreams' "can't play
this" videos (uncached after all, failed, rate-limited) count as failures.

The model never gets a shell. Actions are a closed, validated set (`actions.py`); every TV command goes through
`tv/adb.py`, which quotes each argument; stream URLs reach Kodi as JSON, never through a shell; link values are
URL-encoded into the hook templates.

## Measured (2026-10-22, TCL 65Q6C + Ryzen 7 8845HS / Radeon 780M)

| Stage | Result |
|---|---|
| Speech-to-text | 103 ms p50, 193 ms p95 in real use (5 s test clip: 211 ms) |
| Decision | fixed phrase / pattern under 0.1 ms; the LLM ~0.4 s; d1 ~0.9 s local, ~0.2 s hosted. Rules decide ~96% of commands. Test sets: 107/107 (`eval/utterances.jsonl`), 654/654 full actions (`eval/commands.jsonl`: 476 for tuning, 178 held out), and every pattern of the command list (130 sentences, English and Spanish) |
| Transport, volume, apps | ~0.1-0.8 s after you stop talking |
| Film or episode | ~6 s from your words to moving picture in either player (addon lookup + link check + opening the 4K link dominate) |
| Cost on this computer | tv-agent ~1.1 GB RAM (mostly the speech model), 0% CPU idle; d1-3B 1.7 GB of GPU memory |

How the mic path and the models were chosen: [Phase 0](../docs/phase0/README.md).

## Setup

```bash
scripts/fetch-models.sh                     # llama.cpp b11804 (Vulkan) + d1-3B + Parakeet, SHA-256 checked (~2.2 GB)
uv sync --extra mcp --group dev             # Python 3.13 environment in .venv
.venv/bin/tv-agent setup token              # the token the TV app sends
.venv/bin/tv-agent setup stremio            # prints a link; approve it where you're signed in to Stremio
# On the TV: Kodi > Settings > Services > Control > Allow remote control via HTTP (8080, user kodi, a password)
.venv/bin/tv-agent setup kodi               # asks for that password, checks it with Kodi, saves it
.venv/bin/tv-agent setup services --enable  # writes ~/.config/systemd/user/{llama-d1,tv-agent}.service for this copy
../android/build-and-install.sh             # the TV app: install, microphone permission, pairing
.venv/bin/tv-agent doctor                   # all "ok"
```

Optional:

```bash
.venv/bin/tv-agent setup stt                # the speech models; `setup stt parakeet-v3` downloads (487 MB) and uses
                                            # the multilingual one (Spanish commands), checked against its SHA-256
op read "op://Personal/<item>/credential" | .venv/bin/tv-agent setup liquid      # Liquid's hosted d1
op read "op://Personal/<item>/credential" | .venv/bin/tv-agent setup openrouter  # the LLM step
```

The keys are checked with the service, never shown, and kept in `secrets.toml` (the panel's Settings can set them
too). How a command is decided is set in the panel's Decisions tab, live (or `decide_steps`, `decide_off`,
`d1_where` in config.toml): after the clean-up, fixed phrases, pattern rules, the LLM and d1 run in that order by
default, each can be moved or switched off, and d1 can be the local one, Liquid's hosted one, or either first. A
step that can't be reached passes to the next. With an OpenRouter key the LLM (`llm_model`, DeepSeek V4.1 Flash
with reasoning off) decides what the rules don't, before d1: on fresh hard commands the whole path was right 95-100%
of the time in English and Spanish, at ~0.4 s, against 65-80% with d1 ([research §2.9](../docs/research-2026-10-22.md));
it also says what a title the search can't find was misheard from. What goes to them: the LLM gets what you said,
what's playing, the app names and up to 10 of your library's titles (fastest provider; `llm_private` for
zero-data-retention ones only); Liquid gets what you said and the options (its terms allow training on it). With
both cloud steps on, the local d1 server (llama-d1) can be stopped: the Decisions tab offers it.

`config.toml` needs at least `tv_hosts` (the TV's address, e.g. Ethernet then Wi-Fi; mDNS is tried otherwise).
The agent follows this computer's address on the TV's network while it runs: when it changes (Ethernet unplugged,
Wi-Fi taking over, a new DHCP lease) its listeners move there within about 5 seconds and the TV app and TV Agent
Player are paired again over ADB (the TV shows it; a TV in standby is paired once it answers). `lan_host`,
`tv_hosts` and `phone_hosts` edited in `config.toml` are used the same way, without a restart. A phone paired with
the old address needs the new one in the phone app (Agents › Addresses…), or a `phone_hosts` address that doesn't
change, such as Tailscale's.
Every setting: [config.example.toml](config.example.toml). Secrets: `~/.config/tv-agent/secrets.toml` (mode 600;
the daemon refuses to start otherwise). For services across logouts: `loginctl enable-linger`.

## Everyday

| I want to… | Do this |
|---|---|
| See what it heard and did | `journalctl --user -u tv-agent -f`, or the panel's Activity tab |
| Try a sentence without the remote | `tv-agent say "play the bear season 3 episode 2"` (`--dry-run` only prints what it would do) |
| Run one action | `tv-agent do '{"type":"volume","op":"set","amount":15}'` |
| See the streams it would choose from, best first | `tv-agent streams "the bear" --season 3 --episode 2` (resolution, quality, HDR, languages, bitrate, cached; never links) |
| Check after changing the rules | `tv-agent eval`, `tv-agent eval --file eval/commands.jsonl --split test`, `uv run pytest` |
| Run a command on the TV | `tv-agent tv shell dumpsys media_session` |
| Re-pair the TV app (new token; a new address is paired by itself) | `tv-agent tv pair` |
| Let Claude Code drive the TV | `claude mcp add --scope user tv-agent -- $PWD/.venv/bin/tv-agent-mcp` (12 tools, no shell) |
| Use it from my phone | The [TV Agent phone app](https://github.com/boujuan/tv-agent-phone); pair it in the panel's Settings › Phones |
| Stop it for a while | `systemctl --user stop tv-agent llama-d1` (the mic button then says "Agent offline") |
| Fix a command it got wrong | Say "that's wrong" right after; then in the panel's Activity tab right-click it, "Teach the right action…" (or `tv-agent review` for the list, with an LLM's guess) |

## HTTP API

LAN, on this computer's address on the TV's network (port 8765), for the TV app only: requests must come from the
TV's address and carry the token.

| | |
|---|---|
| `POST /v1/listen/start` | the mic pill opened (warms the TV connection) |
| `POST /v1/utterance` | 16 kHz mono PCM16 → transcript, decision, reply; the action runs after |
| `GET /v1/health` | |

Localhost (127.0.0.1:8766), for the panel, the CLI and the MCP server, same token:

| | |
|---|---|
| `POST /v1/text` `{"text"}` | decide and do it |
| `POST /v1/audio` | PCM16 from the computer's microphone |
| `POST /v1/decide` `{"text"}` | decide only, with the path it took |
| `POST /v1/action` `{action}` | run one typed action |
| `GET /v1/state`, `GET /v1/stats` | what's playing; latency per stage |
| `GET/POST /v1/rules`, `POST /v1/rules/reset`, `/reload` | the decision rules (checked, saved, live) |
| `POST /v1/eval` `[{"rules"}]` | score the labelled sentences, optionally with unsaved rules |
| `GET/POST /v1/pipeline` | decision steps, models, speech-to-text (where, models, loaded, keep recordings, language hint, vocabulary; `stt_vocabulary_sent`: every name sent, in the order sent), live |
| `POST /v1/transcribe` `{"recording", "where", "model"}` | transcribe a kept recording again |
| `GET /v1/tv/status`; `POST /v1/tv/speedtest`, `/diagnose`, `/reconnect` | the TV, its network and the ADB link |
| `POST /v1/tv/key` `{"key"}`; `GET /v1/tv/screenshot` | a key from a fixed list; the screen as PNG |
| `GET /v1/now-playing/link`; `POST /v1/tv/play-link` `{"url"}` | the stream the agent plays; play any http(s) link |
| `GET /v1/tv/packages` | the TV app, TV Agent Player, Kodi and Just Player on the TV: installed, version, enabled; the app's APK SHA-256 |
| `GET /v1/about` | this agent's version, Python, start time and the models it uses |
| `GET /v1/now-playing` | the title, episode (name, poster), position, Kodi's audio and subtitle tracks, intro/recap/credits |
| `POST /v1/now-playing/track` `{"audio": i}` / `{"subtitle": i \| null}` | one of Kodi's tracks by its index |
| `POST /v1/remote/key` `{"key", "long"}`; `POST /v1/remote/text` `{"text"}` | a key (d-pad, OK, numbers…, `remote.KEYS`); typing into the TV's focused field (ASCII) |
| `GET /v1/tv/volume`; `GET /v1/apps` | the TV's volume; the apps the agent may open |
| `GET /v1/lists?list=recent\|new\|movies`; `POST /v1/lists/pick` `{"list", "imdb", "video"}` | the numbered lists voice shows on the TV, returned instead; play one (it's also what "number 2" picks from) |
| `GET /v1/activity?limit=&before=`; `POST /v1/activity/mark` `{"t", "wrong", "heard"}`; `GET /v1/recording?name=` | the history, newest first, a page at a time; mark one wrong; a kept recording (WAV) |
| `GET/POST /v1/settings` | the Settings tab's playback and language settings (`remote.SETTINGS`), checked, saved and live |
| `GET /v1/updates`; `POST /v1/updates/check`; `POST /v1/updates/tv`, `/tvplayer` | the latest GitHub release of the agent, panel, TV app, TV Agent Player and phone app against what runs (`updates.py`; checked every 6 h unless `updates = "off"`); install the TV app's or the player's release over ADB (localhost only; only with GitHub's SHA-256 for it; the player not while it plays) |
| `GET /v1/clients`; `POST /v1/clients/pairing`, `GET` it, `/pairing/cancel`; `POST /v1/clients/revoke\|rename` `{"id"}` | paired phones (localhost only): list, pair (a one-time code and the QR code's link), revoke |

Phones, paired from the panel (Settings › Phones), on this computer's LAN address (and `phone_hosts`, e.g. a
Tailscale address), for the [TV Agent phone app](https://github.com/boujuan/tv-agent-phone):

| | |
|---|---|
| `POST /v1/pair` `{"code", "name"}` (port 8768, HTTPS) | the panel's one-time code → this phone's own token, the agent's name and version, the stream port |
| the endpoints above, with that token (port 8768, HTTPS) | all but the stream link, the rules editor (`POST /v1/rules`, reset, reload, eval) and the client list; `GET /v1/clients/me` and `POST /v1/clients/me/forget` instead, and `GET /v1/updates` but not `POST /v1/updates/tv`. A phone's `X-App-Version` header (or `app_version` in its stream's first line) is kept as its app version for the panel |
| port 8769, TLS | the TV app's audio stream protocol (`server.py`), with the phone's token in the first line |

## Security

- **LAN listener** on one address only (`lan_host`, or the one the route to the TV uses, followed while the
  agent runs; never `0.0.0.0`).
  Requests must come from the TV's address **and** carry the token (constant-time compare); bodies over 400 KB are
  refused; one utterance at a time. The local API (127.0.0.1) needs the token too.
- **The token** reaches the TV app by pairing (`tv-agent tv pair`, an ADB broadcast only the shell may send); it
  is never built into the APK. Rotate: `tv-agent setup token --rotate`, then `tv-agent tv pair`.
- **Phones** (`clients.py`) never get the agent token: each pairs with a one-time code from the panel (8
  characters, 40 bits, 5 minutes, one use, closed after 5 wrong tries; `/v1/pair` answers 10 tries a minute) and
  gets its own token, of which only the SHA-256 is kept (`clients.json`, mode 600), checked in constant time,
  revoked at once from the panel. Their listeners speak TLS with this computer's own certificate
  (`phone-tls.crt`/`.key`, EC P-256, made on first start, key mode 600), whose SHA-256 the QR code carries and the
  app pins: it never sends its token to anything else on a network (another Wi-Fi with the same addresses). Typed
  in instead, the app shows the fingerprint to compare with the panel's. A phone reaches only the listed endpoints
  (`clients.PHONE_ROUTES`), with bodies up to 64 KB (audio 400 KB); never the stream link, the rules editor, the
  list of phones or any secret; its settings are the panel's playback and language ones, not addresses or ports.
  Its tokens open nothing on the TV's listener or the local API. `phones = false` turns the listeners off.
  New certificate: delete both files, restart, pair the phones again.
- **Stremio auth key**: full access to your Stremio account, obtained with Stremio's own link login (no password
  typed anywhere). Your addons' URLs carry your debrid key and stay in memory only. Logs keep only hostnames of any
  URL and mask known secrets (`redact.py`).
- **Hosted models** (when you use them) get what you said (the LLM, decision models) or its recording (hosted
  speech-to-text): OpenRouter and Liquid may keep it. `GET /v1/now-playing/link` returns the debrid link, on the
  local API only, and is never logged.
- **Kodi** web control is plain HTTP on the LAN with a password, like ADB.
- **TV Agent Player** gets the debrid link and its headers only inside its TLS connection to the agent (never on an
  ADB command line, an intent or in logcat); it only talks to the certificate it was paired with, and the agent only
  accepts it from the TV's address with its own token (`player_token` in `secrets.toml`, sent at pairing over ADB).
- **Just Player** gets the debrid link on its `am start` command line (over ADB), and `ffprobe` gets it as an
  argument on this computer.
- **ADB** must stay on for this (USB debugging, port 5555).

## Undo

```bash
systemctl --user disable --now tv-agent llama-d1 && rm ~/.config/systemd/user/{tv-agent,llama-d1}.service
tv-agent logout                                              # invalidates the Stremio auth key
tv-agent tv shell pm uninstall com.boujuan.tvagent
tv-agent tv shell pm uninstall com.boujuan.tvagent.player                 # TV Agent Player, if installed
tv-agent tv shell settings put secure accessibility_captioning_enabled 0   # if a Just Player session was cut off
# Remove "84:com.boujuan.tvagent" from your launcher's key mapping
# On the TV: Kodi > Settings > Services > Control > HTTP off; Settings > Privacy > Microphone off (if wanted)
rm -rf ~/.config/tv-agent ~/.local/share/tv-agent ~/.local/opt/llama.cpp ~/.local/opt/llama.cpp-dl
```

## Files

| Path | What it is |
|---|---|
| `src/tv_agent/server.py` | The daemon: LAN listener for the TV app, localhost API, background execution |
| `src/tv_agent/clients.py`, `remote.py` | Paired phones (pairing, tokens, their TLS listeners); the API the phone app adds (now playing, remote, lists, activity, settings) |
| `src/tv_agent/cli.py`, `install.py` | The `tv-agent` command; user services, TV app install and pairing |
| `src/tv_agent/stt.py` | Speech-to-text (Parakeet via sherpa-onnx, or hosted through OpenRouter); recordings |
| `src/tv_agent/tv/status.py` | The TV's status, network, speed test and ADB diagnosis for the panel |
| `src/tv_agent/decide/` | `router.py` (the decision path), `rules.py` + `default_rules.json` (the rules engine and defaults), `text.py`, `numbers.py`, `d1.py`, `evaluate.py` |
| `src/tv_agent/actions.py`, `agent.py` | The closed action set and what each one does on the TV |
| `src/tv_agent/tv/` | `adb.py` (the only shell path), `apps.py`, `media.py`, `kodi.py`, `justplayer.py` |
| `src/tv_agent/stremio/` | `account.py` (API, link login), `cinemeta.py`, `streams.py`, `subtitles.py`, `library.py`, `progress.py` |
| `src/tv_agent/languages.py` | Audio/subtitle rule and track matching |
| `src/tv_agent/config.py`, `redact.py`, `state.py`, `activity.py` | Settings and secrets, log redaction, memory between commands, the history file |
| `src/tv_agent/mcp_server.py` | MCP tools for Claude Code (`tv-agent-mcp`) |
| `systemd/` | User services for a packaged install (`tv-agent setup services` writes them for a checkout) |
| `scripts/` | `fetch-models.sh`, Phase 0 benchmarks |
| `eval/` | `utterances.jsonl` (107 labelled sentences, the panel's tests), `commands.jsonl` (654, made by `build_commands.py`: every action, titles made of command words, numbers in every form, fillers, mishearings, non-commands) |
| `tests/` | Offline unit tests |
