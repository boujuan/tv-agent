"""Overview › the TV: model and Android, its network and the laptop's (with a speed test), CPU and memory, the app in
front, the TV app's version against this copy's (install, update, uninstall, pair), ADB checks, shortcut keys, what
the agent plays (its stream link) and sending any link to the TV. Refreshed every few seconds while it's shown."""
import subprocess
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from .common import AMBER, BLUE, GREEN, PURPLE, RED, REPO, TV_AGENT, android_version, api, dim, load_cfg, metric

SHORTCUTS = [("⌂ Home", {"key": "home"}), ("↩ Back", {"key": "back"}), ("⏯", {"key": "play_pause"}),
             ("Vol −", {"key": "volume_down"}), ("Vol +", {"key": "volume_up"}), ("Mute", {"key": "mute"}),
             ("Wake", {"key": "wake"}), ("Screen off", {"key": "sleep"}),
             ("Kodi", {"app": "kodi"}), ("Stremio", {"app": "stremio"})]


def screenshot() -> Path:
    """The TV's screen as a PNG in ~/Pictures."""
    cfg = load_cfg()
    req = urllib.request.Request(f"http://127.0.0.1:{cfg.local_port}/v1/tv/screenshot",
                                 headers={"Authorization": f"Bearer {cfg.secret('agent_token') or ''}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    folder = Path.home() / "Pictures"
    folder.mkdir(exist_ok=True)
    path = folder / time.strftime("tv-%Y%m%d-%H%M%S.png")
    path.write_bytes(data)
    return path


class TvCard(QFrame):
    def __init__(self, panel):
        super().__init__()
        self.setObjectName("card")
        self.panel = panel
        self.info: dict = {}
        self.link: dict = {}
        self._busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        top = QHBoxLayout()
        self.title = QLabel("<b>TV</b>")
        self.title.setObjectName("cardTitle")
        top.addWidget(self.title, 1)
        for label, tip, fn in (("Diagnose", "Each step of reaching the TV over ADB, with what to do", self.diagnose),
                               ("Reconnect", "Drop the ADB connection and connect again", self.reconnect)):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            top.addWidget(b)
        lay.addLayout(top)
        self.network = QLabel("–")
        self.network.setTextFormat(Qt.TextFormat.RichText)
        self.network.setWordWrap(True)
        lay.addWidget(self.network)
        laptop = QHBoxLayout()
        self.laptop = QLabel("")
        self.laptop.setTextFormat(Qt.TextFormat.RichText)
        laptop.addWidget(self.laptop, 1)
        self.speed = QPushButton("Speed test")
        self.speed.setToolTip("Reads 16 MB from the TV over ADB: a lower bound for the link")
        self.speed.clicked.connect(self.speed_test)
        laptop.addWidget(self.speed)
        lay.addLayout(laptop)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        cpu_box, self.cpu, self.cpu_spark = metric("TV CPU", BLUE, 20.0)
        mem_box, self.mem, self.mem_spark = metric("TV memory", PURPLE, 50.0)
        grid.addWidget(cpu_box, 0, 0)
        grid.addWidget(mem_box, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.busy = dim("")
        lay.addWidget(self.busy)

        app = QHBoxLayout()
        self.app = QLabel("")
        self.app.setTextFormat(Qt.TextFormat.RichText)
        app.addWidget(self.app, 1)
        self.install = QPushButton("Build and install")
        self.install.setToolTip("Builds the TV app from this copy's code (android/build-and-install.sh), installs "
                                "it, grants the microphone and pairs it. Releases from GitHub: Settings › Updates")
        self.install.clicked.connect(self.install_app)
        self.uninstall = QPushButton("Uninstall")
        self.uninstall.clicked.connect(self.uninstall_app)
        pair = QPushButton("Pair again")
        pair.setToolTip("Sends the TV app and TV Agent Player this computer's addresses and their tokens")
        pair.clicked.connect(lambda: self.run_cli(["tv", "pair"], "Pairing…"))
        for b in (self.install, self.uninstall, pair):
            app.addWidget(b)
        lay.addLayout(app)

        keys = QGridLayout()                    # two rows, so the card fits a narrow window
        keys.addWidget(dim("Shortcuts"), 0, 0)
        shot = QPushButton("Screenshot")
        shot.setToolTip("Saved in ~/Pictures and opened")
        shot.clicked.connect(self.take_screenshot)
        buttons = []
        for label, what in SHORTCUTS:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, w=what: self.shortcut(w))
            buttons.append(b)
        for n, b in enumerate(buttons + [shot]):
            keys.addWidget(b, n // 6, 1 + n % 6)
        keys.setColumnStretch(7, 1)
        lay.addLayout(keys)

        play = QHBoxLayout()
        self.playing = QLabel("")
        self.playing.setTextFormat(Qt.TextFormat.RichText)
        self.playing.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.playing.linkActivated.connect(self.link_clicked)
        play.addWidget(self.playing, 1)
        lay.addLayout(play)
        send = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("Send a link to the TV: a video's https:// address")
        self.url.returnPressed.connect(self.send_link)
        go = QPushButton("Play on the TV")
        go.clicked.connect(self.send_link)
        send.addWidget(self.url, 1)
        send.addWidget(go)
        lay.addLayout(send)
        self.note = dim("")
        lay.addWidget(self.note)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(8000)
        QTimer.singleShot(1500, self.refresh)

    # ---- refreshing ----

    def refresh(self):
        if self._busy or not self.isVisible() or not self.panel.overview.cards["tv-agent"].active:
            return
        self._busy = True

        def got(res, err):
            self._busy = False
            if err or not res or res.get("error"):
                self.title.setText("<b>TV</b> <span style='color:gray'>· not answering over ADB "
                                   f"({(res or {}).get('error') or err})</span>")
                return
            self.show_info(res)

        self.panel.bg.run(lambda: api("/v1/tv/status", timeout=20), got)
        self.panel.bg.run(lambda: api("/v1/now-playing/link", timeout=5), self.show_link)

    def show_info(self, i: dict):
        self.info = i
        screen = {"awake": "", "asleep": f" · <span style='color:{AMBER}'>screen off</span>",
                  "dreaming": " · screensaver"}.get(i.get("screen"), "")
        self.title.setText(f"<b>TV</b> <span style='color:gray'>· {i.get('model', '')} · Android "
                           f"{i.get('android', '')} · up {i.get('uptime') or '?'}</span>{screen}")
        if i.get("link") == "wifi" and i.get("wifi"):
            w = i["wifi"]
            band = f"{int(w['mhz']) / 1000:.1f} GHz" if w.get("mhz") else ""
            signal = int(w["rssi"]) if w.get("rssi") else None
            quality = ("strong" if signal > -60 else "fair" if signal > -72 else "weak") if signal is not None else ""
            net = (f"Wi-Fi “{w.get('ssid')}” · {w.get('link_mbps')} Mbit/s link · signal {signal} dBm ({quality}) "
                   f"· {band}")
        elif i.get("link") == "ethernet":
            net = "Ethernet"
        else:
            net = "network unknown"
        self.network.setText(f"{net} · at {i.get('address')}")
        lap = i.get("laptop") or {}
        kind = "Wi-Fi" if lap.get("wifi") else "Ethernet"
        speed = f" {lap['link_mbps']} Mbit/s" if lap.get("link_mbps") else ""
        via = f" ({lap.get('via') or lap.get('interface', '')})" if lap else ""
        ping = f" · ping {i['ping_ms']:.1f} ms" if i.get("ping_ms") is not None else " · no ping answer"
        self.laptop.setText(f"This laptop: {kind}{speed}{via}{ping}")
        if i.get("cpu") is not None:
            self.cpu.setText(f"{i['cpu']:.0f} %")
            self.cpu_spark.push(i["cpu"])
        if mem := i.get("memory"):
            pct = 100 * mem["used_mb"] / mem["total_mb"]
            self.mem.setText(f"{mem['used_mb'] / 1024:.1f} of {mem['total_mb'] / 1024:.1f} GB")
            self.mem_spark.push(pct)
        busiest = ", ".join(f"{b['name'].split('.')[-1]} {b['cpu']:.0f}%" for b in i.get("busiest", [])[:4])
        front = i.get("foreground_label") or i.get("foreground") or "–"
        self.busy.setText(f"In front: {front} · busiest lately: {busiest or '–'}")
        app, here = i.get("app") or {}, android_version()
        if not app.get("installed"):
            self.app.setText(f"<span style='color:{AMBER}'>⚠</span> <b>TV app not installed</b>"
                             + (f" (this copy's code: {here})" if here else ""))
            self.install.setText("Build and install")
        else:
            newer = here and _version(here) > _version(app.get("version") or "0")
            state = (f"<span style='color:{AMBER}'>this copy's code is {here}</span>" if newer else
                     f"<span style='color:{GREEN}'>the same as this copy's code</span>" if here == app.get("version")
                     else f"<span style='color:gray'>this copy's code is {here or '?'}</span>")
            self.app.setText(f"<b>TV app {app.get('version')}</b> · {state} · installed {app.get('updated') or '?'}")
            self.install.setText(f"Build and install {here}" if newer else "Build and reinstall")
        self.uninstall.setEnabled(bool(app.get("installed")))

    def show_link(self, res, err):
        self.link = res or {}
        if not self.link.get("url"):
            self.playing.setText("<span style='color:gray'>The agent isn't playing anything</span>")
            return
        vid = self.link.get("video_id", "")
        ep = f" · S{vid.split(':')[1]}E{vid.split(':')[2]}" if vid.count(":") == 2 else ""
        self.playing.setText(f"Playing <b>{self.link.get('name', '')}{ep}</b> "
                             f"<span style='color:gray'>{self.link.get('filename', '')}</span> · "
                             "<a href='open'>open the stream</a> · <a href='copy'>copy its link</a>")

    def link_clicked(self, what: str):
        url = self.link.get("url")
        if not url:
            return
        if what == "copy":
            QGuiApplication.clipboard().setText(url)
            self.note.setText("Stream link copied (it carries your debrid key: keep it to yourself)")
        else:
            subprocess.Popen(["xdg-open", url])

    # ---- actions ----

    def shortcut(self, what: dict):
        if "key" in what:
            self.panel.bg.run(lambda: api("/v1/tv/key", what, timeout=8),
                              lambda r, e: self.note.setText(f"<span style='color:{RED}'>{e}</span>" if e else ""))
        else:
            self.panel.bg.run(lambda: api("/v1/action", {"type": "open_app", "app": what["app"]}, timeout=15),
                              lambda r, e: self.note.setText(str(e) if e else (r or {}).get("say", "")))

    def take_screenshot(self):
        self.note.setText("Taking a screenshot…")

        def done(path, err):
            if err:
                self.note.setText(f"<span style='color:{RED}'>{err}</span>")
                return
            self.note.setText(f"Saved {path}")
            subprocess.Popen(["xdg-open", str(path)])

        self.panel.bg.run(screenshot, done)

    def speed_test(self):
        self.speed.setEnabled(False)
        self.speed.setText("Testing…")

        def done(r, err):
            self.speed.setEnabled(True)
            self.speed.setText(f"{r['mbps']:.0f} Mbit/s over ADB" if r and r.get("mbps") else "Speed test")
            if err or not r or r.get("error"):
                self.note.setText(f"Speed test failed: {err or r.get('error')}")

        self.panel.bg.run(lambda: api("/v1/tv/speedtest", {}, timeout=90), done)

    def send_link(self):
        url = self.url.text().strip()
        if not url.startswith(("http://", "https://")):
            self.note.setText("A link starting with http:// or https://")
            return
        self.panel.bg.run(lambda: api("/v1/tv/play-link", {"url": url}, timeout=60),
                          lambda r, e: self.note.setText(str(e) if e else (r or {}).get("say", "")))

    def reconnect(self):
        self.note.setText("Reconnecting…")
        self.panel.bg.run(lambda: api("/v1/tv/reconnect", {}, timeout=20),
                          lambda r, e: self.note.setText(f"Connected to {r['host']}" if r and r.get("ok") else
                                                         f"Not connected: {e or (r or {}).get('error')}"))

    def diagnose(self):
        self.note.setText("Checking each step…")

        def done(r, err):
            self.note.setText("")
            lines = []
            for s in (r or {}).get("steps", []):
                mark = "✓" if s["ok"] else "•" if s["ok"] is None else "✗"
                lines.append(f"{mark} {s['step']}: {s['detail']}" + (f"\n    → {s['hint']}" if s.get("hint") else ""))
            dlg = QDialog(self)
            dlg.setWindowTitle("Reaching the TV")
            v = QVBoxLayout(dlg)
            box = QPlainTextEdit("\n".join(lines) or str(err))
            box.setReadOnly(True)
            box.setMinimumSize(640, 260)
            v.addWidget(box)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(dlg.reject)
            v.addWidget(bb)
            dlg.exec()

        self.panel.bg.run(lambda: api("/v1/tv/diagnose", {}, timeout=60), done)

    def run_cli(self, args: list[str], doing: str):
        self.note.setText(doing)

        def run():
            r = subprocess.run([str(TV_AGENT), *args], capture_output=True, text=True, timeout=600)
            return [l for l in (r.stdout + r.stderr).splitlines() if " INFO " not in l][-1:] or ["done"]

        self.panel.bg.run(run, lambda out, err: (self.note.setText((out or [str(err)])[0]), self.refresh()))

    def install_app(self):
        script = REPO / "android" / "build-and-install.sh"
        if QMessageBox.question(self, "TV Agent", "Build the TV app from this copy and install it on the TV? Building "
                                "takes a minute; playback isn't touched.") != QMessageBox.StandardButton.Yes:
            return
        self.install.setEnabled(False)
        self.note.setText("Building and installing the TV app…")

        def run():
            r = subprocess.run([str(script)], capture_output=True, text=True, timeout=900)
            return r.returncode, [l for l in (r.stdout + r.stderr).splitlines() if l.strip()][-2:]

        def done(res, err):
            self.install.setEnabled(True)
            code, out = res if res else (1, [str(err)])
            self.note.setText(("Installed: " if code == 0 else "Didn't work: ") + " · ".join(out))
            self.refresh()

        self.panel.bg.run(run, done)

    def uninstall_app(self):
        if QMessageBox.question(self, "TV Agent", "Uninstall the TV app? The remote's mic button then does nothing "
                                "until it's installed again.") != QMessageBox.StandardButton.Yes:
            return
        self.run_cli(["tv", "shell", "pm", "uninstall", "com.boujuan.tvagent"], "Uninstalling…")


def _version(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))
