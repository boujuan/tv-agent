# tv-agent TV app

The app on the TV (`com.boujuan.tvagent`, ~28 KB, plain Java, no libraries) that the remote's mic button opens.
It shows a small "Listening…" pill and streams the audio to the [tv-agent server](../server/README.md) while you
speak; the server shows what it has understood so far, decides during your pause and tells the app to stop. The
pill shows what it heard and what it will do, and closes. With an older server (or if the connection drops) it
records until 0.7 s of quiet and sends it all at once. Nothing runs between
presses: no service, no listener.

## Build and install

```bash
./build-and-install.sh            # build, then `tv-agent tv install`: push, microphone permission, TCL auto-start, pairing
./build-and-install.sh --build    # only build: app/build/outputs/apk/release/app-release.apk
```

Needs JDK 17 and the Android SDK (`JAVA_HOME`, `ANDROID_HOME`; defaults `~/.local/share/jdk-17`, `~/Android/Sdk`)
and `tv-agent` on PATH or in `../server/.venv`. To install a released APK instead:
`tv-agent tv install tv-agent-android-<version>.apk`.

## Pairing

The APK holds no address and no secret. `tv-agent tv pair` (run by `tv install`) sends this computer's agent
address and its token to the app's `ConfigReceiver` over ADB; the app keeps them in its private settings. Pair
again after `tv-agent setup token --rotate` or when the computer's address changes. Unpaired, the pill says
"Not paired with tv-agent".

## The mic button

Map `KEYCODE_SEARCH` (84, the remote's mic button on Google TV remotes) to the app in your launcher. With the
[cleanyourtv](https://github.com/Gathin23/cleanyourtv) launcher: add `"84:com.boujuan.tvagent"` to the theme's
`app_keys` and the package to `hidden_apps`. The button is a single press (press, release, then speak); on that
press the TV opens the remote's own microphone and hands it to the app.

Settings > Privacy > Microphone must be on (only the app records, and only while the pill is on screen).

## Inside

| Class | What it does |
|---|---|
| `ListenActivity` | The pill: streams the microphone (16 kHz, 20 ms chunks) and stops when the agent has decided; 10 s cap; falls back to recording until 0.7 s of quiet and uploading |
| `Pairing`, `ConfigReceiver` | Where the agent is, its token and its audio-stream port; set over ADB only (`DUMP` permission) |
| `ChoicesActivity` | The agent's numbered lists ("what was I watching?", "what's new?") with posters; "number 2", OK or a number key picks (`POST /v1/choose`); closes when playback covers it, on BACK or after 90 s; ADB only. In help mode ("what can you do?") every command as a pattern with its ‹placeholders› in blue, beside what it does, by section, in the language you asked in |
| `SayReceiver` | Shows the agent's messages ("Playing The Bear · S3E2 · 4K") as a toast; ADB only |
| `ProbeActivity` | Phase 0 microphone probe (which input a normal app gets); ADB only |

Cleartext HTTP is allowed because the agent's address is only known at pairing time; the app talks to that one
address, with a bearer token.

## Signing

Releases are signed with the project's release key, whose certificate has the SHA-256 digest
`bd934e07d6784766bbfb3948bf3467ef9b2cb0c9c1146fdf9528dc76c8eb1e08` (check a download with
`apksigner verify --print-certs tv-agent-android-<version>.apk`), so each release installs over the last.

A local build uses the same key when `~/.gradle/gradle.properties` has it, otherwise the computer's debug key;
`build-and-install.sh` prints which. An install signed with one key can't be updated by an APK signed with another:
uninstall first (`tv-agent tv shell pm uninstall com.boujuan.tvagent`), then install, which pairs again. Setting up
the key: [RELEASING.md](../RELEASING.md#signing-android).
