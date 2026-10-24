# Player tests on the TV — 2026-10-23

The nine on-TV tests from the research report *Kodi alternatives for Android TV* ("Run nine tests on the TV before
writing code"), run on the living-room TCL 65Q6C with you at the TV, plus one extra check (HDR10+). They decide the
details of the custom Media3 player that is to replace Kodi. Each test gives what was done, what was seen and the
decision it leads to. Anything resting on one file only is marked as such.

## Decisions at a glance

| Question | Answer |
|---|---|
| Media3 version to pin | **1.11.1** (Just Player 0.217). 1.10.1 (0.212) is just as good. 1.12.0-alpha01 (0.219) stalled for ~20 s after seeking forward, in 2 of 2 runs (test 3) |
| HDR10+ stripping (androidx/media PR #3085, issue #1895) | **Not needed.** DV P8.1 + HDR10+ hybrids show Dolby Vision with a correct picture in both Just Player and Kodi (test 2) |
| P7 → P8.1 conversion (ExoplayerHdrUtils) | **Not needed for a DV picture.** The TV's `c2.mtk.dvhe.stn` decoder plays a P7 FEL REMUX in Dolby Vision, correctly and smoothly; Media3 picked it by itself (test 5, one disc) |
| P5 releases | **Keep them.** P5 decodes natively in DV on `c2.mtk.dvhe.stn` (test 5); the screensaver already uses it all day |
| FFmpeg audio | **Not needed** on the TV speakers. EAC3 Atmos, TrueHD Atmos and DTS-HD MA all go as bitstreams to the TV's own decoders and sound right (test 6) |
| Frame-rate switching | **Not needed.** Android has a single 60 Hz mode, and the MediaTek video plane drives the panel at 120 Hz (24p shown 5×) or 100 Hz (25/50p) by itself (test 7) |
| Tunneling | **Off, and it can't be on:** no decoder offers it, and Media3 ignores the request (test 9) |
| Baseline to beat (test 8) | Just Player: first frame 2.9 s after the command (link already resolved), 208 MB PSS while playing 4K DV, nothing resident between plays. Kodi: 2.4 s warm / ~5.8 s from a cold start, 277 MB PSS playing, 130 MB resident when idle |

What it means for the migration plan is under "What this changes" near the end.

## The TV

| | |
|---|---|
| Build | `TCL/G08_4K_GB/G08:14/UTT2.250416.001/AU02`, Android 14, ABIs `armeabi-v7a,armeabi` |
| Chip | `ro.hardware=mt5896`, `ro.board.platform=merak` (Pentonic 700 family); note `ro.soc.model=MT9689`, a name none of the research sources used |
| Memory | 2.45 GB total; 0.8–0.9 GB available when idle |
| Players | Kodi 22.0-RC1 (versionCode 2190901), Just Player 0.217 (Media3 1.11.1, logged as `AndroidXMedia3/1.11.1`), TV Agent app 0.8.0 |
| Audio out | TV speakers only: no eARC device (`hdmi_earc_connected=0`) |

## How it was measured

- **Files**: the agent's own stream lookup (`Agent.fetch_streams` / `streams_for`) and `streams.resolve` on your
  AIOStreams/Premiumize add-on. File facts come from `ffprobe` on the laptop: the DV configuration record and, from
  the first decoded frames, whether HDR10+ metadata is present.
- **Players were started directly**, never through the agent's play path, so no test play reached the Stremio
  library: Just Player with `am start -S -W -a android.intent.action.VIEW -d <link> -t video/* --ei position <ms>`,
  Kodi with JSON-RPC `Player.Open`. Debrid links were never printed or saved; logcat was captured through a `sed`
  that masks every URL.
- **Dolby Vision on or off**: TCL's picture service logs `PictureManager2: initCommonSignalInfo: signalInfo = N`
  (5 = Dolby Vision, 2 = HDR10+, 8 = no HDR signal). It agreed with what you saw every time you looked.
- **Smoothness per playback**: `dumpsys media.metrics` keeps one record per decoder session (frames rendered, dropped
  and skipped, freezes and the longest one, flushes, tunneled). The record is written only when the player exits
  normally: an `am start -S` relaunch kills Just Player first and loses it.
- **First frame on the panel**: `VIDEOFUNC: TclpqStatus [pqPorcCallBackCheck] FirstFrame = 1`. The panel's cadence comes
  from the MediaTek renderer's frame-rate-conversion (FRC) log lines.
- **Your eyes and ears** for the DV badge, the picture, the sound and stutter. You said you trust the stats more than
  your eye for subtle stutter, so test 3 is decided by them.

### Test files

| | File | Video | Audio |
|---|---|---|---|
| A | Dune Part Two Hybrid 2160p iT WEBRip DV HDR10+ (PrimeX), 6.8 GB, 5.5 Mbit/s | HEVC 23.976, DV P8.1 + HDR10+ | EAC3 Atmos 5.1 |
| B | Dune Part Two 2160p WEB-DL DDP5.1 Atmos DV HDR (FLUX), 31.4 GB, 25.3 Mbit/s | HEVC 23.976, DV P8.1 + HDR10+ | EAC3 Atmos 5.1 |
| C | Dune Part Two UHD BluRay REMUX (FraMeSToR), 69 GB, 55.5 Mbit/s | DV **P7 FEL** | TrueHD Atmos 7.1 (default), EAC3 Atmos 5.1 |
| D | Rebel Moon Part One 2160p DV Profile 5 NF WEB-DL (MP4), 26.4 GB, 25.9 Mbit/s | DV **P5** (no HDR10 base) | EAC3 Atmos |
| E | Oppenheimer 2160p MA WEB-DL DTS-HD MA DV-HDR (TheBiscuitMan), 40.5 GB, 29.9 Mbit/s | DV P8.1 | DTS-HD MA 5.1 (default), EAC3 |
| F | The Boys S04E01 2160p AMZN WEB-DL HDR10+ (HONE), 7.0 GB | HEVC HDR10+ | DD+ 5.1 |
| — | Test 4 only: Extraction 2 (FLUX), Glass Onion (MZABI), The Gray Man (APEX), Rebel Moon (DVT P8) | DV WEB-DLs | EAC3 Atmos |

## 1. What the decoders, display and audio output advertise

**Done**: `dumpsys media.player`, `dumpsys display`, `dumpsys media.audio_policy` and `dumpsys media.audio_flinger`, with
nothing playing.

**Seen**
- Dolby Vision decoders: `c2.mtk.dvhe.dtr` (P4), **`c2.mtk.dvhe.stn` (P5)**, `c2.mtk.dvhe.st` (P8), `c2.mtk.dvav.se`
  (P9) and `c2.mtk.dvav1.10` (P10), each up to UHD 60 and 4096×2304, plus secure versions. **None lists P7**
  (`dvhe.dtb`). The research brief's "P8 only" was wrong.
- HEVC (`c2.mtk.hevc`): Main, Main 10, Main 10 HDR10. It lists no HDR10+ profile, yet HDR10+ plays (extra check).
  AV1 lists Main 10 HDR10+.
- **Every decoder reports `feature-tunneled-playback = 0`.**
- Display: **a single mode, 3840×2160 at 60 Hz**, with no alternative refresh rates. HDR types: Dolby Vision, HDR10,
  HLG and HDR10+.
- Audio: one output device, the speakers. Its direct profiles are PCM, AC3, E-AC3, E-AC3 JOC, DTS and DTS-HD; the
  offload output also takes TrueHD, MAT, AC-4 and AAC.
- `dumpsys media.metrics` shows the Aerial Views screensaver playing on `c2.mtk.dvhe.stn` for hours today, so P5 is
  in daily use on this TV.

**Decision**: P5 can decode natively (confirmed in test 5). P7 has no listed decoder, but see test 5. Android
cannot switch refresh rate (test 7). Passthrough candidates are everything Dolby and DTS-HD (test 6).

## 2. DV P8 + HDR10+ hybrid: does androidx/media #1895 hit this TV?

**Done**: file A in Just Player 0.217 from 30:00 for ~10 minutes, with ~25 fast-forward/rewind seeks and an exact jump
to 1:20:00 by relaunch. Then the same file and seeks in Kodi 22 RC1. Kodi's settings were read, not changed:
`allowedhdrformats` [DV, HDR10+] (its default, so Kodi passed HDR10+ to the DV decoder), DV conversion off, refresh
rate adjusted on start/stop, passthrough off. Last, file B (the full-bitrate hybrid) in Just Player.

**Seen**
- Just Player: `c2.mtk.dvhe.st`; **DV badge and a normal picture**, all seeks smooth (you).
- Kodi: same decoder; **DV and a normal picture**, seeks smooth on screen (you). Its log, though, shows the known
  MediaTek flush errors on every seek: `CMediaCodecVideoBuffer::ReleaseOutputBuffer error in render(false)` and
  `OutputPicture - timeout waiting for buffer` (xbmc PR #30059). Stats: 5 dropped frames, 2 freezes (longest 25 ms)
  and 12 flushes in 138 s.
- File B (25 Mbit/s): DV and smooth in Just Player (you).
- The TV's audio server crashed and restarted once (15:49:54), and you heard a short dropout. **My tooling caused
  it**: an accidental full `dumpsys` (which includes the audio dump) during playback. Just Player recovered by itself
  ("Recoverable renderer error"). The agent never runs that dump.

**Decision**: #1895 doesn't occur on this TV for these hybrids. **Don't port PR #3085**, and keep DV for hybrids
rather than preferring HDR10+. Kodi's DV-only setting for hybrids wasn't needed for these two files either.

## 3. Media3 version: Just Player 0.212, 0.217 and 0.219

**Done**: you approved the downloads. `Just.Player.v0.219.apk` (45.1 MB) and `Just.Player.v0.212.apk` (51.2 MB) came
from github.com/moneytoo/Player releases; their SHA-256 matched GitHub's digests and they are signed with the same
certificate as the installed 0.217. 0.219 installed over 0.217. 0.212 needed an uninstall, because Android refuses
downgrades. 0.217 was put back from the APK copied off the TV (identical to GitHub's 0.217). Just Player's settings
were all defaults before the uninstall and are defaults again.

The script was the same for every run: file B from 30:00, 50 s, five fast-forwards a second apart, 20 s, five
rewinds, 25 s, Back. Every run was in DV on `c2.mtk.dvhe.st`.

| Just Player | Media3 | Run | Played | Dropped | Skipped | Freezes (longest) | After the 5 fast-forwards |
|---|---|---|---|---|---|---|---|
| 0.212 | 1.10.1 | 1 | 99 s | 6 | 3 | 1 (83 ms) | kept playing |
| 0.212 | 1.10.1 | 2 | 101 s | 7 | 2 | 1 (216 ms) | kept playing |
| 0.217 | 1.11.1 | 1 | 101 s | 9 | 1 | 2 (224 ms) | kept playing |
| 0.217 | 1.11.1 | 2 | 101 s | 4 | 1 | 0 | kept playing |
| 0.219 | 1.12.0-alpha01 | 1 | 80 s | 60 | 33 | 36 (183 ms) | **BUFFERING ~20 s** |
| 0.219 | 1.12.0-alpha01 | 2 | 75 s | 16 | 9 | 5 (150 ms) | **BUFFERING ~20 s** |

A first, longer 0.217 run (190 s, more seeks) gave 14 dropped, 3 freezes (longest 116 ms), and you saw it as smooth.
On 0.219 you "couldn't really tell". The judder from Just Player #743 (0.216, Media3 1.11.0, on a same-chip C7K) did
not show on 0.217.

**Decision**: **pin Media3 1.11.1.** 1.10.1 is an equal fallback. Before moving to 1.12.x, rerun this script on it:
the stall after seeking forward is a regression for exactly what voice control does ("skip ahead"). Its cause is
unknown; 1.12.0-alpha01 changed how a load is retried after a seek, but that link is *unverified*.

## 4. Kodi's failure modes in Just Player: seek, flush, decoder release

**Done**
- (a) File B at 50:00: 20 rapid alternating ±10 s media-key seeks, ~0.5 s apart, three times; then an exact jump to
  1:40:00 by relaunch.
- (b) Five DV titles back to back, each started with `am start -S` over the previous one (as the agent does), ~25 s each.
- (c) The DV state after every stop and exit.

**Seen**
- (a) Media3 merges back-to-back seeks: 20 key presses gave only 3 decoder flushes. Each burst ended with a short
  BUFFERING (1–4 s), then normal play. The run with stats: 62 s, 0 dropped, 0 freezes. The jump played 3.5 s after the
  command, in DV. You saw nothing wrong: no freeze with sound running, no black picture, no time racing ahead.
- (b) All five started in DV and played with sound (you); DV came on 3.4–8.8 s after the previous title stopped,
  link lookup included. You noticed the home screen flash between some titles. The log shows why: `-S` force-stops
  Just Player, the launcher is resumed for ~1 s until the new Just Player window appears, and whether it gets drawn
  in that gap varies.
- (c) DV went off (signal 8) within half a second of every stop or exit, after all 30 DV starts today. It never stuck.

**Decision**: Media3 avoids Kodi's MediaTek lifecycle bugs on this chip. The custom player should replace the media
item in place (no relaunch, so no home-screen flash and no 0.5–1 s process start) and keep merging rapid seeks.

## 5. P7 REMUX and a P5 file

**Done**: file C with Just Player's defaults, then with "Dolby Vision profile 7 fallback" switched on (it was switched
back off afterwards); then file D.

**Seen**
- **P7 FEL, defaults**: no decoder supports `dvhe.07`, so Media3 fell back to the first DV decoder,
  **`c2.mtk.dvhe.stn`**. The TV switched to Dolby Vision and you said the picture **looks correct**. Stats: 250 s with
  6 seeks, 5 dropped, 3 skipped, 0 freezes, at 55 Mbit/s. Whether the decoder uses the FEL or ignores it is unknown.
- **P7 with the fallback option on**: nothing changed (same decoder, still DV). The option is for screens without DV,
  and this one reports DV.
- **P5**: `c2.mtk.dvhe.stn`, DV, **correct colours** (you). 42 s, 1 dropped, 0 freezes. Slow to start: 22 s to moving
  picture, because this 26 GB MP4 loads a large index over HTTP before the first frame (the MKVs took 3–5 s). A seek
  forward rebuffered ~5 s.
- **Subtitles** (your observation): the REMUX shows English lines during the Chakobsa dialogue with captions off and
  no forced track in the file, so they are burned into the picture. In the WEB-DL (B) the same lines are a separate
  English track flagged *forced + default*, which Just Player hid because captions were off. In that file the Fremen
  dialogue had no subtitles at all.

**Decision**
- **No P7 → P8.1 conversion and no HDR10 fallback needed**; leave ExoplayerHdrUtils out. The custom player should
  send `dvhe.07` to `c2.mtk.dvhe.stn` on purpose (a small `MediaCodecSelector`) rather than rely on the fallback
  order. Confirm with a P7 MEL disc, since only one FEL disc was tested.
- **Don't filter out P5.**
- Subtitle rule for the custom player: with subtitles "off", still show a forced track in the audio language. With
  subtitles "on", don't take a forced track for the full one (Media3 prefers default-flagged tracks, and here the
  forced track is the default).

## 6. Audio: EAC3 Atmos, TrueHD Atmos, DTS-HD MA

**Done**: files B, C and E in Just Player (defaults: device decoders first, FFmpeg available) on the TV speakers.
The output format was read from the audio HAL's own log lines; no audio dump was run during playback after test 2.

| Track | Sent as | TV side | You heard |
|---|---|---|---|
| EAC3 Atmos 5.1 (B) | E-AC3 bitstream (`AudioTrack` format 0xa000000, 6 ch) | offload output, Dolby decoder (`DecoderFormat=Ac3`) with Dolby audio processing | fine |
| TrueHD Atmos 7.1 (C) | **TrueHD bitstream** (0xe000000, 8 ch) | offload output, MAT decoder | fine |
| DTS-HD MA 5.1 (E) | **DTS-HD bitstream** (0xc000000, 6 ch) | offload output, DTS decoder (DTS:X/IMAX filter thread) | fine; the first frame held 2.6 s until the audio started |

The log also shows two DTS variants not supported natively: `audio/vnd.dts.hd;profile=lbr` (DTS Express) and
`audio/vnd.dts.uhd;profile=p2` (DTS:X P2). Kodi has passthrough off and 2.0 channels, so it decodes everything to
stereo PCM itself (not compared by ear).

**Decision**: the passthrough policy is "bitstream what the TV lists": AC3, E-AC3 (including Atmos), TrueHD, DTS and
DTS-HD. Media3's own audio-capability check already does exactly this here. **FFmpeg is not needed for the
speakers.** Add it only if an eARC receiver or DTS:X / DTS Express files turn up. eARC wasn't tested (nothing connected).

## 7. 23.976 fps and frame-rate switching

**Done**: Just Player's "Auto frame rate matching" on (switched back off afterwards), file B, then `dumpsys display`,
`dumpsys SurfaceFlinger` and the MediaTek renderer's log during playback.

**Seen**
- Android stayed at its only mode, 60 Hz. Just Player's request ended as a 60 Hz frame-rate override for the app, so
  there was nothing to switch and no blackout.
- The MediaTek video plane is independent of Android's 60 Hz UI. Its FRC table picks the panel rate from the video's
  own frame rate: **23.976/24 fps → 120 Hz, each frame shown 5×** (`PanelTiming 3840x2160 fps 119880`); 25 → 100 Hz ×4;
  30 → 120 Hz ×4; 48 → 96 Hz ×2; 50 → 100 Hz ×2; 60 → 120 Hz ×2.
- Every playback today chose that 24p mode, in Just Player with matching on or off and in Kodi alike.

**Decision**: **don't port display-mode switching**; leave Media3's default frame-rate strategy. There's no 3:2
judder to fix on this TV, and Kodi's "adjust refresh rate" does nothing useful here either.

## 8. Footprint and start time

**Done**: file B from 30:00 with the link already resolved: `am start -W` and the first-frame time for Just Player;
a cold and a warm start for Kodi; `dumpsys meminfo <package>` and `MemAvailable` 35 s into 4K DV playback.

| | Just Player 0.217 | Kodi 22 RC1 |
|---|---|---|
| Window up (`am start -W` TotalTime) | 0.46–0.70 s (always a cold start: `-S`) | 0.75 s cold, 0.31 s warm |
| Ready for a link | at once (the link is in the intent) | JSON-RPC answers 3.1 s after a cold start |
| Command → first frame | **2.9 s** | **2.4 s** warm (after `Player.Open`); ~5.8 s from a cold Kodi (3.1 + 2.7) |
| Memory while playing 4K DV (PSS / RSS) | 208 / 322 MB | 277 / 385 MB |
| Memory between plays | not running | 130 MB PSS, resident |
| Available memory while playing | 656 MB (908 MB idle before) | 531 MB (734 MB idle before) |

**Decision**: the custom player has to beat ~2.9 s from link to first frame and ~210 MB while playing 4K. Living in
the TV app, it adds nothing when idle beyond the app itself. Its real gains are the ones measured as costs above: no
relaunch per jump or title, no 3 s Kodi cold start, and ~70 MB less than Kodi while playing.

## 9. Tunneling

**Done**: Just Player's "Tunneled playback" on (switched back off afterwards), file C from 1:00:00 with six seeks.

**Seen**: the session ran non-tunneled anyway (metrics `tunneled=0`; audio HAL "MM in non-tunnel mode") and played
normally: 31 s, 4 dropped, 1 freeze of 16 ms.

**Decision**: tunneling stays off and needs no option: no decoder on this TV offers it (test 1), so Media3 never
enables it.

## Extra: an HDR10+-only file

File F in Just Player: `c2.mtk.hevc.decoder`, and the TV reported **HDR10+** (`isHdr10Plus = true`), even though the
HEVC decoder lists no HDR10+ profile; 16 s, 1 dropped. A per-load HDR mode (DV / HDR10+ / HDR10), as the report
proposes, is therefore possible, but nothing today calls for anything other than DV.

## What this changes

Migration step 3 ("Audio and HDR", 2–4 days) shrinks to well under a day:
- Drop FFmpeg in CI, HDR10+ stripping, P7 conversion and HEVC mapping, and display-mode switching.
- Keep two small things: a codec selector that sends DV P7 to `c2.mtk.dvhe.stn`, checked with a P7 MEL disc; and
  logging the chosen decoder and audio format, as was done here.

Step 2 (the minimum viable player) gains three requirements from these tests:
- Replace the media item in place instead of relaunching.
- Keep the forced-subtitle rule above.
- Report the first frame, so the agent stops polling. The TV's own DV/HDR signal (PictureManager2) is a useful
  cross-check in the logs.

## Side findings for the agent (not changed here)

- **AIOStreams error clip slips through.** For Fallout S01E01 "Hybrid.REMUX.DoVi.HDRPlus" (marked cached),
  `streams.resolve` returned a link to a 30 s 1080p H.264 clip whose path ends in `error.mp4` (with a query string,
  on the add-on's own host). `PROBLEM_VIDEO` only matches `/static/…mp4`, so the agent would have played the error clip.
- **The ranking pushes DV REMUXes down** (`not dv_remux` in `streams.rank`, "profile 7 remuxes often fail outside
  dedicated players"). On this TV a P7 REMUX plays in DV in Media3, so the rule can go once the new player is the
  default (Kodi with its DV conversion off was not tested on P7).
- **Never dump audio during playback** in tooling: a full `dumpsys` crashed the audio server once (test 2).
- Just Player's `-S` relaunch is what flashes the home screen between titles (test 4).

## Not covered

- eARC or a receiver (none connected).
- A P7 MEL disc; P7 in Kodi.
- The Media3 demo app as a cleaner check than Just Player's patched ExoPlayer build.
- Kodi's own bugs from today ("InstanceGuard locked" when opening over a playing file; the DV decoder probe hang):
  Kodi was only ever opened over a stopped player here.
- Kodi watched for ~2.5 minutes, not 10 (Just Player ~10).
