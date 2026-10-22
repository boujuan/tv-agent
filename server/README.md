# tv-agent server

The daemon behind tv-agent: speech-to-text, the decision rules and d1-3B, TV control over ADB, Stremio → player
playback and progress sync. Also the `tv-agent` command line and the MCP server. Part of
[tv-agent](../README.md); the desktop panel and the TV app talk to it over its HTTP API.

## What you can say

| Kind | Examples | What happens |
|---|---|---|
| Films and series | "play The Bear season 3 episode 2", "put on Dune Part Two", "play Severance from the start", "play la casa de papel" | Your Stremio library first, then Cinemeta (d1 picks between close candidates: *la casa de papel* is Money Heist). Plays from your addons; resumes where you stopped unless you say "from the start" |
| Continue / next | "continue The Bear", "keep watching", "carry on with the show", "next episode", "previous episode" | Stremio's Continue Watching; past 90% it goes on to the next episode. With the player open, "continue" just unpauses |
| Same show, other episode | "go to season 4 episode 2", "play episode five", "season two" | No title: the series that's on (the one the agent started, else your latest in Continue Watching) |
| Stremio page | "show me The Office in Stremio" | Opens the title (or episode) in Stremio without playing |
| YouTube, Twitch | "lofi hip hop on YouTube", "search YouTube for paella", "Ibai on Twitch" | Top YouTube result in TizenTube; Twitch live stream |
| Apps | "open Prime Video", "go back to YouTube", "open Movistar", "go home" | |
| Playback | "pause", "resume", "skip forward thirty seconds", "go back ten seconds", "go to twelve minutes", "go to the middle", "back to the beginning", "stop" | Kodi: exact, over its API. Just Player: media keys for skips, reopening the link at the time for jumps. Other apps: media keys only |
| Volume | "volume twenty", "louder", "turn it down a bit", "mute", "unmute" | TV volume 0-100 |

Audio and subtitles: a title made in a country that speaks `native_language` plays in it without subtitles;
anything else plays in `audio_language` (when the file has it) with the first available of `subtitle_languages`
(English, Spanish, Chinese Simplified/Traditional and others). Dubbed-only releases (French-only, Latino-only) of
titles from elsewhere are marked down. Kodi applies this per file; Just Player only partly (see below).

Anything it doesn't understand shows "Didn't get that: …" and does nothing.

## Player: Kodi (default) or Just Player

`player = "kodi"` or `player = "just"` in `~/.config/tv-agent/config.toml` (or the panel's Settings). Kodi is the
default: it's the only player the laptop can fully drive, including subtitles from your Stremio subtitle addons
(why: [research of 2026-10-22](../docs/research-2026-10-22.md)). When Kodi isn't running, it's started while the
stream is still being looked up.

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

## How it decides

All of this is data in `~/.config/tv-agent/rules.json` (or `src/tv_agent/decide/default_rules.json` until you
save your own), editable in the panel's **Decisions** tab and used at once, without a restart:

1. **Clean-up:** fillers at the start ("hey tv", "um", "okay", "can you"), words removed anywhere ("please"),
   politeness at the end ("thanks"), and fixes for common mishearings ("got to" → "go to", "un pause" → "unpause").
2. **Fixed phrases** (instant): an exact sentence → its action ("pause", "next episode").
3. **Pattern rules** (instant), tried in order: a regular expression with named parts → an action template whose
   fields are filled by converters (`seconds`, `time`, `number`, `amount`, `title`, `play`, `episode_here`, `app`…).
   A converter marked `!` makes the rule not match when its part can't be converted, so the next rule gets a try.
   The last two catch "play / put on / watch / start + a title" and a title said with a season or episode, so the
   commonest commands never wait for the model, and titles made of command words ("Up", "Stop Making Sense") play.
4. **d1-3B** (Liquid AI's decision model, ~0.65 s) chooses among the intents by their descriptions; unless it's
   0.65 sure and its answer is twice as likely as the next, the answer is "Didn't get that". d1 sees only the descriptions, never the intent
   names, so each one says what it does with examples; there is one intent per action (pause, resume, seek to,
   previous episode, set volume…), so nothing has to be squeezed into a near miss. Built-in intents pull titles and
   numbers out with code; your own intents do a fixed action or fill one from the whole sentence. d1 also picks
   between candidate titles when a name is ambiguous.
5. **Apps and hooks:** the only apps the agent may start, the names you can call them, and which app gets YouTube,
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
| Decision | fixed phrase / pattern under 0.1 ms; d1 ~0.65 s (22 described intents, ~470 prompt tokens). Rules decide ~96% of commands. Test sets: 107/107 (`eval/utterances.jsonl`) and 654/654 full actions (`eval/commands.jsonl`: 476 for tuning, 178 held out) |
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

`config.toml` needs at least `tv_hosts` (the TV's address, e.g. Ethernet then Wi-Fi; mDNS is tried otherwise).
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
| Re-pair the TV app (new token or address) | `tv-agent tv pair` |
| Let Claude Code drive the TV | `claude mcp add --scope user tv-agent -- $PWD/.venv/bin/tv-agent-mcp` (12 tools, no shell) |
| Stop it for a while | `systemctl --user stop tv-agent llama-d1` (the mic button then says "Agent offline") |

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

## Security

- **LAN listener** on one address only (`lan_host`, or the one the route to the TV uses; never `0.0.0.0`).
  Requests must come from the TV's address **and** carry the token (constant-time compare); bodies over 400 KB are
  refused; one utterance at a time. The local API (127.0.0.1) needs the token too.
- **The token** reaches the TV app by pairing (`tv-agent tv pair`, an ADB broadcast only the shell may send); it
  is never built into the APK. Rotate: `tv-agent setup token --rotate`, then `tv-agent tv pair`.
- **Stremio auth key**: full access to your Stremio account, obtained with Stremio's own link login (no password
  typed anywhere). Your addons' URLs carry your debrid key and stay in memory only. Logs keep only hostnames of any
  URL and mask known secrets (`redact.py`).
- **Kodi** web control is plain HTTP on the LAN with a password, like ADB.
- **Just Player** gets the debrid link on its `am start` command line (over ADB), and `ffprobe` gets it as an
  argument on this computer.
- **ADB** must stay on for this (USB debugging, port 5555).

## Undo

```bash
systemctl --user disable --now tv-agent llama-d1 && rm ~/.config/systemd/user/{tv-agent,llama-d1}.service
tv-agent logout                                              # invalidates the Stremio auth key
tv-agent tv shell pm uninstall com.boujuan.tvagent
tv-agent tv shell settings put secure accessibility_captioning_enabled 0   # if a Just Player session was cut off
# Remove "84:com.boujuan.tvagent" from your launcher's key mapping
# On the TV: Kodi > Settings > Services > Control > HTTP off; Settings > Privacy > Microphone off (if wanted)
rm -rf ~/.config/tv-agent ~/.local/share/tv-agent ~/.local/opt/llama.cpp ~/.local/opt/llama.cpp-dl
```

## Files

| Path | What it is |
|---|---|
| `src/tv_agent/server.py` | The daemon: LAN listener for the TV app, localhost API, background execution |
| `src/tv_agent/cli.py`, `install.py` | The `tv-agent` command; user services, TV app install and pairing |
| `src/tv_agent/stt.py` | Speech-to-text (Parakeet via sherpa-onnx) |
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
