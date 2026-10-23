"""Phones card (Settings tab): the phones paired with the agent (the TV Agent phone app), renaming and revoking them,
and pairing a new one with a QR code (`qrencode`, else the code and address to type). All through the agent's local
API (/v1/clients…); the agent keeps only a hash of each phone's token, and none of it passes through here."""
import html
import shutil
import subprocess
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout,
                               QWidget)

from .common import AMBER, GREEN, RED, api, card, dim


def ago(t: float | None) -> str:
    if not t:
        return "never"
    s = int(time.time() - t)
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return time.strftime("%d %b", time.localtime(t))


def grouped(fingerprint: str) -> str:
    """The certificate's fingerprint as the phone shows it to compare: its first 16 hex digits, in fours."""
    f = fingerprint[:16].upper()
    return " ".join(f[i:i + 4] for i in range(0, 16, 4))


def qr_png(text: str) -> bytes | None:
    exe = shutil.which("qrencode")
    if not exe:
        return None
    r = subprocess.run([exe, "-t", "PNG", "-s", "8", "-m", "2", "-l", "M", "-o", "-", text], capture_output=True,
                       timeout=10)
    return r.stdout if r.returncode == 0 and r.stdout else None


class Phones(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        box = card()
        lay.addWidget(box)
        self.v = QVBoxLayout(box)
        self.v.setContentsMargins(16, 14, 16, 14)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Phones</b> <span style='color:gray'>· the TV Agent phone app: remote, voice, "
                              "activity and settings, paired to this agent</span>"), 1)
        self.pair_button = QPushButton("Pair a phone…")
        self.pair_button.clicked.connect(self.pair)
        head.addWidget(self.pair_button)
        self.v.addLayout(head)
        self.status = dim("")
        self.v.addWidget(self.status)
        self.rows = QVBoxLayout()
        self.v.addLayout(self.rows)
        self.v.addWidget(dim("A phone's token is kept only as a hash and works only on the phones' own HTTPS "
                             "listener; revoking it there stops it at once. The phones never get API keys, the "
                             "Stremio key, the Kodi password or the stream links."))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        self.panel.bg.run(lambda: api("/v1/clients", timeout=5), self.show_phones)

    def show_phones(self, res, err=None):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if err or not res:
            self.status.setText(f"<span style='color:gray'>Can't list phones: {err or 'no answer'} (is the voice "
                                "agent running, and from this version?)</span>")
            self.pair_button.setEnabled(False)
            return
        if not res.get("enabled"):
            self.status.setText(f"<span style='color:{AMBER}'>⚠ Phones are off (<code>phones = false</code> in "
                                "config.toml)</span>")
        elif not res.get("listening"):
            self.status.setText(f"<span style='color:{RED}'>✗ The phones' listener isn't running (port "
                                f"{res.get('port')} taken? see the voice agent's log)</span>")
        else:
            self.status.setText(f"<span style='color:{GREEN}'>✔</span> Listening for phones on port {res['port']} "
                                f"(HTTPS) and {res['stream_port']} (voice) · certificate "
                                f"<code>{grouped(res['fingerprint'])}</code>")
        self.pair_button.setEnabled(bool(res.get("listening")))
        devices = res.get("devices") or []
        if not devices:
            self.rows.addWidget(dim("No phones paired yet."))
        for d in devices:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 2, 0, 2)
            seen = ago(d.get("last_seen")) + (f" from {d['last_ip']}" if d.get("last_ip") else "")
            h.addWidget(QLabel(f"📱 <b>{html.escape(d['name'])}</b> <span style='color:gray'>· paired "
                               f"{ago(d['paired'])} · last seen {html.escape(seen)}</span>"), 1)
            rename = QPushButton("Rename…")
            rename.clicked.connect(lambda _=False, d=d: self.rename(d))
            revoke = QPushButton("Revoke")
            revoke.clicked.connect(lambda _=False, d=d: self.revoke(d))
            h.addWidget(rename)
            h.addWidget(revoke)
            self.rows.addWidget(row)

    def rename(self, d: dict):
        name, ok = QInputDialog.getText(self, "TV Agent", "This phone's name:", text=d["name"])
        if ok and name.strip():
            self.panel.bg.run(lambda: api("/v1/clients/rename", {"id": d["id"], "name": name.strip()}),
                              lambda *_: self.refresh())

    def revoke(self, d: dict):
        if QMessageBox.question(self, "TV Agent", f"Revoke {d['name']}? It stops working at once; to use it again, "
                                "pair it again.") != QMessageBox.StandardButton.Yes:
            return

        def done(_res, err):
            self.panel.say(f"Not revoked: {err}" if err else f"{d['name']} revoked")
            self.refresh()
        self.panel.bg.run(lambda: api("/v1/clients/revoke", {"id": d["id"]}), done)

    def pair(self):
        def done(res, err):
            if err:
                QMessageBox.warning(self, "TV Agent", f"Can't start pairing: {err}")
                return
            PairDialog(self, res).exec()
            self.refresh()
        self.panel.bg.run(lambda: api("/v1/clients/pairing", {}, timeout=5), done)


class PairDialog(QDialog):
    """The QR code (or the code and address to type) until a phone pairs, the code expires or this closes."""

    def __init__(self, phones: Phones, res: dict):
        super().__init__(phones)
        self.phones, self.panel = phones, phones.panel
        self.setWindowTitle("Pair a phone")
        self.deadline = time.time() + int(res.get("expires_in", 300))
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.addWidget(QLabel("<b>In the TV Agent phone app: Add an agent › Scan the QR code</b>"))
        qr = QLabel()
        qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        png = qr_png(res["link"])
        pix = QPixmap()
        if png and pix.loadFromData(png):
            qr.setPixmap(pix)
            qr.setToolTip(res["link"].split("&c=")[0] + "&c=…")
        else:
            qr.setText("<span style='color:gray'>No QR code here: install <code>qrencode</code> for one. "
                       "Type the code below instead.</span>")
        v.addWidget(qr)
        code = QLabel(f"<span style='font-size:22pt; font-weight:600; letter-spacing:3px'>{res['code']}</span>")
        code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(code)
        addresses = " or ".join(f"<code>{a}</code>" for a in res.get("addresses") or [])
        v.addWidget(dim(f"Or type the code, with the address {addresses}. The app then shows a fingerprint: it must "
                        f"be <b><code>{grouped(res['fingerprint'])}</code></b>, else don't pair."))
        self.state = QLabel("")
        v.addWidget(self.state)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        v.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        self.paired = False
        self.tick()

    def tick(self):
        left = max(0, int(self.deadline - time.time()))
        self.state.setText(f"<span style='color:gray'>Waiting for the phone · the code works once, for "
                           f"{left // 60}:{left % 60:02d} more</span>")

    def poll(self):
        if self.paired:
            return
        self.tick()

        def done(res, err):
            if err or not res or self.paired:
                return
            if res.get("paired"):
                self.paired = True
                self.state.setText(f"<span style='color:{GREEN}; font-weight:600'>✔ "
                                   f"{html.escape(res['paired']['name'])} paired</span>")
                self.panel.say(f"{res['paired']['name']} paired")
                QTimer.singleShot(1500, self.accept)
            elif not res.get("open"):
                self.timer.stop()
                self.state.setText(f"<span style='color:{AMBER}'>The code expired (or too many wrong tries): close "
                                   "this and pair again</span>")
        self.panel.bg.run(lambda: api("/v1/clients/pairing", timeout=3), done)

    def done(self, result):
        self.timer.stop()
        if not self.paired:
            self.panel.bg.run(lambda: api("/v1/clients/pairing/cancel", {}, timeout=3))
        super().done(result)
