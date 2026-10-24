# tv-agent panel

Desktop app for [tv-agent](../README.md) with a system-tray icon: is voice control on, what's playing, both
services with live CPU / memory / GPU graphs, the history of commands, the decision rules as an editable diagram,
the agent's settings, and the computer's microphone as a second remote. Native Qt (PySide6 and psutil from your
distribution, follows the desktop theme), no network listener of its own, ~125 MB and <1% of a core while open.

## Install (from a checkout)

```bash
./install.sh --desktop        # menu entry, and a Desktop shortcut
```

Needs Python 3 with PySide6 (Qt 6, including QtNetwork) and psutil, `pw-record` (PipeWire) for the microphone,
`qrencode` for the QR code that pairs a phone (optional: the code can be typed instead),
`systemctl --user` for the services, and the tv-agent server running on this computer (it reads
`~/.config/tv-agent/` and talks to `127.0.0.1:8766`). It uses the installed `tv_agent` Python package if there is
one, else `../server/src`. Run it directly with `python3 tv_agent_panel.py` (`--tray` starts hidden).

## What's in it

- **Tray:** closing the window keeps it in the tray. Hover: on/off, what's playing, memory and GPU use. Left click
  shows/hides the window, middle click starts a voice command. Right click: Open, Speak a command, Pause/Resume
  the TV, which player films and series use, Start/Stop voice control, **Quit**. A second launch brings up the
  running one.
- **Beside the tabs** (on every tab): **Save** / **Revert** for what Settings and Decisions have unsaved (a ● on
  their tab), **Run checks** (tv-agent doctor and the rules on the labelled sentences), **Files** (export / import
  everything, open the settings file, the rules or the folder, reset the rules) and **ⓘ About** (versions here,
  running, on the TV and on GitHub, with checksums; the pinned downloads; the models in use). Messages from every
  tab show in the status bar.
- **Overview:** is voice control on, the TV, what's playing (title, position, player); the **TV** card: model,
  Android, uptime and screen state, Wi-Fi or Ethernet with signal and link speed, the laptop's link and ping, a
  speed test, the TV's CPU and memory, the busiest processes and the app in front, the TV app's version against
  this copy's (Install / Update / Uninstall / Pair again), **Diagnose** (each ADB step with what to do) and
  Reconnect, shortcut keys and a screenshot, the agent's stream link (open or copy) and a field to send any video
  link to the TV; both services with Start/Stop/Restart, Start at boot, live CPU, memory and (for the model) GPU
  memory and GPU busy; every process; Start all / Stop all · free resources; a warning if the rules have an
  error.
- **Activity:** give a command typed or **spoken into the computer's microphone** (stops when you stop talking;
  press again to send at once); the history of everything said from the remote, the computer, typed or via
  Claude, with how it was decided and what happened; **Export** (JSON lines or CSV), **Import** (merges, skipping
  duplicates), **Erase**; double-click a line to see in Decisions what happened, as recorded (how it was decided,
  then how its title was found and how it started, step by step), or right-click it to decide it again now, or
  say **This was right** or **This was wrong, it should be…** (what it should have done, the words said, the right
  episode or moment; taught unless you untick it): either way it becomes a test, a voice test when its recording is
  kept, else one of your sentences, and an episode or scene case when it searched for one (for
  `server/scripts/bench_voice.py`, `bench_chains.py`, `bench_finding.py`; also in Decisions, beside Copy as JSON). With
  **Keep recordings** on, right-click a spoken command (🔊) to play it or transcribe it again with another model. **Response times**: a chart of the
  last commands (speech-to-text, deciding, waiting for your pause; doing it) with typical and slowest times by
  step, model and action, and the cloud cost. History lives in `~/.local/share/tv-agent/activity.jsonl`, recordings
  in `recordings/` beside it (mode 600; nothing is written with "Keep a history" off).
- **Quality:** how well the agent does, from the history (today, the last 7 or 30 days, all; from the TV, the
  phones, typed or the laptop's mic): each step's success rate and p50/p95 time (speech to text, deciding, the
  title, the episode, the scene, the streams, the start, the whole command), which rule or model decided and how
  often rightly, each model's calls and cost, the cost per day, the commonest failures (double-click: an example in
  Decisions). The **nightly check** (04:30; **Set up the nightly check** if it isn't): each night's run of the voice
  tests, sentences and chains, episodes and scenes against the baseline, its regressions and fixes, its cost; **Run
  it now**; **Accept as the baseline** after a change made on purpose. A regression notifies you once.
- **Commands:** everything you can say, as patterns with ‹placeholders›, by section, in English or Spanish, with a
  filter; **try** fills one in with an example you can edit, then explains it (nothing sent) or does it on the TV.
- **Decisions:** how a command is decided, as a diagram whose decision steps (fixed phrases, pattern rules, the
  LLM, the decision model) you drag into another order (or ▲▼) and switch on or off, live at once; drag the
  divider up to give the editors room, or fold the diagram to its header (▲ at its top right) or give it the whole
  height (▼; click it again for both). Type a sentence to
  light up its path and read why (which phrase or rule fit, which parts were read, the LLM's answer, d1's top three
  with confidences and where it answered from, two-command sentences); for a title, a row under the steps shows how
  it's found, as a start would find it (your library, the title search and its candidates, the LLM's sound-alikes,
  the pick and by what: d1 with its confidence, drawn from its card, or popularity), and for a command from the
  Activity tab how it started too (streams, link check, the player, the load, the first frame), each with its
  time, and under it **the whole trace**: every step at its time, how it was done (rule, model, local, add-on…),
  its confidence against its bar and its verdict, **details** for the plots, subtitle passages and model prompts
  and answers it used, **Settings at the time**, **Copy as JSON**; **Add as fixed phrase**; editors in the
  order a command goes through them: **speech to text** (this laptop, hosted, or either first; the local and
  hosted models; the vocabulary the hosted model is sent, all of it in **Show all…**: your names to find, add or
  remove, then Twitch favourites and your library's titles; keep recordings for debugging), clean-up, fixed
  phrases, pattern rules ("How a pattern rule
  works…"), the LLM (model, reasoning, private providers), the **decision model** (where it runs and offers to
  start or stop the local server to match, a local GGUF file and a hosted model, question, threshold, what happens
  when no model answers, title choice, intents) and apps & hooks; each tab shows whether the key it needs is set,
  with a link to it in Settings. Rule edits are saved with Save (live at once).
- **Settings:** player (and whether it's installed on the TV), resolution, bitrate (or no limit), link check, languages (audio, or the original
  language; when subtitles show; subtitles as an ordered list, including Chinese Simplified 简体 / Traditional 繁體), binge mode, auto skip, the Liquid and OpenRouter keys,
  Twitch favourites, history on/off, TV addresses, and advanced addresses, ports, the Kodi login and pairing the TV
  app again (Save restarts the agent; what's playing carries on); and the panel's own behaviour: close to tray, open hidden in the
  tray, details on hover, notifications, start at login. Panel settings live in `~/.config/tv-agent/panel.conf`.
  **Updates:** a row per part (the voice agent, the panel, the TV app, TV Agent Player, the phones) with what runs
  against the latest GitHub release, and a button for what to do now: *Update to X* (this computer's copy is
  fast-forwarded to the release tag, never over uncommitted or unpushed work; the TV's apps over ADB), *Restart to
  use X* when the copy on disk is newer than the voice agent or the panel running, or a greyed *Up to date*. Updates
  install by themselves, are checked and told, or aren't checked; an update of this computer runs `uv sync` and
  restarts the agent. A test build on the TV newer than the latest release is said so and never replaced by it;
  each phone updates itself.
  **Phones:** the phones paired with the agent ([TV Agent phone app](https://github.com/boujuan/tv-agent-phone)),
  their app version (and whether a newer one is out), when each was last seen and from where, rename and revoke (at once); **Pair a phone…** shows a QR code and a
  one-time code (5 minutes) with the certificate fingerprint the app shows when the code is typed in.

## Files

| Path | What it is |
|---|---|
| `tv_agent_panel.py` | Entry point |
| `tvpanel/app.py` | Window, tray icon, single instance, resource sampling |
| `tvpanel/overview.py`, `activity_tab.py`, `quality_tab.py`, `commands_tab.py`, `decisions.py`, `settings_tab.py` | The six tabs |
| `tvpanel/tuning.py` | "This was right / wrong, it should be…" for a command of the history: taught, kept as a test |
| `tvpanel/tv_card.py`, `speech_tab.py`, `about.py` | Overview's TV card; Decisions › Speech to text; About |
| `tvpanel/timeline.py` | A recorded command's whole trace in Decisions (stdlib only: tested with the server's tests) |
| `tvpanel/phones.py` | Settings › Phones: paired phones, pairing with a QR code |
| `tvpanel/updates.py` | Settings › Updates: GitHub releases, updating the TV app and this checkout |
| `tvpanel/mic.py` | The computer's microphone (`pw-record`, end-of-speech detection) |
| `tvpanel/common.py` | systemd, the agent's API, small widgets, preferences |
| `tv-agent-panel.desktop.in`, `install.sh`, `tv-agent.svg` | Desktop entry template, installer, icon |
