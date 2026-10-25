# tv-agent server

The daemon behind tv-agent: speech-to-text, the decision rules and d1-3B, TV control over ADB, Stremio → player
playback and progress sync. Also the `tv-agent` command line and the MCP server. Part of
[tv-agent](../README.md); the desktop panel and the TV app talk to it over its HTTP API.

## What you can say

| Kind | Examples | What happens |
|---|---|---|
| Films and series | "play The Bear season 3 episode 2", "put on Dune Part Two", "play Severance from the start", "play la casa de papel", "play el hoyo" | Your Stremio library first, then Cinemeta and your metadata addons side by side (AIOMetadata knows Spanish names: *el hoyo* is The Platform); Cinemeta's popularity, then d1, settle close calls; when d1 isn't sure (under 40%) or can't be asked, the candidates are listed on the TV instead of a guess played ("number 2" plays that one, as asked). Plays from your addons; resumes where you stopped unless you say "from the start" |
| Continue / next | "continue The Bear", "keep watching", "carry on with the show", "next episode", "previous episode" | Stremio's Continue Watching; past 90% it goes on to the next episode. With the player open, "continue" just unpauses |
| Same show, other episode | "go to season 4 episode 2", "play episode five", "season two" | No title: the series that's on (the one the agent started, else your latest in Continue Watching) |
| Stremio page | "show me The Office in Stremio" | Opens the title (or episode) in Stremio without playing |
| YouTube, Twitch | "lofi hip hop on YouTube", "search YouTube for paella", "Ibai on Twitch" | Top YouTube result in TizenTube; Twitch live stream |
| Apps | "open Prime Video", "go back to YouTube", "open Movistar", "go home" | |
| Playback | "pause", "resume", "skip forward thirty seconds", "go back ten seconds", "go to twelve minutes", "go to the middle", "back to the beginning", "stop" | Kodi: exact, over its API. Just Player: media keys for skips, reopening the link at the time for jumps. Other apps: media keys only |
| Volume | "volume twenty", "volume to the max", "louder", "turn it down a bit", "mute", "unmute" | TV volume 0-100 |
| Binge mode | "binge mode on", "turn off binge mode" | On (default): when an episode ends the next one starts, already found and checked 20 s into the episode. After a film or a season's last episode: Home |
| Sleep timer | "stop after this episode", "turn the TV off after this one", "stop in 30 minutes", "turn off in an hour", "cancel the sleep timer" | Stops (then Home) or puts the TV to sleep; "after this one" wins over binge mode |
| Subtitles, audio | "subtitles on", "subtitles off", "subtitles" (switches them over), "Spanish subtitles", "original audio", "audio in English", "ponlo en español" | The file's own track, else your subtitle addons for that release; a language the file lacks: the best other copy that has it, from the same moment (the TV says it's looking). Kept for the title's next episodes |
| Language settings | "from now on, audio in Spanish", "a partir de ahora sin subtítulos", "always English subtitles", "subtítulos solo cuando no sea en español" | Your `audio_language`, `subtitles` and `subtitle_languages`, as the panel and the phone change them; what's playing follows |
| Missed a line | "what did they say?", "say that again" | Back 10 s, subtitles on for a moment |
| Intro, recap, credits | "skip the intro", "skip the recap", "skip the credits"; "auto skip on" / "off" | Jumps to the end of the part you're in, found in the file itself (its chapters, other copies', the season's sound; below), else from TheIntroDB / IntroDB; before a sure intro: "The intro starts at 6:23: skipping it then"; an intro with no times at all: 90 s ahead; credits: the next episode in binge mode. Auto skip (off by default, `auto_skip`) skips sure intros and recaps by themselves. TV Agent Player shows a Skip intro button and, at the credits, a Next episode card (OK to accept; counting down with binge on) |
| Lists | "what was I watching?", "what's new?", "new films", then "number 2" / "the second one" | A numbered list on the TV (posters); the remote's OK or number keys pick too |
| A moment | "go to the part where they kiss", "ve a la parte en la que le disparan", "jump to when Paul rides the worm" | Found in the subtitles of what's playing, in the language you asked in; the line it was found by shows on the TV, then the player jumps there. Not sure where: the places to pick from. In the episode before or after: offered ("yes" goes there). [Finding an episode or a scene](#finding-an-episode-or-a-scene) |
| An episode described | "the Simpsons episode where Mr. Burns gets shot", "el capítulo de Breaking Bad de la mosca", "the Community Halloween episode" | Found by its plot (Wikipedia's and TVmaze's summaries, kept a week) and checked in its subtitles; when more than one fits, or none, up to 3 are listed to pick from: never a guess played |
| Suggestions | "recommend something like Dark", "what should we watch?", "put on a comedy under 90 minutes", "recomiéndame una serie de miedo", "algo como La casa de papel" | The LLM suggests titles that fit, with what you watched lately as taste; each is checked by the title search (and its length, when you gave a limit), and the ones you've seen are left out; a numbered list on the TV, "number 2" plays it |
| What's on | "what am I watching?", "where am I in Severance?" | The title, how far in and when it ends; your last episode of a title and how far in |
| A few in one go | "pon el capítulo 5 de la segunda temporada de The Bear y vete al minuto 12", "play Dune Part Two and go to the scene where Paul rides the worm", "pon Oppenheimer desde el principio con subtítulos en español", "cancel" | A play, then up to two steps on what it plays, each once the last one is done: [Chains](#chains-a-play-and-what-follows) |
| Help | "what can you do?", "help" | Every command as a pattern with its ‹placeholders›, by section, on the TV in the language you asked in (also the panel's Commands tab) |
| Newest episode | "play the latest episode of The Bear" | The newest episode out |
| Your library | "add this to my library", "remove Friends from my library", "mark this as watched", "mark The Bear season 3 as watched", "mark it as unwatched" | Written to your Stremio library, with Stremio's own per-episode watched marks |

Audio and subtitles: a title made in `native_language` plays in it; anything else plays in `audio_language` (a
language, or `"original"`: the one it was made in) when the file has it, and streams that have it come first. The
language a title was made in is the files' own original track when AIOStreams reads it, else its first country.
Subtitles: the first available of `subtitle_languages` (English, Spanish, Chinese Simplified/Traditional and
others), shown per `subtitles`: `"foreign"` (default) only while the audio playing isn't `native_language` (a
Spanish dub, a Spanish-made film: none; a file without your audio: on), `"always"` or `"off"`. Audio or subtitles
asked for by voice hold for that title (its next episodes too) for 12 hours. Kodi and TV Agent Player apply this per
file; Just Player only partly (see below).

A language the file lacks ("ponlo en español" over an English-only file): the best other copy by the stream rules
below that has it takes over at the same moment. What AIOStreams read from each file's own tracks (or its name says)
comes first; the best few copies that say nothing get their tracks read from the file's Matroska header (one range
read each, at once). With `probe_streams` off only what the addons say counts. No copy with it: what's playing goes
on, and the TV says so.

Intros, recaps and credits are found in the files themselves (`intros.py`): the services' own markers are named
chapters in most web releases ("Intro", "Opening Credit", "Recap", "End Credits"), read from the file's header when
it starts; other copies' headers ~20 s later, one per release group, and a marker is sure when two groups agree (one
group's can be a minute off: FLUX's Pluribus); another cut's chapters are moved onto your file by matching a few
seconds of their sound; and with no chapters anywhere, the season's intro is learned from the sound of two or three
episodes' starts, as Plex and Jellyfin do (credits by sound find only the studios' logos at the end). In the
background, at most 80 Mbit/s, pausing while TV Agent Player's buffer is low, the next episode's while this one
plays; kept per file and per season in `~/.local/share/tv-agent/intros/`. Measured accuracy and cost:
[docs/intros-2026-10-24.md](../docs/intros-2026-10-24.md). What a file lacks comes from TheIntroDB and IntroDB (the
episode's IMDb id and the file's length), which only jump when the part you asked about contains where you are, and
skip by themselves only with timestamps made on a file within 2 s of yours' length, from 3 or more people (or both
databases agreeing). Rules and coverage: [research §2.6](../docs/research-2026-10-22.md).

In Spanish too, with the multilingual speech model (`tv-agent setup stt parakeet-v3`): "pausa", "siguiente
capítulo", "pon la casa de papel temporada dos capítulo tres", "sube el volumen", "¿qué hay de nuevo?", "salta la
intro", "eso está mal". A clean-up layer turns the Spanish into the English the rules know; titles stay as said.
A request asked politely is the command itself: "¿puedes poner…?", "¿me pones…?", "¿podrías ir al minuto 20?", "¿me
bajas el volumen?", "¿puedes cerrarlo?" ("close it", "ciérralo": stop).

Anything it doesn't understand shows "Didn't get that: …" and does nothing. While the pill listens, the volume
is halved (Kodi's own volume when Kodi plays, else the TV's) and comes back afterwards; nothing is paused.

## Chains: a play and what follows

"Pon el capítulo 5 de la segunda temporada de The Bear y vete al minuto 12" is two steps: play that episode, then
jump to 12:00. A chain is heavily guarded; anything it isn't sure of, it doesn't chain.

**What can be chained**, fixed (`actions.chain_problem`): first one step that plays something (a title, an episode,
"continue …", "next episode"), then one or two that act on what it plays, in the order said:

| Step after the play | Say | Checked by |
|---|---|---|
| A jump: a time, the middle, the start | "… y vete al minuto 12", "… and go to the middle", "… from minute 12", "… desde el minuto 12" | The position, within 10 s of it; a time past the end fails |
| A jump: a scene | "… and go to the scene where Paul rides the worm", "… y ve a la parte en la que se besan" | The line it was found by, shown on the TV for 2.5 s before the jump; the position |
| A jump: past the intro or recap | "… y salta la intro" | Skipped when it comes (TheIntroDB / IntroDB, as "skip the intro") |
| Audio or subtitle language | "… con subtítulos en español", "… with English subtitles", "… in English", "… en versión original" | The track playing is in that language (a language the file lacks: another copy that has it takes over, as usual) |
| Volume | "… and turn it up", "… y pon el volumen a 20" | The TV's level (a set level or mute) |
| Pause | "… y pausa" | The player paused; a pause comes last |

At most three steps, one jump (a time and a scene would fight), each kind once. Never after a play: apps, Home,
settings ("from now on…"), timers, binge or auto skip, stop, relative skips, another title, lists. A sentence of
commands that can't be chained runs **the first one alone**, and the TV says what wasn't done and why ("Not done:
“apaga la tele cuando acabe” (a sleep timer can't follow a play)").

**How it's read** (`decide/chain.py`, before the decision steps):

- Rules first. The sentence is cut at its joins ("and", "then", "y", "y luego", "después", commas) and endings that
  ride on a play are cut off ("… con subtítulos en español", "… in English", "… from minute 12"); every part after
  the first must be a command the fixed phrases or pattern rules know on its own. The first part then goes through
  the decision steps like any command, and must be a play some step is sure of (the same bar as a single command:
  a pattern rule, the LLM, d1 only when it's sure), said with a play verb or an episode ("the bear and pause" isn't
  one). "Pride and prejudice", "treinta y cinco" and "an hour and a half" stay one command.
- The LLM only proposes. When the rules find no chain but the sentence sounds like one (a play, then a join or an
  ending, then words of a step that can follow), the LLM is asked for its steps as JSON in a strict schema, in place
  of the LLM step. Any action or field outside the chain's list, a step's field put in the play ("seconds" in
  play_title: two steps merged), a step after the play whose words weren't said (a pause nobody asked for), or a
  shape the rules above refuse rejects **the whole chain**: the first step alone runs if it holds up by itself,
  with a note; else nothing happens. A one-step answer is the LLM's usual single command.

**How it runs** (`chain.py`):

1. The plan shows first: in the pill ("The Bear S2E5, then 12:00"), then on the TV with "say “cancel” or press
   Back to stop", and on TV Agent Player's loading screen while the title starts ("Finding streams… · then 12:00").
2. The play step, then its proof within 75 s: something new playing, the episode asked for (by its id: "it started
   S2E6 instead" stops the chain), its picture moving (TV Agent Player's first frame, Kodi's length, Just Player's
   state).
3. Each step after it, once the same title still plays: done, then checked (table above), within its own limit
   (20 s for a jump, 50 s for a scene, 30 s for a language, 8 s for the volume or a pause). A language change or a
   pause first waits up to 25 s for the episode check's verdict on a file it hasn't checked before: a wrong file
   is replaced by the right one at the same moment, which keeps a jump but not a track or a pause.
4. A step that fails or runs out of time stops the chain: "Step 2 (12:00): 12:00 is past the end (0:24:10) · not
   done: pause". Nothing is tried again.
5. **"Cancel"** ("cancela", "never mind", "olvídalo"), any other command but the volume, or **Back** in TV Agent
   Player stops it at once. A start still on its way stops too (its loading screen closed); a title already
   playing goes on. In Kodi and Just Player, Back isn't seen: say "cancel". "Cancel" also stops a single start on
   its way, and says "Nothing to cancel" when there's none.

**Scenes and episodes described** are found as [below](#finding-an-episode-or-a-scene); a chain's play that
lists episodes to pick from stops there, and the pick goes on with the chain's other steps.

`scripts/bench_chains.py` decides the chained sentences of `eval/chains.jsonl` with your setup (rules, the LLM) and
judges each step; `--off llm` for the rules alone. Chained commands said to the TV become voice tests like any
other (`scripts/bench_voice.py --seed`, below).

## Finding an episode or a scene

`finding.py`. Nothing other than what you asked for plays without asking: when it isn't clear, a list to pick from
("number 2"; "yes" or "sí" for the first).

**An episode** by its name ("Advanced Dungeons & Dragons") is matched at once. Described after the show's name ("play
‹show›, the one where…", "‹show› episode where…", "the episode of ‹show› where…", "pon ‹show›, el capítulo donde…",
"¿puedes poner el episodio de ‹show› en el que…?"), the rules take it at once: the show's name goes to the title
search and the rest to this search, never one title made of both. By what happens in it:

1. Each episode's plot: Cinemeta's line, TVmaze's summary and English Wikipedia's ("List of … episodes" and each
   season's page, which tell the side plots too), kept a week in `~/.local/share/tv-agent/finding/plots/`, and
   fetched ahead for the shows in your Continue Watching when the mic opens (the first time for a long show takes
   ~3 s: The Simpsons is 39 pages).
2. Meanwhile the LLM gives the words to look for in English and Spanish, and up to 3 episodes it knows it could be.
3. The plots are ranked by those words here (BM25, milliseconds); the best 10 and the LLM's guesses go to the LLM
   with their whole plots. It says which fit, each "exactly" (what was asked happens in it) or "partly".
4. Exactly one fits exactly: it plays, once its subtitles (fetched while the LLM chose) have some of the words; a
   pick whose subtitles say none of them is listed instead. More than one (the Halloween episodes), or only partly
   ones: listed. None: the subtitles of the likeliest 30 episodes are searched for 3 s, then the closest listed.

**A scene** of what's playing:

1. A description cut off while it was said ("… where Paul rides the") is refused: say it again.
2. Its subtitles in the language you asked in first, hearing-impaired ones before plain (their [sound cues] tell
   what happens), then the other language. Kept on disk (`finding/subs/`) and fetched ahead from every addon when a
   title starts; a search doesn't wait for slow addons (OpenSubtitles PRO answers in 6-9 s): what the fast ones
   (SubSense, ~0.5 s) give is used, and the rest is there next time.
3. The words to look for, in both languages and the show's own names for things (a nickname, what the characters
   call a creature or a place); the passages that hold the most of them (your own words count more) go to the LLM:
   about 200 lines instead of 1,700. Each reaches further back than forward (10 lines before a line with the words,
   6 after: a moment often comes just before the lines that react to it or talk about it), and a gap of a few lines
   between two passages is sent too. The line it's placed by is said before the moment or as it happens, never one
   that explains it afterwards.
4. The LLM is asked twice at once and quotes the line it places the moment by; the agent finds that line in the
   subtitles (the model's own arithmetic was once 7 hours off), and it must be among the passages with your words.
   Both answers at the same place: the jump, 3 s before the line, after the line shows on the TV in your language
   (translated when the subtitles were in the other). They differ: a third answer settles it, else the places are
   listed to pick from.
5. No passage has the words: the episodes before and after are searched the same way; found there, it's offered
   (a list of one: "yes" goes there). Else: "it isn't in the subtitles": a moment without words or sounds in them
   can't be found this way.

Times are matched to the file playing: the episode check measured how far its subtitles are from the file, and the
subtitles searched are matched to those line by line. `scripts/bench_finding.py` times both with your addons and
the LLM on `eval/finding.jsonl` (cold: nothing kept; warm), nothing sent to the TV:

| 2026-10-24 | episode, cold | episode, warm | scene, cold | scene, warm | right |
|---|---|---|---|---|---|
| Before (Cinemeta's lines, one LLM pick; all the subtitles to the LLM) | p50 0.40 s | p50 0.38 s | p50 0.75 s, p95 1.9 s | p50 0.36 s | episodes 4/8 (Spanish ones and "the Halloween episode" wrong), scenes 1/2 |
| After | p50 2.0 s, p95 7.4 s | p50 1.2 s, p95 2.1 s | p50 1.9-5.2 s | p50 1.0 s, p95 1.8 s | episodes 7/8 (the Halloween, paintball and D&D ones listed), scenes 14 of 16 runs right, 1 listed |

The cold numbers are a show's or a film's first time ever; a title started fetches its subtitles ahead, and the
plots of what you're watching are kept, so a real request is usually the warm one.

## Player: TV Agent Player, Kodi or Just Player

`player = "tvplayer"` (the default), `"kodi"` or `"just"` in `~/.config/tv-agent/config.toml` (or the panel's
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
| While it starts, and paused | A loading screen with the title, its artwork, what you said and the step it's at; on pause, the plot, the stream's source, debrid service, size, release and badges (with a player that has these screens) | Kodi's own screens |
| Jumps, skips, intros | Exact, in place, also in Dolby Vision; ±N s at the nearest keyframe, fast commands added up | Exact, but a Dolby Vision file is reopened at the time (its decoder's flush fails on this TV) |
| Holding ← or → | The target glides along the bar with a picture of it above, made on this computer from the file (`tv/thumbs.py`: one range read and ffmpeg per picture, ~0.45 s on the TV; Matroska files; player 0.5.0) | Kodi's own seek bar |
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

## Starting fast

Where a start's time went (33 starts on the TV, 2026-10-24, TV Agent Player 0.6.2, from the log): from your last
word, 1.3 s to the decision (the speech to text ~1 s of it), 0.4-0.7 s for the title and its details, then the
stream lookup (AIOStreams: ~2 s for a title it was asked for lately, 7-9 s for one it wasn't, up to its 8 s
timeout), 0.8 s to check the link and ~2 s for the player's first frame. 10.4 s p50 when the streams had to be looked
up, 4.6 s when they were known.

What the agent does about it:

- **Lookups start as soon as the video can be told.** A film; an episode by season and number; "episode 3" (of the
  season you're on) and "season 2" (its first episode) before Cinemeta's episode list is in; where you were; the
  episode the plots point to while an episode is found from what happens in it; the title the search leans to while
  d1 decides; Continue Watching's first two when the mic opens. One lookup per video at a time: a start waits for the
  one already running.
- **Answers are kept.** 30 minutes as they are. Up to 12 hours, an answer whose best stream already fits your rules
  (the top resolution allowed, the language wanted) or that is for a film or episode out for more than two weeks is
  used at once and asked for again meanwhile, for the next time; if none of its links answers, the new answer is
  waited for, and if the addons fail, the kept one is used.
- **A link is checked once.** As soon as a lookup made ahead is in, the link of the stream a start would pick is
  followed to the file on the debrid service (the addon's redirect: 0.5-1.5 s). That address is kept 20 minutes:
  given as it is right after it answered (90 s), else after one request to it (~0.2 s).
- **An addon that times out or drops the connection is asked again at once.** AIOStreams goes on with an answer it
  was too slow with, so the second ask is shorter (2-5 s measured); before, the start said "No playable stream".
- **Connections stay open 5 minutes, and open while you speak** (OpenRouter for the speech to text and the LLM,
  Cinemeta): no TLS handshake (~0.3 s each) after your last word.
- **The trace says where it went:** each addon's time and answer ("Addons asked"), how the link was found (kept,
  checked at its address, asked the addon, with each request's host and time, never its path), and in the log the
  player's own account of each start (`player timing …`: first byte, file prepared, first frame, playing).

`scripts/bench_starts.py` times starts from a decided request to the link the player would get, with your addons and
debrid service and nothing sent to the TV, in four cases: nothing kept (cold), the same start again, the same start
31 minutes later, and a start whose lookup began 4 s before (as when the mic opens).

Measured 2026-10-24 (8 titles and episodes, the code before and after these changes taking turns title by title,
p50 / p95 in seconds, from the decided request to the link):

| Case | Before | After | Waiting for the streams | The link check | Addon lookups |
|---|---|---|---|---|---|
| Nothing kept | 3.86 / 8.48 | 4.49 / 14.02 | 2.42 / 6.86 → 2.94 / 12.84 | 0.44 / 1.61 → 0.53 / 1.08 | 8 → 8 |
| The same start again | 0.70 / 1.58 | **0.00 / 0.23** | 0 → 0 | 0.70 / 1.06 → 0 | 0 → 0 |
| 10 minutes later | 1.13 / 1.42 | **0.22 / 0.22** | 0 → 0 | 0.80 / 1.09 → 0.20 / 0.20 | 0 → 0 |
| 31 minutes later | 4.60 / 7.66 | **0.87 / 1.57** | 3.69 / 5.90 → 0 | 0.54 / 1.43 → 0.83 / 1.54 | 8 → 0 |
| Looked up 4 s ahead | 0.97 / 5.26 | **0.00 / 0.19** | 0 / 3.48 → 0 | 0.57 / 1.45 → 0 | 2 → 0 |

With nothing kept the time is AIOStreams' (its public instance was slow and uneven during the runs: 1.2-7.1 s for
the same kind of title); one of its answers timed out at 8 s, was asked again and played (14 s), where it used to
fail. On the TV the rest of a start stays: ~1 s from your last word to the decision and ~2 s to the player's first
frame.

What's left is outside the agent: AIOStreams' own time for a title it wasn't asked for lately (the public instance's
addons' timeouts, in its own settings) and TV Agent Player's ~2 s to its first frame.

## Voice: decided while you pause

The TV app streams the remote's microphone in 20 ms chunks from the moment the pill opens (`stream_port`, 8767,
on the same address and token as the TV's HTTP endpoint). tv-agent tracks speech and quiet as it arrives and,
in the quiet after you speak, transcribes and decides: at ~160 ms of quiet for a complete command that can't be
the start of a longer one ("pause", "next episode", "go home", "… season 3 episode 2"), at ~460 ms for other
understood commands ("volume twenty" could still become "twenty five"), at ~720 ms otherwise (a title alone, since
a season often follows), and at ~1.5 s when the words so far can't end a command ("… throws the crystal at",
"cambia los subtítulos a": people pause before a name). If you go on talking, the early result is dropped. When no
voice is heard at all (the remote's mic sending near-silence, the film faintly behind it), nothing is transcribed. Measured with the remote on
2026-10-22: 240-310 ms from your last word to the decision for complete commands (it was ~0.9 s). The pill shows
what it has understood so far. Pressing the mic again sends at once; without the stream the app falls back to
recording until 0.7 s of quiet and sending it all. Logic: `listen_stream.py`; protocol: `server.py`.

Speech-to-text can also be hosted (`stt_where = "hosted"`, or `"hosted-first"` with this laptop's model as backup,
or `"local-first"`, hosted only when this laptop's model fails, hears nothing or writes in another script): an
OpenRouter transcription model (`stt_hosted_model`) gets the recording at the same checkpoints, with a language hint
(`stt_language`, your native language by default) and, for AssemblyAI, a vocabulary of names to hear as written
(`stt_vocabulary`, or the hint's own list in `stt_vocabularies`, plus your library's titles and Twitch favourites with
`stt_vocabulary_library`; the first 100 go,
most likely first: what's playing and Continue Watching, yours, what you watched, Twitch, the rest of the library;
the titles as Stremio names them with a Spanish hint, Cinemeta's original names with an English one, both without a
hint). Parakeet guesses
the language of every command and can't be limited: on 30 real commands from a Spanish speaker it wrote 11 wrong
(Russian or French spellings of short Spanish commands, "Simpsons" as "sin"), where AssemblyAI Universal-3.6 Pro
with a Spanish hint and the vocabulary wrote none wrong, ~0.26 s and ~$0.0002 a command (docs/research §2.12). The
local model then isn't loaded (~1 GB less memory). `keep_recordings = true` keeps each spoken command's audio (the
last 300, in `~/.local/share/tv-agent/recordings/`, mode 600) for the panel's Activity tab to play or transcribe
again, and for [voice tests](#voice-tests).

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
3. **Chains** ("play ‹title› episode 2 and go to the middle"), before the steps: the sentence cut at its joins,
   every part after the first a command the rules know, the first part through the steps; else, when it sounds
   like one, the LLM's steps, checked. See [Chains](#chains-a-play-and-what-follows).
4. **Apps and hooks:** the only apps the agent may start, the names you can call them, and which app gets YouTube,
   Twitch and Stremio-page links (and the link format).

Saving checks everything first (regexes compile, actions and fields exist, hooks point at real apps) and keeps the
current rules if anything's wrong; a hand-edited `rules.json` is picked up on the next command the same way.
The rules and the models' prompts are general: none names a title, a show, a character or an episode (their examples
say X, ‹title›), so a fix for one command is a fix for every command said that way; real titles are only test data
(`eval/`, your voice tests).
A `rules.json` saved before rules version 2 keeps working (its broad `transport` and `volume` intents are still
understood) but doesn't get the new phrases, patterns and intents: in the panel, **Reset to defaults**, then redo
your own changes.
Every decision records the path it took, which the panel draws.

Stream choice (`stremio/streams.py`): only direct debrid links, cached, ≤ `max_height`, ≤ `max_mbps`, no cam rips.
Of those: the file has the audio the title should play in (`languages.plan`: AIOStreams' reading of the file's
tracks or name, other addons' flags and filename words; Latin American Spanish just below Castilian), then the
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
.venv/bin/tv-agent setup services --enable  # writes ~/.config/systemd/user/{llama-d1,tv-agent}.service for this copy
../android/build-and-install.sh             # the TV app: install, microphone permission, pairing
.venv/bin/tv-agent tv install tv-agent-player-<version>.apk   # TV Agent Player, the default player: install, pairing
.venv/bin/tv-agent doctor                   # all "ok"
```

Optional:

```bash
# Kodi as the player instead (player = "kodi"): on the TV, Kodi > Settings > Services > Control > Allow remote
# control via HTTP (8080, user kodi, a password); then this asks for that password, checks it with Kodi, saves it
.venv/bin/tv-agent setup kodi
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
`tv_hosts` and `phone_hosts` edited in `config.toml` are used the same way, without a restart. A paired phone learns
the new address the next time it reaches the agent at any of its addresses (this computer's Tailscale one is offered
by itself), and keeps its list current from then on (phone app 0.2.2).
TV Agent Player's pictures for a held ←/→ need `ffmpeg` on this computer, built with libplacebo (Vulkan) to
tone-map HDR and Dolby Vision the best way; without it they use zscale, and without ffmpeg there are none.
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
| Check a change against what you actually said | `.venv/bin/python scripts/bench_voice.py --compare before.json` ([voice tests](#voice-tests)) |
| Run a command on the TV | `tv-agent tv shell dumpsys media_session` |
| Re-pair the TV app (new token; a new address is paired by itself) | `tv-agent tv pair` |
| Let Claude Code drive the TV | `claude mcp add --scope user tv-agent -- $PWD/.venv/bin/tv-agent-mcp` (12 tools, no shell) |
| Use it from my phone | The [TV Agent phone app](https://github.com/boujuan/tv-agent-phone); pair it in the panel's Settings › Phones |
| Stop it for a while | `systemctl --user stop tv-agent llama-d1` (the mic button then says "Agent offline") |
| Fix a command it got wrong | Say "that's wrong" right after; then in the panel's Activity tab right-click it, "This was wrong, it should be…": it's taught and becomes a test (or `tv-agent review` for the list, with an LLM's guess) |
| See how well it does, step by step | The panel's Quality tab ([quality](#quality-and-the-nightly-check)); `tv-agent quality nightly` runs the night's check now |

## Voice tests

With **Keep recordings** on (the panel's Activity tab), each spoken command's audio stays with its history entry.
`scripts/bench_voice.py` replays the ones you've labelled through speech to text and the decision steps, set up as
the running agent has them (rules, taught sentences, steps on and off, models), and judges each against its label.
Nothing is sent to the TV; the LLM and hosted models are called, and cost, as in use (`--off llm,d1`: rules only).

```bash
.venv/bin/python scripts/bench_voice.py --seed --save before.json   # label new recordings, run, keep the results
# … change the rules, a model, the code …
.venv/bin/python scripts/bench_voice.py --compare before.json       # regressions and fixes (exit 1 on a regression)
.venv/bin/python scripts/bench_voice.py --stt local,parakeet-v2,mistralai/voxtral-mini-transcribe-3-0,heard,text
.venv/bin/python scripts/bench_voice.py --stt assemblyai/universal-3-6-pro@es,assemblyai/universal-3-6-pro@none
.venv/bin/python scripts/bench_voice.py --transcripts before.json --compare before.json   # a rules change alone
```

- **Labels** live in `~/.local/share/tv-agent/voice-tests/labels.jsonl` (mode 600, never in the repo), one per
  recording, its audio hard-linked beside it so the agent keeping only its last 300 recordings, or erasing the
  history, doesn't take it. Removing a test: its line and its `.wav`.
- `--seed` labels each recording not labelled yet with the action it got, when it worked and wasn't marked "that's
  wrong", or with what you taught it in the Activity tab; it says how many it left out and why (not understood,
  didn't work, marked wrong but not taught). In the panel, right-click a command (Activity or Decisions), **This
  was right** or **This was wrong, it should be…**: that also asks for the words said, so the transcripts are judged
  too. A typed command (no recording) becomes one of your sentences for `bench_chains.py`, and an episode or a scene
  it searched for a case for `bench_finding.py`, with the episode or moment it found or the one you give
  (`voice-tests/sentences.jsonl`, `finding.jsonl`, yours alone like the labels).
- A label: `{"recording": "…wav", "expect": {…}, "exact": true, "text": "what was said"}`. `expect` is as in
  `eval/*.jsonl`; with `exact` every field it doesn't name must be at its default (film or series and a guessed
  title count only when named). Edit one by hand to loosen it (`"exact": false`; `"title_any"`,
  `"episode_name_any"`: any of these words; `"seconds_any": [0, null]`: any of these values) or correct it.
- Per recording: ✓ right, ~ right kind of action, ✗ wrong; the transcript, ✓/✗ against `text` (case, punctuation,
  accents and "three" / "3" don't count); the timings. Per speech model: how many right, word error rate, p50/p95
  of speech to text and deciding, which steps decided, cost. `--stt heard` decides on what the agent heard back
  then (the decision steps alone), `text` on the words said (what perfect hearing would get). A hosted model
  with `@es`, `@en` or `@none` after its id gets that language hint and that hint's names instead of the agent's
  own, so hints can be compared in one run.
- `--transcripts run.json` reuses a saved run's transcripts (same `--stt` names): the decision steps alone, with no
  speech-to-text cost, and none of the hosted model's run-to-run wobble (2-3% of AssemblyAI's transcripts change
  between two identical runs) in a comparison.
- `--save` / `--compare` with a bare file name keep the run with the voice tests (its transcripts are yours);
  `--compare` lists regressions, fixes, changed transcripts and what changed in the setup (commit, models, steps).
  Also `--no-learned` (without the taught sentences), `--failures` (only what's wrong).

## Quality and the nightly check

The panel's **Quality** tab reads the history's traces (today, the last 7 or 30 days, or all; from the TV, the
phones, typed or the laptop's mic) into `quality.py`'s numbers: for each step (speech to text, deciding, the title,
the episode, the scene, the streams, the start, the whole command) how often it went right, how often it asked
instead of guessing, and its p50/p95 time; which rule or model decided and how often that was right (it worked and
wasn't marked wrong); each model's calls, cost and time; the cost per day; the commonest failures, each opening an
example in Decisions.

Every night at 04:30 (`tv-agent-nightly.timer`, `tv-agent setup nightly --enable`; missed nights run at the next
start) `tv-agent quality nightly` runs `bench_voice.py` (your voice tests through the agent's own speech to text),
`bench_chains.py` and `bench_finding.py` with your setup, nothing sent to the TV, and compares each case with its
baseline (the first run's, until a run is accepted as the baseline after a change made on purpose). A case that was
right and is wrong now is run again, twice at most: it's a regression only when it's wrong every time, else it's
"unsteady" (the LLM's answers and a hosted speech model's transcripts vary from run to run; shown, not alerted). A
case that couldn't be checked (the hosted speech model failing, a subtitle addon's downloads failing) counts neither
way. The runs stay in
`~/.local/share/tv-agent/quality/runs/` with what they cost (the LLM, d1 and a hosted speech model are called as in
use: $0.10 and 3 minutes for 207 voice tests, 29 sentences and 24 searches). The folder is yours alone (mode 700). A
regression leaves `quality/alert.json`: the panel notifies you, and a paired phone
asking `GET /v1/quality` sees it, until it's seen.

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
| `GET/POST /v1/pipeline` | decision steps, models, speech-to-text (where, models, loaded, keep recordings, language hint, vocabulary; `stt_vocabulary_sent`: every name sent, in the order sent; `stt_vocabularies`: each hint's own names, set one language at a time, `null` removes one; `stt_vocabulary_sent_by_language`: what each hint would send), live |
| `POST /v1/transcribe` `{"recording", "where", "model"}` | transcribe a kept recording again |
| `GET /v1/tv/status`; `POST /v1/tv/speedtest`, `/diagnose`, `/reconnect` | the TV, its network and the ADB link |
| `POST /v1/tv/key` `{"key"}`; `GET /v1/tv/screenshot` | a key from a fixed list; the screen as PNG |
| `GET /v1/now-playing/link`; `POST /v1/tv/play-link` `{"url"}` | the stream the agent plays; play any http(s) link |
| `GET /v1/tv/packages` | the TV app, TV Agent Player, Kodi and Just Player on the TV: installed, version, enabled; the app's APK SHA-256 |
| `GET /v1/about` | this agent's version, Python, start time and the models it uses |
| `GET /v1/now-playing` | the title, episode (name, poster), position, Kodi's audio and subtitle tracks, intro/recap/credits |
| `POST /v1/now-playing/track` `{"audio": i}` / `{"subtitle": i \| null}` | one of the player's tracks by its index (a language instead, as by voice: `POST /v1/action` `{"type": "tracks", "audio": "spa"}`) |
| `POST /v1/remote/key` `{"key", "long"}`; `POST /v1/remote/text` `{"text"}` | a key (d-pad, OK, numbers…, `remote.KEYS`); typing into the TV's focused field (ASCII) |
| `GET /v1/tv/volume`; `GET /v1/apps`; `GET /v1/apps/icon?package=`; `POST /v1/apps/refresh` | the TV's volume; the apps on the TV the agent may open (what its home screen lists, read by the TV app with names and icons at the start, daily and on request; one not among the rules' apps can be opened by voice too, by its name); one's icon; read them again now |
| `GET /v1/help?lang=en\|es` | everything you can say, by section, with an example sentence each (the title left as ‹title›) |
| `GET /v1/shown`; `POST /v1/shown/pick` `{"number", "id"}` | the numbered list on the TV now ("number 2" picks), or null; pick one of it (409 once the TV shows another list) |
| `GET /v1/lists?list=recent\|new\|movies`; `POST /v1/lists/pick` `{"list", "imdb", "video"}` | the numbered lists voice shows on the TV, returned instead; play one (it's also what "number 2" picks from) |
| `GET /v1/activity?limit=&before=`; `POST /v1/activity/mark` `{"t", "wrong", "heard"}`; `GET /v1/recording?name=` | the history, newest first, a page at a time; mark one wrong; a kept recording (WAV) |
| `GET /v1/activity/details?t=`; `POST /v1/activity/right` `{"t", "words"}`; `POST /v1/activity/wrong` `{"t", "words", "should", "episode", "moment"}` | a command's big parts on demand (what its searches read, model prompts); "This was right" and "This was wrong, it should be…" as in the panel (kept as tests; teaching is `/v1/learn`) |
| `GET /v1/quality?days=7&sources=tv,phone`; `POST /v1/quality/seen` | the [Quality](#quality-and-the-nightly-check) numbers (`days=0`: all), the last nightly runs, a regression not seen yet (`alert`, `null` once seen); seen |
| `GET/POST /v1/settings` | the Settings tab's playback and language settings (`remote.SETTINGS`), checked, saved and live; `choices` lists what each takes (`audio_language` with "original", `subtitles_when`) |
| `GET /v1/updates`; `POST /v1/updates/check`; `POST /v1/updates/tv`, `/tvplayer` | the latest GitHub release of the agent, panel, TV app, TV Agent Player and phone app against what runs (`updates.py`; checked every 6 h unless `updates = "off"`); install the TV app's or the player's release over ADB (localhost only; only with GitHub's SHA-256 for it; the player not while it plays) |
| `GET /v1/clients`; `POST /v1/clients/pairing`, `GET` it, `/pairing/cancel`; `POST /v1/clients/revoke\|rename` `{"id"}` | paired phones (localhost only): list, pair (a one-time code and the QR code's link), revoke |

Phones, paired from the panel (Settings › Phones), on this computer's LAN address (and `phone_hosts`, e.g. a
Tailscale address), for the [TV Agent phone app](https://github.com/boujuan/tv-agent-phone):

| | |
|---|---|
| `POST /v1/pair` `{"code", "name"}` (port 8768, HTTPS) | the panel's one-time code → this phone's own token, the agent's name and version, the stream port |
| the endpoints above, with that token (port 8768, HTTPS) | all but the stream link, the rules editor (`POST /v1/rules`, reset, reload, eval) and the client list; `GET /v1/clients/me` and `POST /v1/clients/me/forget` instead, and `GET /v1/updates` but not `POST /v1/updates/tv`. A phone's `X-App-Version` header (or `app_version` in its stream's first line) is kept as its app version for the panel |
| port 8769, TLS | the TV app's audio stream protocol (`server.py`), with the phone's token in the first line |

## The history and its traces

`~/.local/share/tv-agent/activity.jsonl` (mode 600), one line per command when `log_transcripts` is on; the panel's
Activity tab shows, exports, imports and erases it, and a double-click opens a command in Decisions as it happened.
Each line has the whole trace (`tracing.py`): the speech to text (a negative time: before the command), the decision's
steps, then every step after it, each with

| Field | |
|---|---|
| `label`, `stage`, `step` | what it was ("Which episode fits", "Passages sent", "Title chosen") |
| `at`, `ms` | when it ended, in ms from when the command began, and how long it took |
| `method` | how: `rule` (its id), `llm` / `d1` / `stt` (the model), `local` (the algorithm), `addon`, `web`, `cache` |
| `confidence`, `threshold` | how sure, against the bar it had to pass (d1's 0.40 for a title, a name match's 90) |
| `verdict`, `why` | what came of it (played, listed, refused, agreed…) and why |
| `detail` | the key of its big part in the command's own file |

and `settings`: the versions of the agent (and its commit), TV app, player, phones and Kodi, and the settings that
change the result (speech to text, decision steps and models, languages, playback). Never a secret, a key or a
debrid link. The big parts (the plots it read, subtitle passages, each model's prompt and answer, the streams
offered) are in `~/.local/share/tv-agent/traces/<t>.json.gz` (mode 600), named by the line's `details`; the newest
500 are kept, Erase removes them, an exported history carries them (`details_data`) and an import puts them back.

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
  For the pictures of a held ←/→ the agent reads the file itself (range requests from this computer) and gives
  ffmpeg the keyframe on a pipe: the link isn't on its command line.
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
| `src/tv_agent/decide/chain.py`, `chain.py` | Chains: reading one (rules, the LLM's checked proposal), running one (plan, proof of each step, stop, cancel) |
| `src/tv_agent/tracing.py` | Each command's whole trace: its steps, timed and labelled, and its big parts |
| `src/tv_agent/finding.py` | Finding an episode or a scene from a description: plots (TVmaze, Wikipedia), subtitles kept, ranking, the LLM's answers compared |
| `src/tv_agent/intros.py`, `skips.py` | Intros, recaps and credits: files' chapters and release groups' votes, sound fingerprints, the season's intro, kept; the timestamp databases |
| `src/tv_agent/tv/` | `adb.py` (the only shell path), `apps.py`, `media.py`, `kodi.py`, `justplayer.py` |
| `src/tv_agent/stremio/` | `account.py` (API, link login), `cinemeta.py`, `streams.py`, `subtitles.py`, `library.py`, `progress.py` |
| `src/tv_agent/languages.py` | Audio/subtitle rule and track matching |
| `src/tv_agent/config.py`, `redact.py`, `state.py`, `activity.py` | Settings and secrets, log redaction, memory between commands, the history file |
| `src/tv_agent/mcp_server.py` | MCP tools for Claude Code (`tv-agent-mcp`) |
| `systemd/` | User services for a packaged install (`tv-agent setup services` writes them for a checkout) |
| `src/tv_agent/voicetests.py` | Voice tests: labels for kept recordings, seeded from the history; comparing runs; a command kept as tests |
| `src/tv_agent/quality.py` | The Quality numbers from the history's traces; the nightly check against its baselines |
| `scripts/` | `fetch-models.sh`; `bench_voice.py` (your voice tests), `bench_fallbacks.py` (decision paths and models on `eval/`), `bench_chains.py` (chained commands), `bench_finding.py` (episodes and scenes from a description, timed), `bench_starts.py` (starts up to the link, timed, nothing on the TV), Phase 0 benchmarks |
| `eval/` | `utterances.jsonl` (107 labelled sentences, the panel's tests), `commands.jsonl` (654, made by `build_commands.py`: every action, titles made of command words, numbers in every form, fillers, mishearings, non-commands), `chains.jsonl` (chained commands, ones that aren't, ones refused), `finding.jsonl` (episodes and scenes described, with the right answers), `starts.jsonl` (films and episodes to start, for `bench_starts.py`) |
| `tests/` | Offline unit tests |
