# tv-agent

Voice control for an Android / Google TV, decided on your own computer. Press the remote's mic button and say what
you want: "play The Bear season 3 episode 2", "go to the middle", "lofi hip hop on YouTube", "volume twenty". A
computer on the same network turns the speech into text, decides what you meant with fixed rules and Liquid AI's
**d1-3B** decision model, and drives the TV over ADB: apps, deep links, playback, volume, and films and series from
your **Stremio** account and addons played straight into Just Player or Kodi, without opening Stremio.

```
Remote mic button ─► TV app ("Listening…", records until you stop talking)
      │ 16 kHz audio over the LAN, with a token
      ▼
This computer: tv-agent daemon
   speech-to-text   Parakeet TDT 0.6B v2 (CPU, ~0.1-0.2 s)
   decide           clean-up → fixed phrases → pattern rules → d1-3B (GPU, ~0.65 s, ~4% of commands) → one typed action
   do it            ADB: apps, deep links, media keys, volume
                    Stremio account + Cinemeta + your addons → best cached debrid link → Just Player or Kodi
   afterwards       the player's position → your Stremio library (Continue Watching)
```

Built and measured on a TCL 65Q6C (Google TV, Android 14) with a Ryzen 7 8845HS / Radeon 780M laptop running
CachyOS (KDE Plasma). Anything it doesn't understand does nothing ("Didn't get that").

## Components

| Folder | What it is | Runs on | Release tags |
|---|---|---|---|
| [`server/`](server/README.md) | The daemon (`tv-agent serve`), the CLI, the decision rules and the MCP server for Claude | Linux (systemd user services) | `server-vX.Y.Z` |
| [`panel/`](panel/README.md) | Desktop app with a tray icon: status, services, resource use, activity, decision rules, settings, laptop mic | Linux (Qt 6 / PySide6) | `panel-vX.Y.Z` |
| [`android/`](android/README.md) | The TV app the mic button opens (records, sends, shows the reply) | Android TV 14 (API 34) | `android-vX.Y.Z` |
| [`packaging/`](packaging/README.md) | Package recipes (AUR drafts for now) | | |
| `docs/` | [Phase 0](docs/phase0/README.md) measurements (mic path, models); [research and decisions, 2026-10-22](docs/research-2026-10-22.md) (players, decision model, speech-to-text, add-on data, roadmap) | | |

They talk over one HTTP API (the server's), so each can be released on its own. Versions and tags:
[RELEASING.md](RELEASING.md); what changed: [CHANGELOG.md](CHANGELOG.md).

## Quick start (from a checkout)

```bash
cd server
scripts/fetch-models.sh                    # llama.cpp (Vulkan) + d1-3B + Parakeet, SHA-256 checked (~2.2 GB)
uv sync --extra mcp --group dev            # Python 3.13 environment in server/.venv
.venv/bin/tv-agent setup token             # the token the TV app sends
.venv/bin/tv-agent setup stremio           # prints a link; approve it where you're signed in to Stremio
.venv/bin/tv-agent setup kodi              # optional: Kodi's web password (Kodi > Settings > Services > Control)
.venv/bin/tv-agent setup services --enable # the llama-d1 and tv-agent user services, started now and at login
cd ../android && ./build-and-install.sh    # build the TV app, install it, grant the mic, pair it
cd ../panel && ./install.sh --desktop      # the desktop panel in the menu and on the Desktop
../server/.venv/bin/tv-agent doctor        # everything "ok"
```

Then map the remote's mic button (`KEYCODE_SEARCH`, 84) to `com.boujuan.tvagent` in your launcher; with the
[cleanyourtv](https://github.com/Gathin23/cleanyourtv) launcher that's `"84:com.boujuan.tvagent"` in the theme's
`app_keys`. Configuration lives in `~/.config/tv-agent/` (`config.toml`, `secrets.toml` mode 600, `rules.json`).

## License

[MIT](LICENSE).
