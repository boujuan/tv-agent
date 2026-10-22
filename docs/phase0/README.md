# Phase 0: is it feasible? (2026-10-22)

Checks made before building, on a TCL 65Q6C (Google TV, Android 14, remote `BT_RC833A_B5`) and a Ryzen 7 8845HS /
Radeon 780M laptop. `probe-log.jsonl` is the TV app probe's output; the two voice recordings it made stay on the
laptop (not in git).

| Gate | Result | Measured |
|---|---|---|
| Mic key | **Pass** | The mic button is HID usage `0x0c0221` (AC Search), `KEY_SEARCH` → `KEYCODE_SEARCH` (84), a single tap (down and up 0.3 ms apart, so no hold-to-talk). The launcher's `app_keys` entry `84:com.boujuan.tvagent` opens the app on a real press. Keys sent with `input keyevent` skip the launcher's key service, so test with the remote. |
| Audio | **Pass, remote mic** | After a mic press the TV opens the remote's microphone and an app recording `VOICE_RECOGNITION` gets it as `remote-submix` (location: peripheral). Remote at the mouth: speech −16.8 dB, background −62.2 dB. At arm's length: −23.9 / −62.8 dB. Started without the button (`am start`), the same app gets the TV's built-in far-field mic instead. |
| Idle cost | **None** | Awake on Home: 4.0% of 4 cores with Microphone access off, 2.4% with it on (no listener runs; the Assistant stays disabled). |
| Decision | **210 ms** | d1-3B Q4_K_M on the 780M (Vulkan, llama.cpp b11804): one intent question with short option texts, 134 tokens, p50 210 ms, p95 224 ms, 23/25 right (both misses below 0.5 confidence and caught by cue-word rules). Longer prompts: 16/16 at 317 ms (one question) and 630 ms (three). Its prompt cache doesn't shorten repeats. |
| Speech | **Pass** | Parakeet TDT 0.6B v2 int8 (sherpa-onnx, 4 CPU threads): 211 ms for 5 s of audio (p95 215 ms). |
| Slot model | **Dropped** | LFM2.5-1.2B-Instruct invented seasons, episodes and titles even with per-intent JSON schemas and examples (p50 294 ms). Titles and numbers come from rules plus fuzzy matching against the library and Cinemeta instead. |

Benchmarks: `server/scripts/bench_stt.py`, `server/scripts/bench_decide.py`.
