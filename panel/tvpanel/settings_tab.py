"""Settings tab: the agent's settings (config.toml, applied by restarting the agent) and this panel's own
behaviour (tray, notifications, start at login; applied at once)."""
import json
import os
import subprocess
import tomllib

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .common import agent_config, card, dim, load_cfg, run_doctor, set_autostart

AUDIO = [("English", "eng"), ("Spanish", "spa"), ("French", "fre"), ("Italian", "ita"), ("German", "ger"),
         ("Portuguese", "por"), ("Japanese", "jpn"), ("Korean", "kor"), ("Chinese", "chi")]
SUBTITLES = [("English", "eng"), ("Spanish", "spa"), ("Chinese (Simplified · 简体)", "chs"),
             ("Chinese (Traditional · 繁體)", "cht"), ("Chinese (either)", "chi"), ("French", "fre"),
             ("Italian", "ita"), ("German", "ger"), ("Portuguese", "por"), ("Japanese", "jpn"), ("Korean", "kor")]


def toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


class OrderedChecklist(QWidget):
    """Tick the languages to use, drag (or Up/Down) to set which is tried first."""

    def __init__(self, options: list[tuple[str, str]]):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list.setMaximumHeight(150)
        for label, code in options:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        btns = QVBoxLayout()
        for text, d in (("▲", -1), ("▼", 1)):
            b = QPushButton(text)
            b.setFixedWidth(34)
            b.clicked.connect(lambda _=False, d=d: self.move(d))
            btns.addWidget(b)
        btns.addStretch()
        lay.addWidget(self.list, 1)
        lay.addLayout(btns)

    def move(self, d: int):
        row = self.list.currentRow()
        if row < 0 or not 0 <= row + d < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(row + d, item)
        self.list.setCurrentRow(row + d)

    def set_codes(self, codes: list[str]):
        items = [self.list.item(i) for i in range(self.list.count())]
        by_code = {it.data(Qt.ItemDataRole.UserRole): it for it in items}
        order = [c for c in codes if c in by_code] + [it.data(Qt.ItemDataRole.UserRole) for it in items
                                                      if it.data(Qt.ItemDataRole.UserRole) not in codes]
        for _ in range(self.list.count()):
            self.list.takeItem(0)
        for code in order:
            it = by_code[code]
            it.setCheckState(Qt.CheckState.Checked if code in codes else Qt.CheckState.Unchecked)
            self.list.addItem(it)

    def codes(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


class Settings(QScrollArea):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        self.setWidget(inner)
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)

        play = card()
        f1 = QFormLayout(play)
        f1.setContentsMargins(16, 14, 16, 14)
        f1.addRow(QLabel("<b>Playback</b>"))
        self.player = QComboBox()
        self.player.addItem("Just Player · simple screen, media-key control", "just")
        self.player.addItem("Kodi · full control (exact seeks, per-file languages)", "kodi")
        self.height_ = QComboBox()
        for label, v in (("4K (2160p)", 2160), ("1440p", 1440), ("1080p", 1080), ("720p", 720)):
            self.height_.addItem(label, v)
        self.mbps = QDoubleSpinBox()
        self.mbps.setRange(5, 95)
        self.mbps.setDecimals(0)
        self.mbps.setSuffix(" Mbit/s")
        self.mbps.setToolTip("The TV's Ethernet port tops out at ~94 Mbit/s; Wi-Fi does ~300")
        self.probe = QCheckBox("Check a link answers before playing it")
        self.probe.setToolTip("Turn off while the TV uses a VPN (debrid services allow one IP at a time)")
        f1.addRow("Player", self.player)
        f1.addRow("Highest resolution", self.height_)
        f1.addRow("Highest bitrate", self.mbps)
        f1.addRow("", self.probe)
        outer.addWidget(play)

        lang = card()
        f2 = QFormLayout(lang)
        f2.setContentsMargins(16, 14, 16, 14)
        f2.addRow(QLabel("<b>Languages</b>"))
        f2.addRow(dim("Titles made where the native language is spoken play in it without subtitles; everything "
                      "else uses this audio and the first available subtitles in the order below."))
        self.audio = QComboBox()
        self.native = QComboBox()
        for label, code in AUDIO:
            self.audio.addItem(label, code)
            self.native.addItem(label, code)
        self.subs = OrderedChecklist(SUBTITLES)
        f2.addRow("Audio", self.audio)
        f2.addRow("Subtitles, in order", self.subs)
        f2.addRow("Native language", self.native)
        f2.addRow(dim("Kodi tells Simplified and Traditional Chinese apart by the track's name; Just Player only "
                      "knows “Chinese” and shows the first Chinese track."))
        outer.addWidget(lang)

        other = card()
        f3 = QFormLayout(other)
        f3.setContentsMargins(16, 14, 16, 14)
        f3.addRow(QLabel("<b>Other</b>"))
        self.twitch = QLineEdit()
        self.twitch.setPlaceholderText("ibai, auronplay")
        self.twitch.setToolTip("Matched first when you say a streamer's name (speech-to-text spells names freely)")
        self.log = QCheckBox("Keep a history of what you say (Activity tab)")
        self.hosts = QLineEdit()
        self.hosts.setToolTip("The TV's addresses (Ethernet, Wi-Fi); also the only ones the agent accepts audio from")
        f3.addRow("Twitch favourites", self.twitch)
        f3.addRow("", self.log)
        f3.addRow("TV addresses", self.hosts)
        row = QHBoxLayout()
        self.note = dim("")
        doctor = QPushButton("Run checks")
        doctor.clicked.connect(self.doctor)
        open_file = QPushButton("Open config file")
        open_file.clicked.connect(lambda: subprocess.Popen(["xdg-open", str(agent_config.CONFIG_FILE)]))
        save = QPushButton("Save and apply")
        save.setDefault(True)
        save.clicked.connect(self.save)
        row.addWidget(self.note, 1)
        row.addWidget(doctor)
        row.addWidget(open_file)
        row.addWidget(save)
        f3.addRow(row)
        outer.addWidget(other)

        me = card()
        f4 = QVBoxLayout(me)
        f4.setContentsMargins(16, 14, 16, 14)
        f4.addWidget(QLabel("<b>This panel</b> <span style='color:gray'>· applies at once</span>"))
        self.prefs_boxes = {}
        for key, text in (("close_to_tray", "Closing the window keeps the panel in the system tray"),
                          ("start_in_tray", "Open hidden in the tray"),
                          ("tray_details", "Show what's playing and resource use when hovering the tray icon"),
                          ("notify", "Show results as notifications (laptop mic and tray commands)"),
                          ("autostart", "Start the panel when I log in (in the tray)")):
            box = QCheckBox(text)
            box.setChecked(panel.prefs.get(key))
            box.toggled.connect(lambda on, key=key: self.set_pref(key, on))
            self.prefs_boxes[key] = box
            f4.addWidget(box)
        outer.addWidget(me)
        outer.addStretch()
        self.load()

    def set_pref(self, key: str, on: bool):
        self.panel.prefs.set(key, on)
        if key == "autostart":
            set_autostart(on)
        self.panel.prefs_changed()

    @staticmethod
    def _select(combo: QComboBox, value):
        i = combo.findData(value)
        if i < 0:
            combo.addItem(str(value), value)
            i = combo.count() - 1
        combo.setCurrentIndex(i)

    def load(self):
        try:
            cfg = load_cfg()
        except SystemExit as e:
            self.note.setText(str(e))
            return
        self._select(self.player, cfg.player)
        self._select(self.height_, cfg.max_height)
        self.mbps.setValue(cfg.max_mbps)
        self.probe.setChecked(cfg.probe_streams)
        self._select(self.audio, cfg.audio_language)
        self._select(self.native, cfg.native_language)
        self.subs.set_codes(cfg.subtitle_languages)
        self.twitch.setText(", ".join(cfg.twitch_favorites))
        self.log.setChecked(cfg.log_transcripts)
        self.hosts.setText(", ".join(cfg.tv_hosts))

    def values(self) -> dict:
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
        return {
            "player": self.player.currentData(),
            "max_height": int(self.height_.currentData()),
            "max_mbps": float(self.mbps.value()),
            "probe_streams": self.probe.isChecked(),
            "audio_language": self.audio.currentData(),
            "subtitle_languages": self.subs.codes() or ["eng"],
            "native_language": self.native.currentData(),
            "twitch_favorites": split(self.twitch.text()),
            "log_transcripts": self.log.isChecked(),
            "tv_hosts": split(self.hosts.text()) or agent_config.Config().tv_hosts,
        }

    def save(self, values: dict | None = None):
        path = agent_config.CONFIG_FILE
        old = path.read_text() if path.exists() else None
        try:
            current = tomllib.loads(old) if old else {}
        except ValueError:
            current = {}
        merged = {**current, **(values or self.values())}     # settings not shown here are kept
        lines = ["# tv-agent settings (written by the TV Agent panel; see config.example.toml for all of them).\n"]
        lines += [f"{k} = {toml(v)}\n" for k, v in merged.items()]
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(lines))
        os.replace(tmp, path)
        try:
            load_cfg()                             # the daemon's own validation
        except SystemExit as e:
            if old is None:
                path.unlink()
            else:
                path.write_text(old)
            QMessageBox.warning(self, "TV Agent", f"Not saved: {e}")
            return False
        agent = self.panel.overview.cards["tv-agent"]
        if agent.active:
            self.note.setText("Saved. Restarting the voice agent to apply (what's playing carries on)…")
            agent.act("restart")
        else:
            self.note.setText("Saved. It applies the next time the voice agent starts.")
        self.load()
        return True

    def doctor(self):
        self.note.setText("Running checks…")

        def done(out, err):
            self.note.setText("")
            dlg = QDialog(self)
            dlg.setWindowTitle("TV Agent checks")
            v = QVBoxLayout(dlg)
            text = QPlainTextEdit(out if out else str(err))
            text.setReadOnly(True)
            text.setMinimumSize(560, 220)
            v.addWidget(text)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(dlg.reject)
            v.addWidget(bb)
            dlg.exec()

        self.panel.bg.run(run_doctor, done)
