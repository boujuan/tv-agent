"""Settings tab: the agent's settings (config.toml; the window's Save writes them and restarts the agent) and this
panel's own behaviour (tray, notifications, start at login; applied at once). Also export/import of everything, the
checks, and whether the chosen player is on the TV."""
import json
import os
import subprocess
import time
import tomllib
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .common import AMBER, GREEN, RED, TV_AGENT, Say, agent_config, api, card, dim, load_cfg, set_autostart
from .phones import Phones
from .updates import UpdatesCard

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
        self.dirty = False
        self._loading = False
        self.note = Say(panel)                      # messages go to the window's status bar
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
        self.mbps.setRange(5, 400)
        self.mbps.setDecimals(0)
        self.mbps.setSuffix(" Mbit/s")
        self.mbps.setToolTip("Streams above it are skipped. The TV's Ethernet port tops out at ~94 Mbit/s; "
                             "Wi-Fi does ~300")
        self.no_limit = QCheckBox("No limit")
        self.no_limit.setToolTip("The best stream whatever its bitrate (the resolution limit still applies)")
        self.no_limit.toggled.connect(lambda on: self.mbps.setEnabled(not on))
        mbps_row = QHBoxLayout()
        mbps_row.addWidget(self.mbps)
        mbps_row.addWidget(self.no_limit)
        mbps_row.addStretch()
        self.probe = QCheckBox("Check a link answers before playing it")
        self.probe.setToolTip("Turn off while the TV uses a VPN (debrid services allow one IP at a time)")
        self.binge = QCheckBox("Binge mode: when an episode ends, the next one starts")
        self.binge.setToolTip("Also by voice: “binge mode on / off”. “Stop after this episode” wins for one episode.")
        self.auto_skip = QCheckBox("Skip intros and recaps by themselves when their timestamps are sure")
        self.auto_skip.setToolTip("From TheIntroDB and IntroDB; “skip the intro” works either way. "
                                  "Also by voice: “auto skip on / off”.")
        f1.addRow("Player", self.player)
        self.player_state = dim("", wrap=False)     # is it installed on the TV
        f1.addRow("", self.player_state)
        self.player.currentIndexChanged.connect(lambda _i: self.show_players())
        self.packages: dict = {}
        f1.addRow("Highest resolution", self.height_)
        f1.addRow("Highest bitrate", mbps_row)
        f1.addRow("", self.probe)
        f1.addRow("", self.binge)
        f1.addRow("", self.auto_skip)
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

        self.keys_card = card()
        f5 = QFormLayout(self.keys_card)
        f5.setContentsMargins(16, 14, 16, 14)
        f5.addRow(QLabel("<b>API keys</b> <span style='color:gray'>· checked with the service, then kept in "
                         "secrets.toml (never shown)</span>"))
        self.key_lines = {}
        for what, name, used in (("openrouter", "OpenRouter", "the LLM step, hosted speech-to-text and decision "
                                                              "models"),
                                 ("liquid", "Liquid", "d1 and d1-ultra through Liquid's own API")):
            line = QHBoxLayout()
            status = QLabel("")
            status.setTextFormat(Qt.TextFormat.RichText)
            b = QPushButton(f"Set {name} key…")
            b.setToolTip(f"tv-agent setup {what}")
            b.clicked.connect(lambda _=False, w=what: self.set_key(w))
            line.addWidget(status, 1)
            line.addWidget(b)
            self.key_lines[what] = (status, name, used)
            f5.addRow(line)
        f5.addRow(dim("Which steps decide a command, their order and models, and speech-to-text: the Decisions tab, "
                      "live at once. OpenRouter and Liquid may keep what they're sent (what you said)."))
        outer.addWidget(self.keys_card)

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
        outer.addWidget(other)

        self.updates = UpdatesCard(self, panel)     # its mode is saved with the rest; its buttons act at once
        outer.addWidget(self.updates)
        self.phones = Phones(panel)                 # its own API calls, applied at once (not the window's Save)
        outer.addWidget(self.phones)

        self.show_advanced = QCheckBox("Show advanced settings (addresses, ports, Kodi login, pairing)")
        outer.addWidget(self.show_advanced)
        self.advanced = card()
        f6 = QFormLayout(self.advanced)
        f6.setContentsMargins(16, 14, 16, 14)
        f6.addRow(QLabel("<b>Advanced</b> <span style='color:gray'>· a wrong value can stop the voice agent from "
                         "starting; “Run checks” tells</span>"))
        self.adv: dict[str, QWidget] = {}
        for key, label, tip in (
                ("lan_host", "This computer on the TV's network", "Empty: worked out (never 0.0.0.0)"),
                ("lan_port", "Port for the TV app", ""), ("stream_port", "Port for the TV app's audio", ""),
                ("local_port", "Local API port (panel, CLI)", ""), ("adb_port", "TV's ADB port", ""),
                ("adb_key", "ADB key file", ""), ("kodi_port", "Kodi web port", ""), ("kodi_user", "Kodi web user", ""),
                ("d1_url", "Local decision model server", "llama.cpp's address (llama-d1)"),
                ("llama_server", "llama-server program", "Empty: on PATH or under ~/.local/opt; after changing it: "
                                                         "tv-agent setup services")):
            if key.endswith("_port"):
                w = QSpinBox()
                w.setRange(1, 65535)
            else:
                w = QLineEdit()
            w.setToolTip(tip)
            self.adv[key] = w
            f6.addRow(label, w)
        more = QHBoxLayout()
        for label, tip, fn in (("Set Kodi password…", "Kodi › Settings › Services › Control: the web password",
                                self.set_kodi_password),
                               ("Pair the TV app again", "Sends the app this computer's address and the token",
                                self.pair_app)):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            more.addWidget(b)
        more.addStretch()
        f6.addRow("", more)
        f6.addRow(dim("The local decision model's file and the speech model are chosen in the Decisions tab."))
        self.advanced.setVisible(False)
        self.show_advanced.toggled.connect(self.advanced.setVisible)
        outer.addWidget(self.advanced)

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
        for w in (self.player, self.height_, self.audio, self.native, self.updates.mode):
            w.currentIndexChanged.connect(self.mark_dirty)
        for w in (self.no_limit, self.probe, self.binge, self.auto_skip, self.log):
            w.toggled.connect(self.mark_dirty)
        self.mbps.valueChanged.connect(self.mark_dirty)
        for w in (self.twitch, self.hosts):
            w.textEdited.connect(self.mark_dirty)
        self.subs.list.itemChanged.connect(self.mark_dirty)
        self.subs.list.model().rowsInserted.connect(self.mark_dirty)
        for w in self.adv.values():
            (w.valueChanged if isinstance(w, QSpinBox) else w.textEdited).connect(self.mark_dirty)
        self.load()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.dirty:
            self.load()           # binge mode and auto skip can change by voice meanwhile
        self.check_players()
        self.updates.check()

    def mark_dirty(self, *_):
        if not self._loading and not self.dirty:
            self.dirty = True
            self.panel.dirty_changed()

    def check_players(self):
        """Kodi and Just Player on the TV: installed, version, not disabled."""
        def done(res, err):
            self.packages = res if res and not err and "error" not in res else {}
            self.show_players(err or (res or {}).get("error"))
        self.panel.bg.run(lambda: api("/v1/tv/packages", timeout=25), done)

    def show_players(self, err=None):
        key = self.player.currentData()
        p = self.packages.get(key)
        name = {"kodi": "Kodi", "just": "Just Player"}.get(key, key)
        if not p:
            self.player_state.setText(f"<span style='color:gray'>{name} on the TV: "
                                      + ("can't check (the voice agent or the TV isn't answering)" if err else
                                         "checking…") + "</span>")
        elif not p["installed"]:
            self.player_state.setText(f"<span style='color:{RED}'>✗ {name} isn't installed on the TV</span>")
        elif not p["enabled"]:
            self.player_state.setText(f"<span style='color:{AMBER}'>⚠ {name} {p['version']} is disabled on the TV"
                                      "</span>")
        else:
            self.player_state.setText(f"<span style='color:{GREEN}'>✔ {name} {p['version']} installed on the TV"
                                      "</span>")

    def highlight_keys(self):
        self.ensureWidgetVisible(self.keys_card, 0, 40)
        self.keys_card.setStyleSheet(f"QFrame#card {{ border: 2px solid {AMBER}; }}")
        QTimer.singleShot(2500, lambda: self.keys_card.setStyleSheet(""))

    def set_key(self, what: str):
        """A cloud fallback's key: typed or pasted here, handed to `tv-agent setup <what>` (checked with the service,
        then saved in secrets.toml); the panel keeps nothing."""
        label = {"liquid": "Liquid API key", "openrouter": "OpenRouter API key"}[what]
        key, ok = QInputDialog.getText(self, "TV Agent", f"{label}:", QLineEdit.EchoMode.Password)
        if not ok or not key.strip():
            return
        self.note.setText(f"Checking the {label}…")

        def run():
            r = subprocess.run([str(TV_AGENT), "setup", what], input=key.strip() + "\n", capture_output=True,
                               text=True, timeout=60)
            return r.returncode, "\n".join(l for l in (r.stdout + r.stderr).splitlines() if " INFO " not in l)

        def done(res, err):
            code, out = res if res else (1, str(err))
            if code == 0:
                agent = self.panel.overview.cards["tv-agent"]
                if agent.active:
                    agent.act("restart")
                self.note.setText(f"{label} saved; the voice agent restarted to use it.")
            else:
                self.note.setText(out.splitlines()[-1] if out else f"{label} not saved")
            self.load()

        self.panel.bg.run(run, done)

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
        self._loading = True
        try:
            self._fill(cfg)
        finally:
            self._loading = False
        if self.dirty:
            self.dirty = False
            self.panel.dirty_changed()

    def _fill(self, cfg):
        self._select(self.player, cfg.player)
        self._select(self.height_, cfg.max_height)
        self.no_limit.setChecked(not cfg.max_mbps)
        self.mbps.setValue(cfg.max_mbps or 90)
        self.probe.setChecked(cfg.probe_streams)
        self._select(self.audio, cfg.audio_language)
        self._select(self.native, cfg.native_language)
        self.subs.set_codes(cfg.subtitle_languages)
        self.twitch.setText(", ".join(cfg.twitch_favorites))
        self.log.setChecked(cfg.log_transcripts)
        self.hosts.setText(", ".join(cfg.tv_hosts))
        self.binge.setChecked(cfg.binge)
        self.auto_skip.setChecked(cfg.auto_skip)
        self._select(self.updates.mode, cfg.updates)
        for key, w in self.adv.items():
            value = getattr(cfg, key)
            if isinstance(w, QSpinBox):
                w.setValue(int(value))
            else:
                w.setText(str(value))
        for what, (status, name, used) in self.key_lines.items():
            ok = bool(cfg.secret(f"{what}_api_key"))
            status.setText((f"<span style='color:{GREEN}'>✔</span>&nbsp; <b>{name}</b> key set" if ok else
                            f"<span style='color:{AMBER}'>⚠</span>&nbsp; <b>{name}</b>: no key")
                           + f" <span style='color:gray'>· {used}</span>")

    def values(self) -> dict:
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
        return {
            "player": self.player.currentData(),
            "max_height": int(self.height_.currentData()),
            "max_mbps": 0.0 if self.no_limit.isChecked() else float(self.mbps.value()),
            "probe_streams": self.probe.isChecked(),
            "audio_language": self.audio.currentData(),
            "subtitle_languages": self.subs.codes() or ["eng"],
            "native_language": self.native.currentData(),
            "twitch_favorites": split(self.twitch.text()),
            "log_transcripts": self.log.isChecked(),
            "tv_hosts": split(self.hosts.text()) or agent_config.Config().tv_hosts,
            "binge": self.binge.isChecked(),
            "auto_skip": self.auto_skip.isChecked(),
            "updates": self.updates.mode.currentData(),
            **{k: (w.value() if isinstance(w, QSpinBox) else w.text().strip()) for k, w in self.adv.items()},
        }

    def save(self, values: dict | None = None):
        values = values or self.values()
        path = agent_config.CONFIG_FILE
        old = path.read_text() if path.exists() else None
        try:
            current = tomllib.loads(old) if old else {}
        except ValueError:
            current = {}
        if any(k in current for k in agent_config.LEGACY):        # replaced settings: keep what they meant
            try:
                cfg = load_cfg()
                current.update(decide_steps=cfg.decide_steps, decide_off=cfg.decide_off, d1_where=cfg.d1_where)
            except SystemExit:
                pass
        current = {k: v for k, v in current.items() if k not in agent_config.LEGACY}
        merged = {**current, **values}                        # settings not shown here are kept
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
            self.note.setText(f"<span style='color:{GREEN}'>✓ Settings saved</span> · restarting the voice agent "
                              "to apply them (what's playing carries on)…")
            agent.act("restart")
        else:
            self.note.setText(f"<span style='color:{GREEN}'>✓ Settings saved</span> · they apply the next time the "
                              "voice agent starts")
        if self.dirty and set(values) != set(self.values()):      # the tray's player only: other edits stay
            self._loading = True
            self._select(self.player, values.get("player", self.player.currentData()))
            self._loading = False
        else:
            self.load()
        return True

    def set_kodi_password(self):
        password, ok = QInputDialog.getText(self, "TV Agent", "Kodi web password (Kodi › Settings › Services › "
                                            "Control):", QLineEdit.EchoMode.Password)
        if not ok or not password:
            return
        self.note.setText("Checking the password with Kodi…")

        def run():
            r = subprocess.run([str(TV_AGENT), "setup", "kodi"], input=password + "\n", capture_output=True,
                               text=True, timeout=90)
            return r.returncode, [l for l in (r.stdout + r.stderr).splitlines() if " INFO " not in l][-1:]

        def done(res, err):
            code, out = res if res else (1, [str(err)])
            self.note.setText((out or ["?"])[0])
            if code == 0 and self.panel.overview.cards["tv-agent"].active:
                self.panel.overview.cards["tv-agent"].act("restart")

        self.panel.bg.run(run, done)

    def pair_app(self):
        self.note.setText("Pairing the TV app…")

        def run():
            r = subprocess.run([str(TV_AGENT), "tv", "pair"], capture_output=True, text=True, timeout=60)
            return [l for l in (r.stdout + r.stderr).splitlines() if " INFO " not in l][-1:] or ["done"]

        self.panel.bg.run(run, lambda out, err: self.note.setText((out or [str(err)])[0]))

    # ---- everything in one file ----

    def export_all(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export all settings", str(Path.home() / "tv-agent-settings.json"),
                                              "JSON (*.json)")
        if not path:
            return
        cfg_file = agent_config.CONFIG_FILE
        try:
            config = tomllib.loads(cfg_file.read_text()) if cfg_file.exists() else {}
        except ValueError as e:
            QMessageBox.warning(self, "TV Agent", f"config.toml can't be read: {e}")
            return
        config = {k: v for k, v in config.items() if k not in agent_config.LEGACY}
        rules_file = agent_config.CONFIG_DIR / "rules.json"
        learned_file = agent_config.CONFIG_DIR / "learned.json"
        data = {"tv_agent_settings": 1, "exported": time.strftime("%Y-%m-%d %H:%M"), "config": config,
                "rules": json.loads(rules_file.read_text()) if rules_file.exists() else None,
                "learned": json.loads(learned_file.read_text()) if learned_file.exists() else [],
                "panel": {k: self.panel.prefs.get(k) for k in self.panel.prefs.DEFAULTS}}
        Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False))
        self.note.setText(f"Exported to {path} (API keys and passwords aren't in it)")

    def import_all(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import all settings", str(Path.home()), "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
            assert data.get("tv_agent_settings") == 1 and isinstance(data.get("config"), dict)
        except (OSError, ValueError, AssertionError):
            QMessageBox.warning(self, "TV Agent", "That isn't a file exported from here.")
            return
        rules = "your own rules" if data.get("rules") else "the default rules"
        if QMessageBox.question(self, "TV Agent", f"Replace every setting with the ones exported {data.get('exported')}"
                                f" ({len(data['config'])} settings, {rules}, {len(data.get('learned') or [])} taught "
                                "sentences, this panel's preferences)? API keys and passwords stay as they are.") \
                != QMessageBox.StandardButton.Yes:
            return
        old = agent_config.CONFIG_FILE.read_text() if agent_config.CONFIG_FILE.exists() else None
        lines = ["# tv-agent settings (imported by the TV Agent panel; see config.example.toml for all of them).\n"]
        lines += [f"{k} = {toml(v)}\n" for k, v in data["config"].items()]
        agent_config.CONFIG_FILE.write_text("".join(lines))
        try:
            load_cfg()
        except SystemExit as e:
            if old is None:
                agent_config.CONFIG_FILE.unlink()
            else:
                agent_config.CONFIG_FILE.write_text(old)
            QMessageBox.warning(self, "TV Agent", f"Not imported: {e}")
            return
        (agent_config.CONFIG_DIR / "learned.json").write_text(json.dumps(data.get("learned") or [], indent=1,
                                                                          ensure_ascii=False))
        for k, v in (data.get("panel") or {}).items():
            if k in self.panel.prefs.DEFAULTS and bool(v) != self.panel.prefs.get(k):
                self.set_pref(k, bool(v))

        def rules_done(res, err):
            msg = f"Imported. Rules: {err}" if err else "Imported"
            agent = self.panel.overview.cards["tv-agent"]
            if agent.active:
                agent.act("restart")
                msg += "; the voice agent restarted to apply it"
            self.note.setText(msg)
            self.load()

        if data.get("rules"):
            self.panel.bg.run(lambda: api("/v1/rules", data["rules"], timeout=10), rules_done)
        else:
            self.panel.bg.run(lambda: api("/v1/rules/reset", {}, timeout=10), rules_done)
