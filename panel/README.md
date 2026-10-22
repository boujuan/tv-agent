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
- **Overview:** is voice control on, the TV, what's playing (title, position, player); both services with
  Start/Stop/Restart, Start at boot, live CPU, memory and (for the model) GPU memory and GPU busy; every process;
  Start all / Stop all · free resources; a warning if the rules have an error.
- **Activity:** give a command typed or **spoken into the computer's microphone** (stops when you stop talking;
  press again to send at once); the history of everything said from the remote, the computer, typed or via
  Claude, with how it was decided and what happened; **Export** (JSON lines or CSV), **Import** (merges, skipping
  duplicates), **Erase**; double-click a line to see it in Decisions. History lives in
  `~/.local/share/tv-agent/activity.jsonl` (mode 600; nothing is written with "Keep a history" off).
- **Commands:** everything you can say, as patterns with ‹placeholders›, by section, in English or Spanish, with a
  filter; **try** fills one in with an example you can edit, then explains it (nothing sent) or does it on the TV.
- **Decisions:** how a command is decided, as a diagram whose decision steps (fixed phrases, pattern rules, the
  LLM, d1) you drag into another order (or ▲▼) and switch on or off, live at once; d1's box picks this laptop's d1,
  Liquid's hosted one or either first, and offers to start or stop the local d1 server to match. Type a sentence to
  light up its path and read why (which phrase or rule fit, which parts were read, the LLM's answer, d1's top three
  with confidences and where it answered from, two-command sentences); **Add as fixed phrase**; editors for
  clean-up, fixed phrases, pattern rules, the LLM (model, reasoning, private providers), the d1 model (hosted model,
  question, threshold, what happens when no model answers, title question, intents) and apps & hooks; **Run tests**
  (also on unsaved edits), Revert, Reset to defaults, **Save · live now**.
- **Settings:** player, resolution, bitrate (or no limit), link check, languages (subtitles as an ordered list,
  including Chinese Simplified 简体 / Traditional 繁體), speech model, binge mode, auto skip, the Liquid and
  OpenRouter keys, Twitch favourites, history on/off, TV addresses, and advanced addresses, ports and model files
  (saving restarts the agent; what's playing carries on); and the panel's own behaviour: close to tray, open hidden
  in the tray, details on hover, notifications, start at login. Panel settings live in
  `~/.config/tv-agent/panel.conf`.

## Files

| Path | What it is |
|---|---|
| `tv_agent_panel.py` | Entry point |
| `tvpanel/app.py` | Window, tray icon, single instance, resource sampling |
| `tvpanel/overview.py`, `activity_tab.py`, `commands_tab.py`, `decisions.py`, `settings_tab.py` | The five tabs |
| `tvpanel/mic.py` | The computer's microphone (`pw-record`, end-of-speech detection) |
| `tvpanel/common.py` | systemd, the agent's API, small widgets, preferences |
| `tv-agent-panel.desktop.in`, `install.sh`, `tv-agent.svg` | Desktop entry template, installer, icon |
