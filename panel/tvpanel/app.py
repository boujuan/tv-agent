"""The TV Agent panel window, its system-tray icon, and start-up (one instance; --tray starts hidden)."""
import sys
import time

import psutil
from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon, QTabWidget

from .activity_tab import Activity
from .commands_tab import Commands
from .common import (APP_ID, ICON, SERVICES, STYLE, Async, Prefs, api, clock, episode_of, gpu_of, gpu_totals, human,
                     pill, unit_info, GREEN, AMBER)
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
        for key, label in (("just", "Just Player"), ("kodi", "Kodi")):
            a = QAction(label, players, checkable=True)
            a.triggered.connect(lambda _=False, key=key: panel.settings.save({"player": key}))
            group.addAction(a)
            players.addAction(a)
            self.player_acts[key] = a
        self.start_act = menu.addAction("Start voice control", lambda: panel.all_services("start"))
        self.stop_act = menu.addAction("Stop voice control · free resources", lambda: panel.all_services("stop"))
        menu.addSeparator()
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

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
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

    def show_decisions(self, text: str):
        self.bring_up()
        self.tabs.setCurrentWidget(self.decisions)
        if not self.decisions.loaded:
            self.decisions.load()
        self.decisions.explain_text(text)

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
            player = {"just": "Just Player", "kodi": "Kodi"}.get(st.get("configured_player"), "?")
            o.tv.setText(f"TV at {st.get('tv') or '–'} · plays films and series in {player}")
            np, pl = st.get("now_playing"), st.get("player")
            if np and pl:
                where = clock(pl["position_s"]) + (f" / {clock(pl['duration_s'])}" if pl.get("duration_s") else "")
                app = {"just": "Just Player", "kodi": "Kodi"}.get(pl.get("app"), "")
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
