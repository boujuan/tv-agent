"""The TV Agent panel window, its system-tray icon, and start-up (one instance; --tray starts hidden). Beside the
tabs, on every one: Save and Revert for what Settings and Decisions have unsaved, Run checks, Files (export, import,
open, reset) and About; messages from every tab show in the status bar at the bottom."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSystemTrayIcon, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .about import About
from .activity_tab import Activity
from .commands_tab import Commands
from .common import (APP_ID, ICON, PLAYER_NAMES, REPO, SERVICES, STYLE, Async, Prefs, WheelGuard, agent_config, api,
                     clock, episode_of, gpu_of, gpu_totals, human, pill, run_doctor, run_eval, unit_info, GREEN, AMBER,
                     RED)
from .decisions import Decisions
from .mic import Mic
from .overview import Overview
from .settings_tab import Settings

SAMPLE_MS, HIDDEN_SAMPLE_MS = 1500, 5000
CPUS = psutil.cpu_count() or 1
INSTANCE = f"tv-agent-panel-{__import__('os').getuid()}"


class Tray(QSystemTrayIcon):
    def __init__(self, panel: "Panel"):
        super().__init__(QIcon(str(ICON)), panel)
        self.panel = panel
        menu = QMenu()
        self.open_act = menu.addAction("Open TV Agent", panel.toggle_visible)
        self.speak_act = menu.addAction("🎤  Speak a command", panel.mic.start)
        self.pause_act = menu.addAction("Pause the TV", self.pause_resume)
        menu.addSeparator()
        players = menu.addMenu("Films and series play in")
        group = QActionGroup(players)
        self.player_acts = {}
        for key, label in PLAYER_NAMES.items():
            a = QAction(label, players, checkable=True)
            a.triggered.connect(lambda _=False, key=key: panel.settings.save({"player": key}))
            group.addAction(a)
            players.addAction(a)
            self.player_acts[key] = a
        self.start_act = menu.addAction("Start voice control", lambda: panel.all_services("start"))
        self.stop_act = menu.addAction("Stop voice control · free resources", lambda: panel.all_services("stop"))
        menu.addSeparator()
        menu.addAction("About TV Agent", panel.show_about)
        menu.addAction("Quit TV Agent panel", panel.quit_app)
        menu.aboutToShow.connect(self.update_menu)
        self.menu = menu
        self.setContextMenu(menu)
        self.activated.connect(self.on_activated)
        self.setToolTip("TV Agent")

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.panel.toggle_visible()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.panel.mic.start()

    def update_menu(self):
        p = self.panel
        self.open_act.setText("Hide TV Agent" if p.isVisible() else "Open TV Agent")
        agent_on = p.overview.cards["tv-agent"].active
        self.speak_act.setEnabled(agent_on and not p.mic.busy)
        player = (p.state or {}).get("player")
        self.pause_act.setEnabled(agent_on)
        self.pause_act.setText("Resume the TV" if player and not player.get("playing") else "Pause the TV")
        try:
            current = p.settings.player.currentData()
        except RuntimeError:
            current = None
        for k, a in self.player_acts.items():
            a.setChecked(k == current)
        states = [c.active for c in p.overview.cards.values()]
        self.start_act.setVisible(not all(states))
        self.stop_act.setVisible(any(states))

    def pause_resume(self):
        player = (self.panel.state or {}).get("player")
        op = "resume" if player and not player.get("playing") else "pause"
        self.panel.bg.run(lambda: api("/v1/action", {"type": "transport", "op": op}, timeout=10),
                          lambda r, e: self.panel.notify("TV Agent", r["say"] if r else "The voice agent isn't answering"))


class Panel(QMainWindow):
    def __init__(self, prefs: Prefs):
        super().__init__()
        self.prefs = prefs
        self.setWindowTitle("TV Agent")
        self.setWindowIcon(QIcon(str(ICON)))
        self.resize(920, 860)
        self.bg = Async(self)
        self.mic = Mic()
        self.procs: dict[int, psutil.Process] = {}
        self.gpu_prev: dict[int, tuple[int, float]] = {}
        self.units: dict[str, dict] = {}
        self.state: dict | None = None
        self.usage: dict[str, tuple] = {}
        self._state_busy = False
        self._told_tray = False

        self.message = QLabel("")                 # the status bar: what the tabs have to say
        self.message.setTextFormat(Qt.TextFormat.RichText)
        self.statusBar().addWidget(self.message, 1)
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(lambda: self.message.setText(""))

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setCornerWidget(self.make_toolbar(), Qt.Corner.TopRightCorner)
        self.overview = Overview(self)
        self.activity = Activity(self)
        self.commands = Commands(self)
        self.decisions = Decisions(self)
        self.settings = Settings(self)
        for w, name in ((self.overview, "Overview"), (self.activity, "Activity"), (self.commands, "Commands"),
                        (self.decisions, "Decisions"), (self.settings, "Settings")):
            self.tabs.addTab(w, name)
        self.tabs.currentChanged.connect(self.tab_changed)
        self.setCentralWidget(self.tabs)
        self.dirty_changed()

        self.tray = Tray(self) if QSystemTrayIcon.isSystemTrayAvailable() else None
        if self.tray:
            self.tray.show()
        self.mic.result.connect(self.mic_result)

        self.sample_timer = QTimer(self)
        self.sample_timer.timeout.connect(self.sample)
        self.sample_timer.start(SAMPLE_MS)
        for interval, fn in ((3000, self.refresh_units), (4000, self.refresh_state)):
            t = QTimer(self)
            t.timeout.connect(fn)
            t.start(interval)
        self.refresh_units()
        QTimer.singleShot(500, self.refresh_state)
        updates = QTimer(self)                  # new releases (Settings › Updates): soon after start, then every 6 h
        updates.timeout.connect(lambda: self.settings.updates.check(quiet=True))
        updates.start(6 * 3600 * 1000)
        QTimer.singleShot(20000, lambda: self.settings.updates.check(quiet=True))

    # ---- beside the tabs: save, revert, checks, files, about ----

    def make_toolbar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 2, 8, 2)
        lay.setSpacing(6)
        self.unsaved = QLabel("")
        self.unsaved.setTextFormat(Qt.TextFormat.RichText)
        self.revert_btn = QPushButton("Revert")
        self.revert_btn.setToolTip("Throw away the unsaved changes in Settings and Decisions")
        self.revert_btn.clicked.connect(self.revert_all)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.save_all)
        checks = QPushButton("Run checks")
        checks.setToolTip("The voice agent, the TV and its app, the models (tv-agent doctor), and the rules as "
                          "edited on the labelled sentences")
        checks.clicked.connect(self.run_checks)
        files = QPushButton("Files")
        menu = QMenu(files)
        menu.addAction("Export all settings…", lambda: self.settings.export_all())
        menu.addAction("Import all settings…", lambda: self.settings.import_all())
        menu.addSeparator()
        menu.addAction("Open the settings file (config.toml)",
                       lambda: subprocess.Popen(["xdg-open", str(agent_config.CONFIG_FILE)]))
        menu.addAction("Open the rules file", self.open_rules)
        menu.addAction("Open the settings folder", lambda: subprocess.Popen(["xdg-open",
                                                                            str(agent_config.CONFIG_DIR)]))
        menu.addSeparator()
        menu.addAction("Reset the rules to the built-in ones…", lambda: self.decisions.reset())
        files.setMenu(menu)
        about = QToolButton()
        about.setText("ⓘ")
        about.setToolTip("About TV Agent: versions, checksums, GitHub")
        about.clicked.connect(self.show_about)
        for w in (self.unsaved, self.revert_btn, self.save_btn, checks, files, about):
            lay.addWidget(w)
        return bar

    def say(self, text: str, ms: int = 10000):
        """A message in the status bar, for a while."""
        self.message.setText(text)
        if text:
            self._message_timer.start(ms)

    def dirty_changed(self):
        """Settings or Decisions got (or lost) unsaved changes: the Save button, and a dot on their tabs."""
        if not hasattr(self, "settings"):
            return
        parts = [name for name, w in (("Settings", self.settings), ("Decisions", self.decisions)) if w.dirty]
        self.unsaved.setText(f"<span style='color:{AMBER}'>● unsaved: {', '.join(parts)}</span>" if parts else "")
        self.save_btn.setEnabled(bool(parts))
        self.revert_btn.setEnabled(bool(parts))
        self.save_btn.setToolTip("Rules save live; settings restart the voice agent (what's playing carries on)"
                                 if parts else "Nothing to save: the speech, model and step choices save at once")
        for w, name in ((self.settings, "Settings"), (self.decisions, "Decisions")):
            self.tabs.setTabText(self.tabs.indexOf(w), f"{name} ●" if w.dirty else name)

    def save_all(self):
        if self.decisions.dirty:
            self.decisions.save()
        if self.settings.dirty:
            self.settings.save()

    def revert_all(self):
        if self.settings.dirty:
            self.settings.load()
        if self.decisions.dirty:
            self.decisions.load()
        self.say("Unsaved changes thrown away")

    def run_checks(self):
        """tv-agent doctor and the rules (as edited) on the labelled sentences, in one report. With the voice agent
        stopped, the saved rules are tested here instead (`tv-agent eval`)."""
        agent_on = self.overview.cards["tv-agent"].active
        if agent_on:
            try:
                tests = self.decisions.tests_job()
            except ValueError as e:
                QMessageBox.warning(self, "TV Agent", str(e))
                return
        else:
            tests = run_eval
        self.say("Running the checks…", 120000)
        got: dict = {}

        def report():
            if len(got) < 2:
                return
            res, err = got["tests"]
            if isinstance(res, str):                     # tested here: the voice agent is stopped
                m = re.search(r"full action (\d+)/(\d+)", res)
                bad = [line[4:] for line in res.splitlines() if line.startswith("BAD ")]
                tests_text = ((f"{m[1]} of {m[2]} right" if m else "no result") + " (the saved rules, tested here: the "
                              "voice agent is stopped" + ("; your unsaved edits need it running" if
                                                           self.decisions.dirty else "") + ")"
                              + "".join(f"\n✗ {b}" for b in bad))
                summary = (f"<span style='color:{GREEN if m and m[1] == m[2] else AMBER}'>rules "
                           f"{m[1]}/{m[2]}</span>" if m else "rules not tested")
            elif err or not res:
                tests_text = f"Couldn't run them: {err}"
                summary = f"<span style='color:{RED}'>rules not tested</span>"
            else:
                fails = [r for r in res["results"] if not r["ok"]]
                tests_text = f"{res['full_ok']} of {res['n']} right" + ("" if not self.decisions.dirty else
                                                                          " (the unsaved rules)")
                tests_text += "".join(f"\n✗ “{r['text']}”\n    expected {json.dumps(r['expect'])}\n    got      "
                                      f"{json.dumps(r['got'])} ({r['via']} {r['rule']})" for r in fails)
                summary = (f"<span style='color:{GREEN if not fails else AMBER}'>rules {res['full_ok']}/{res['n']}"
                           "</span>")
            self.say(f"Checks done · {summary}")
            dlg = QDialog(self)
            dlg.setWindowTitle("TV Agent checks")
            v = QVBoxLayout(dlg)
            box = QPlainTextEdit(f"{got['doctor']}\n\nThe rules on the labelled sentences: {tests_text}")
            box.setReadOnly(True)
            box.setMinimumSize(760, 420)
            v.addWidget(box)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(dlg.reject)
            v.addWidget(bb)
            dlg.exec()

        self.bg.run(run_doctor, lambda out, err: (got.__setitem__("doctor", out or str(err)), report()))
        self.bg.run(tests, lambda res, err: (got.__setitem__("tests", (res, err)), report()))

    def open_rules(self):
        """Your rules.json; on the built-in rules, a read-only look at them or your own copy to edit."""
        path = agent_config.CONFIG_DIR / "rules.json"
        if path.exists():
            subprocess.Popen(["xdg-open", str(path)])
            return
        defaults = (self.decisions.server or {}).get("defaults")
        if not defaults:                       # the voice agent is stopped: the built-in rules from this copy
            try:
                defaults = json.loads((REPO / "server/src/tv_agent/decide/default_rules.json").read_text())
            except (OSError, ValueError) as e:
                self.say(f"Can't read the built-in rules: {e}")
                return
        box = QMessageBox(QMessageBox.Icon.Question, "TV Agent", "You're on the built-in rules, which improve with "
                          "each update. Edit them in the Decisions tab, or open them here as a file:", parent=self)
        look = box.addButton("Open them read-only", QMessageBox.ButtonRole.AcceptRole)
        copy = box.addButton("Make my own rules.json", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setInformativeText("Your own rules.json stops following the built-in ones until you reset it.")
        box.exec()
        text = json.dumps(defaults, indent=2, ensure_ascii=False) + "\n"
        if box.clickedButton() is look:
            view = Path.home() / ".cache/tv-agent/rules (built-in, read-only).json"
            view.parent.mkdir(parents=True, exist_ok=True)
            view.unlink(missing_ok=True)
            view.write_text(text)
            view.chmod(0o444)
            subprocess.Popen(["xdg-open", str(view)])
        elif box.clickedButton() is copy:
            path.write_text(text)                 # the agent picks it up by itself (it watches the file)
            subprocess.Popen(["xdg-open", str(path)])
            QTimer.singleShot(1500, self.decisions.load)

    def show_about(self):
        self.bring_up()
        About(self).exec()

    # ---- window and tray ----

    def tab_changed(self, i: int):
        w = self.tabs.widget(i)
        if w is self.activity:
            self.activity.load()
        elif w is self.decisions and not self.decisions.loaded:
            self.decisions.load()

    def toggle_visible(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.bring_up()

    def bring_up(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def showEvent(self, e):
        self.sample_timer.setInterval(SAMPLE_MS)
        super().showEvent(e)

    def closeEvent(self, e):
        if self.tray and self.prefs.get("close_to_tray"):
            e.ignore()
            self.hide()
            self.sample_timer.setInterval(HIDDEN_SAMPLE_MS)
            if not self._told_tray:
                self._told_tray = True
                self.notify("TV Agent is still in the tray", "Right-click its icon to quit. You can change this in "
                            "Settings.")
            return
        self.quit_app()

    def quit_app(self):
        self.mic.cancel()
        if self.tray:
            self.tray.hide()
        QApplication.quit()

    def notify(self, title: str, text: str):
        if self.tray and self.prefs.get("notify"):
            self.tray.showMessage(title, text, QIcon(str(ICON)), 5000)

    def prefs_changed(self):
        self.update_tooltip()

    def show_settings_keys(self):
        """Settings, scrolled to the API keys, which flash for a moment."""
        self.bring_up()
        self.tabs.setCurrentWidget(self.settings)
        self.settings.highlight_keys()

    def show_decisions(self, text: str):
        self.bring_up()
        self.tabs.setCurrentWidget(self.decisions)
        if not self.decisions.loaded:
            self.decisions.load()
        self.decisions.explain_text(text)

    def show_recorded(self, entry: dict):
        """An Activity entry in the Decisions diagram, as it happened (its recorded trace)."""
        self.bring_up()
        self.tabs.setCurrentWidget(self.decisions)
        if not self.decisions.loaded:
            self.decisions.load()
        self.decisions.show_recorded(entry)

    def mic_result(self, r: dict):
        if self.isVisible() and self.tabs.currentWidget() is self.activity:
            return                      # the Activity tab shows it
        if r.get("error"):
            self.notify("TV Agent", r["error"])
        else:
            self.notify(f"“{r.get('heard', '')}”", r.get("say", ""))
        if self.isVisible():
            self.activity.show_result(r)

    # ---- services ----

    def all_services(self, verb: str):
        from .common import local_d1_needed, set_badge, systemctl
        order = ["tv-agent", "llama-d1"] if verb == "stop" else ["llama-d1", "tv-agent"]
        if verb == "start" and not local_d1_needed():
            order = ["tv-agent"]             # the decision steps don't use this laptop's d1 (Decisions tab)
        for unit in order:
            set_badge(self.overview.cards[unit].state, "Stopping…" if verb == "stop" else "Starting…", AMBER)
        self.bg.run(lambda: [systemctl(verb, f"{u}.service") for u in order], lambda *_: self.refresh_units())

    def refresh_units(self):
        def done(units, err):
            if err:
                return
            self.units = units
            for u, info in units.items():
                self.overview.cards[u].show_info(info)
            self.overview.show_headline()
            self.update_tooltip()

        self.bg.run(lambda: {u: unit_info(u) for u, _, _ in SERVICES}, done)

    def refresh_state(self):
        if self._state_busy or not self.overview.cards["tv-agent"].active:
            if not self.overview.cards["tv-agent"].active:
                self.state = None
            return
        self._state_busy = True

        def done(st, err):
            self._state_busy = False
            o = self.overview
            self.state = st if not err else None
            if err or not st:
                o.tv.setText("Voice agent starting or not answering yet…")
                o.playing.setText("")
                return
            player = PLAYER_NAMES.get(st.get("configured_player"), "?")
            o.tv.setText(f"TV at {st.get('tv') or '–'} · plays films and series in {player}")
            np, pl = st.get("now_playing"), st.get("player")
            if np and pl:
                where = clock(pl["position_s"]) + (f" / {clock(pl['duration_s'])}" if pl.get("duration_s") else "")
                app = PLAYER_NAMES.get(pl.get("app"), "")
                o.playing.setText(f"{pill('Playing' if pl.get('playing') else 'Paused', GREEN if pl.get('playing') else AMBER)}"
                                  f"&nbsp; <b>{np.get('name')}{episode_of(np.get('video_id', ''))}</b> "
                                  f"<span style='color:gray'>· {where} · {app}</span>")
            else:
                o.playing.setText("<span style='color:gray'>Nothing playing from the agent</span>")
            rules = st.get("rules") or {}
            o.warning.setVisible(bool(rules.get("error")))
            if rules.get("error"):
                o.warning.setText(f"<span style='color:#e05555'>⚠ Your rules have an error, the defaults are in use: "
                                  f"{rules['error']}</span>")
            self.update_tooltip()

        self.bg.run(lambda: api("/v1/state", timeout=5), done)

    def update_tooltip(self):
        if not self.tray:
            return
        head = self.overview.headline.text() or "Voice control"           # worked out by the Overview tab
        lines = [f"TV Agent · {head}"]
        if self.prefs.get("tray_details"):
            st = self.state or {}
            np, pl = st.get("now_playing"), st.get("player")
            if np and pl:
                lines.append(f"{'▶' if pl.get('playing') else '⏸'} {np.get('name')}{episode_of(np.get('video_id', ''))}"
                             f" · {clock(pl['position_s'])}")
            agent, model = self.usage.get("tv-agent"), self.usage.get("llama-d1")
            if agent:
                lines.append(f"Voice agent: {human(agent[1])} RAM, {agent[0]:.1f} % CPU")
            if model:
                lines.append(f"Decision model: {human(model[2] or 0)} GPU memory")
        self.tray.setToolTip("\n".join(lines))

    # ---- resource sampling (cheap: psutil + /proc) ----

    def sample(self):
        rows = []
        now = time.monotonic()
        for unit, card_w in self.overview.cards.items():
            pids = self.units.get(unit, {}).get("pids", []) if card_w.active else []
            cpu = rss = gpu_mem = 0
            gpu_busy = 0.0
            for pid in pids:
                proc = self.procs.get(pid)
                try:
                    if proc is None:
                        proc = self.procs[pid] = psutil.Process(pid)
                        proc.cpu_percent(None)
                    c = proc.cpu_percent(None) / CPUS
                    m = proc.memory_info().rss
                    name = proc.name()
                except psutil.Error:
                    self.procs.pop(pid, None)
                    continue
                g_mem, g_ns = gpu_of(pid)
                prev = self.gpu_prev.get(pid)
                busy = 100 * (g_ns - prev[0]) / ((now - prev[1]) * 1e9) if prev and now > prev[1] else 0.0
                self.gpu_prev[pid] = (g_ns, now)
                cpu, rss, gpu_mem, gpu_busy = cpu + c, rss + m, gpu_mem + g_mem, gpu_busy + max(0.0, busy)
                rows.append((unit, name, pid, c, m, g_mem))
            if card_w.active:
                self.usage[unit] = (cpu, rss, gpu_mem)
                card_w.show_usage(cpu, rss, gpu_mem if unit == "llama-d1" else None, min(gpu_busy, 100.0))
            else:
                self.usage.pop(unit, None)
        if self.overview.cards["tv-agent"].active:      # the TV's own apps and players, from the TV card's last check
            for a in (getattr(self.overview.tv_card, "info", None) or {}).get("apps") or []:
                rows.append(("On the TV", a["name"], a["pid"], a["cpu"], a["rss_kb"] * 1024, None))
        if self.isVisible():
            self.overview.show_rows(rows)
            vm = psutil.virtual_memory()
            used, total = gpu_totals()
            gpu = f" · GPU memory {human(used)} of {human(total)}" if total else ""
            self.overview.footer.setText(f"This laptop: CPU {psutil.cpu_percent(None):.0f} % · memory "
                                         f"{human(vm.total - vm.available)} of {human(vm.total)}{gpu}")
        self.update_tooltip()


def main():
    start_hidden = "--tray" in sys.argv
    QApplication.setDesktopFileName(APP_ID)    # Wayland app id: the task bar shows the right icon
    app = QApplication(sys.argv)
    app.setApplicationName("TV Agent")
    app.setWindowIcon(QIcon(str(ICON)))
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)
    guard = WheelGuard(app)
    app.installEventFilter(guard)

    sock = QLocalSocket()                     # already running? bring that one up instead
    sock.connectToServer(INSTANCE)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return
    QLocalServer.removeServer(INSTANCE)
    server = QLocalServer()
    server.listen(INSTANCE)

    prefs = Prefs()
    w = Panel(prefs)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), w.bring_up()))
    if not (start_hidden or prefs.get("start_in_tray")) or not w.tray:
        w.show()
    else:
        w.sample_timer.setInterval(HIDDEN_SAMPLE_MS)
    sys.exit(app.exec())
