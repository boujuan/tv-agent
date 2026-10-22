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
  duplicates), **Erase**; double-click a line to see it in Decisions; with **Keep recordings** on, right-click a
  spoken command (🔊) to play it or transcribe it again with another model. **Response times**: a chart of the
  last commands (speech-to-text, deciding, waiting for your pause; doing it) with typical and slowest times by
  step, model and action, and the cloud cost. History lives in `~/.local/share/tv-agent/activity.jsonl`, recordings
  in `recordings/` beside it (mode 600; nothing is written with "Keep a history" off).
- **Commands:** everything you can say, as patterns with ‹placeholders›, by section, in English or Spanish, with a
  filter; **try** fills one in with an example you can edit, then explains it (nothing sent) or does it on the TV.
- **Decisions:** how a command is decided, as a diagram whose decision steps (fixed phrases, pattern rules, the
  LLM, the decision model) you drag into another order (or ▲▼) and switch on or off, live at once; drag the
  divider up to give the editors room. Type a sentence to
  light up its path and read why (which phrase or rule fit, which parts were read, the LLM's answer, d1's top three
  with confidences and where it answered from, two-command sentences); **Add as fixed phrase**; editors in the
  order a command goes through them: **speech to text** (this laptop, hosted, or either first; the local and
  hosted models; keep recordings for debugging), clean-up, fixed phrases, pattern rules ("How a pattern rule
  works…"), the LLM (model, reasoning, private providers), the **decision model** (where it runs and offers to
  start or stop the local server to match, a local GGUF file and a hosted model, question, threshold, what happens
  when no model answers, title choice, intents) and apps & hooks; each tab shows whether the key it needs is set,
  with a link to it in Settings. Rule edits are saved with Save (live at once).
- **Settings:** player (and whether it's installed on the TV), resolution, bitrate (or no limit), link check, languages (subtitles as an ordered list,
  including Chinese Simplified 简体 / Traditional 繁體), binge mode, auto skip, the Liquid and OpenRouter keys,
  Twitch favourites, history on/off, TV addresses, and advanced addresses, ports, the Kodi login and pairing the TV
  app again (Save restarts the agent; what's playing carries on); and the panel's own behaviour: close to tray, open hidden in the
  tray, details on hover, notifications, start at login. Panel settings live in `~/.config/tv-agent/panel.conf`.

## Files

| Path | What it is |
|---|---|
| `tv_agent_panel.py` | Entry point |
| `tvpanel/app.py` | Window, tray icon, single instance, resource sampling |
| `tvpanel/overview.py`, `activity_tab.py`, `commands_tab.py`, `decisions.py`, `settings_tab.py` | The five tabs |
| `tvpanel/tv_card.py`, `speech_tab.py`, `about.py` | Overview's TV card; Decisions › Speech to text; About |
| `tvpanel/mic.py` | The computer's microphone (`pw-record`, end-of-speech detection) |
| `tvpanel/common.py` | systemd, the agent's API, small widgets, preferences |
| `tv-agent-panel.desktop.in`, `install.sh`, `tv-agent.svg` | Desktop entry template, installer, icon |
