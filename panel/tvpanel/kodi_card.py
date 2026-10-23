"""Kodi card (Settings tab): which Kodi the TV has, and a switch between 21 (Omega, stable) and 22 (Piers, with
fixes for this TV's decoders), done by the voice agent (its kodi_versions.py): a backup on the TV first, the APK from
Kodi's mirrors checked against its SHA-256, and going back to 21 restoring that backup. Applied at once (not the
window's Save); a switch takes a minute or two."""
import html
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from .common import AMBER, GREEN, RED, api, card, dim


class KodiCard(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.state: dict = {}
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        box = card()
        lay.addWidget(box)
        v = QVBoxLayout(box)
        v.setContentsMargins(16, 14, 16, 14)
        v.addWidget(QLabel("<b>Kodi on the TV</b> <span style='color:gray'>· applies at once</span>"))
        self.status = QLabel("…")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        row = QHBoxLayout()
        self.use22 = QCheckBox("Use Kodi 22")
        self.use22.setToolTip("Kodi 22 (Piers) fixes seeking on MediaTek decoders like this TV's. It converts Kodi's "
                              "databases one way, so a backup is made on the TV first, and going back restores it.")
        self.use22.toggled.connect(self.toggled)
        row.addWidget(self.use22)
        row.addStretch()
        self.update_button = QPushButton("Update Kodi")
        self.update_button.clicked.connect(self.update_same)
        self.update_button.setVisible(False)
        row.addWidget(self.update_button)
        v.addLayout(row)
        self.progress = dim("")
        v.addWidget(self.progress)
        v.addWidget(dim("A switch backs up Kodi's settings, libraries and add-ons on the TV, downloads the new Kodi "
                        "from kodi.tv (checked against its published SHA-256) and installs it. Back to 21: Kodi 22 "
                        "is uninstalled, 21 installed and that backup restored. Nothing is played meanwhile."))
        self.poll = QTimer(self)
        self.poll.setInterval(2000)
        self.poll.timeout.connect(self.refresh)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        self.panel.bg.run(lambda: api("/v1/kodi", timeout=30), self.show_state)

    def show_state(self, res, err=None):
        if err or not res or res.get("error"):
            self.status.setText(f"<span style='color:gray'>Can't check Kodi on the TV: "
                                f"{html.escape(str(err or (res or {}).get('error') or 'no answer'))}</span>")
            self.use22.setEnabled(False)
            return
        self.state = res
        avail = res.get("available") or {}
        major = res.get("major")
        where = {"com.android.vending": "Google Play"}.get(res.get("from") or "", res.get("from") or "")
        text = (f"<span style='color:{GREEN}'>✔</span> Kodi {html.escape(res['version'])}"
                + (f" <span style='color:gray'>(from {html.escape(where)}, {res.get('abi')})</span>" if where else "")
                if res.get("installed") else f"<span style='color:{RED}'>✗ Kodi isn't installed on the TV</span>")
        for version, b in sorted((res.get("backups") or {}).items()):
            text += (f"<br><span style='color:gray'>Backup of Kodi {version} on the TV: {b['size'] // 1_000_000} MB, "
                     f"{time.strftime('%d %b %H:%M', time.localtime(b['made']))}</span>")
        self.status.setText(text)
        self._loading = True
        self.use22.setChecked(major == "22")
        self._loading = False
        if "22" in avail:
            self.use22.setText(f"Use Kodi 22 ({avail['22']['label']}"
                               + ("" if avail["22"]["stable"] else ", a release candidate") + ")")
        mine = avail.get(major or "")
        newer = bool(mine and res.get("version") and mine["version"] != res["version"]
                     and tuple(map(int, mine["version"].split("."))) > tuple(map(int, res["version"].split("."))))
        self.update_button.setText(f"Update to {mine['label']}" if newer else "Update Kodi")
        self.update_button.setVisible(newer)
        job = res.get("job")
        running = bool(job and not job.get("done") and not job.get("error"))
        self.use22.setEnabled(not running and "22" in avail and res.get("installed", False))
        self.update_button.setEnabled(not running)
        if job:
            color = RED if job.get("error") else GREEN if job.get("done") else AMBER
            self.progress.setText(f"<span style='color:{color}'>{html.escape(job.get('error') or job.get('step') or '')}"
                                  f"</span>" + (f" · {html.escape(job['note'])}" if job.get("note") else ""))
        if running and not self.poll.isActive():
            self.poll.start()
        elif not running and self.poll.isActive():
            self.poll.stop()
            if job.get("needs") == "replace":
                self.offer_replace(job["to"])

    def toggled(self, on: bool):
        if self._loading:
            return
        to = "22" if on else "21"
        avail = (self.state.get("available") or {}).get(to)
        if not avail:
            return
        backups = self.state.get("backups") or {}
        if to == "22":
            ask = (f"Install Kodi {avail['label']} on the TV?\n\nKodi {self.state.get('version')}'s settings, libraries "
                   "and add-ons are backed up on the TV first; Kodi 22 converts its databases, so going back later "
                   "reinstalls 21 and restores that backup. About 70 MB and a minute or two.")
        else:
            had = sorted((v for v in backups if v.startswith("21.")), reverse=True)
            if not had:
                QMessageBox.warning(self, "TV Agent", "There's no backup from Kodi 21 on the TV: going back would start "
                                    "Kodi 21 empty, so it isn't done from here.")
                self._set(True)
                return
            ask = (f"Go back to Kodi {avail['label']}?\n\nKodi 22 is backed up, then uninstalled; Kodi 21 is installed "
                   f"and the backup from Kodi {had[0]} restored (what you changed in Kodi 22 since isn't kept).")
        if QMessageBox.question(self, "TV Agent", ask) != QMessageBox.StandardButton.Yes:
            self._set(not on)
            return
        self.start(to)

    def _set(self, on: bool):
        self._loading = True
        self.use22.setChecked(on)
        self._loading = False

    def start(self, to: str, replace: bool = False):
        def done(_res, err):
            if err:
                QMessageBox.warning(self, "TV Agent", f"Kodi wasn't switched: {err}")
            self.refresh()
            self.poll.start()
        self.use22.setEnabled(False)
        self.progress.setText("Starting…")
        self.panel.bg.run(lambda: api("/v1/kodi/switch", {"to": to, "replace": replace}, timeout=15), done)

    def update_same(self):
        major = self.state.get("major")
        mine = (self.state.get("available") or {}).get(major or "")
        if mine and QMessageBox.question(self, "TV Agent", f"Update Kodi to {mine['label']}? A backup is made on the "
                                         "TV first.") == QMessageBox.StandardButton.Yes:
            self.start(major)

    def offer_replace(self, to: str):
        if QMessageBox.question(
                self, "TV Agent", "This Kodi came from Google Play, which signs it with another key, so Kodi from "
                "kodi.tv can't be installed over it.\n\nReplace it? It's uninstalled, the new one installed, and the "
                "backup just made restored into it (settings, libraries, add-ons).") == QMessageBox.StandardButton.Yes:
            self.start(to, replace=True)
        else:
            self.refresh()
