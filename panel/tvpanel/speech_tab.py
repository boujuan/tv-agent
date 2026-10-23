"""Decisions › Speech to text: on this laptop (a Parakeet model) or hosted (an OpenRouter transcription model), or
either one with the other as backup; the local model (downloaded here), the hosted one, the vocabulary the hosted one
is sent (all of it in a window of its own), and keeping recordings for debugging in the Activity tab. Live at once
(/v1/pipeline)."""
import html
import subprocess
import urllib.error

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .common import (AMBER, GREEN, RED, TV_AGENT, WHERE, ApiError, KeyLine, agent_config, agent_stt, api, dim,
                     load_cfg, offer_to_delete_recordings, openrouter_check)

VOCABULARY_MAX = getattr(agent_stt, "VOCABULARY_MAX", 200)      # names the hosted model is sent with each command


def clean_names(words) -> list[str]:
    """Names as the agent keeps them: spaces collapsed, up to 100 characters, no repeats (in any case)."""
    out, seen = [], set()
    for w in words:
        w = " ".join(w.split())[:100]
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


class SpeechToText(QWidget):
    def __init__(self, panel, decisions):
        super().__init__()
        self.panel, self.decisions = panel, decisions
        self.state: dict = {}
        self._checked = ""
        self._dl_status = None
        self._vocab_shown = ""                  # the names as last shown in the field (to tell when they're edited)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.where = QComboBox()
        for key, label in WHERE:
            self.where.addItem(label, key)
        self.where.activated.connect(lambda _i: self.where_changed())
        form.addRow("Where", self.where)

        self.local = QComboBox()
        self.local.activated.connect(lambda _i: self.local_changed())
        self.download = QPushButton("Download")
        self.download.clicked.connect(self.download_model)
        folder = QPushButton("Folder…")
        folder.setToolTip("Another sherpa-onnx NeMo transducer model: its folder")
        folder.clicked.connect(self.pick_folder)
        row = QHBoxLayout()
        row.addWidget(self.local, 1)
        row.addWidget(self.download)
        row.addWidget(folder)
        form.addRow("Local model", row)
        self.local_state = dim("", wrap=False)
        form.addRow("", self.local_state)

        self.hosted = QComboBox()
        self.hosted.setEditable(True)                   # any OpenRouter transcription model id
        for model, name in agent_stt.HOSTED.items():
            mark = " **" if model in agent_stt.USES_VOCABULARY else " *" if model in agent_stt.USES_LANGUAGE else ""
            self.hosted.addItem(name + mark, model)
            self.hosted.setItemData(self.hosted.count() - 1, model, 3)      # tooltip: the id
        self.hosted.activated.connect(lambda _i: self.hosted_changed())
        self.hosted.lineEdit().editingFinished.connect(self.hosted_changed)
        form.addRow("Hosted model", self.hosted)
        self.hosted_check = dim("", wrap=False)
        form.addRow("", self.hosted_check)
        form.addRow("", dim("* uses the language hint · ** the language hint and the vocabulary", wrap=False))
        self.key = KeyLine(panel)
        form.addRow("", self.key)
        self.language = QComboBox()
        for code, label in (("native", "Your native language"), ("", "None: the model guesses"), ("es", "Spanish"),
                            ("en", "English")):
            self.language.addItem(label, code)
        self.language.setToolTip("The language the hosted model expects. A Spanish hint still gets English commands "
                                 "right; an English one garbles Spanish; with none it often writes short Spanish "
                                 "commands in another language")
        self.language.activated.connect(lambda _i: self.decisions.set_pipeline(
            {"stt_language": self.language.currentData()}))
        form.addRow("Language hint", self.language)
        self.vocabulary = QPlainTextEdit()
        self.vocabulary.setPlaceholderText("One name per line: Stremio, Better Call Saul…")
        self.vocabulary.setToolTip("Names the hosted model should hear as they're written (AssemblyAI only: other "
                                   "models don't take a list). Few and distinctive works best.")
        self.vocabulary.setFixedHeight(96)
        save = QPushButton("Save vocabulary")
        save.clicked.connect(self.save_vocabulary)
        vrow = QVBoxLayout()
        vrow.addWidget(self.vocabulary)
        hrow = QHBoxLayout()
        self.vocab_library = QCheckBox("Also your library's titles and Twitch favourites")
        self.vocab_library.clicked.connect(lambda on: self.decisions.set_pipeline({"stt_vocabulary_library": on}))
        self.show_all = QPushButton("Show all…")
        self.show_all.setToolTip("Everything sent with each command, in a window of its own: your names, Twitch "
                                 "favourites and your library's titles")
        self.show_all.clicked.connect(self.show_vocabulary)
        hrow.addWidget(self.vocab_library, 1)
        hrow.addWidget(self.show_all)
        hrow.addWidget(save)
        vrow.addLayout(hrow)
        self.vocab_state = dim("", wrap=False)
        vrow.addWidget(self.vocab_state)
        form.addRow("Vocabulary", vrow)
        self.keep = QCheckBox("Keep recordings of commands, for debugging (the last 300, with the history on)")
        self.keep.setToolTip("In Activity, right-click a 🔊 command to hear what the microphone heard, or to try "
                             "another speech model on it and compare (nothing is transcribed twice by itself)")
        self.keep.clicked.connect(self.keep_changed)
        form.addRow("", self.keep)
        lay.addLayout(form)
        lay.addWidget(dim("On this laptop the model listens while you speak, so the command is decided a moment after "
                          "you stop. A hosted model gets the recording when you pause: a few tenths of a second more, "
                          "and a small cost per command (OpenRouter's price per hour of audio)."))
        lay.addStretch()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._show_download)

    # ---- showing ----

    def show_state(self, res: dict):
        self.state = res
        self.where.setCurrentIndex(max(0, self.where.findData(res.get("stt_where", "local"))))
        current = res.get("stt_model", "")
        self.local.blockSignals(True)
        self.local.clear()
        for key, m in agent_stt.MODELS.items():
            self.local.addItem(m.label, key)
        if current not in agent_stt.MODELS:
            self.local.addItem(current, current)
        self.local.setCurrentIndex(max(0, self.local.findData(current)))
        self.local.blockSignals(False)
        self._show_download_button()
        loaded = res.get("stt_loaded")
        self.local_state.setText(f"<span style='color:{GREEN if loaded else AMBER}'>●</span> "
                                 + ("Loaded in the voice agent" if loaded else "Not loaded (loads when it's needed)"))
        hosted = res.get("stt_hosted_model", "")
        i = self.hosted.findData(hosted)
        if i >= 0:
            self.hosted.setCurrentIndex(i)
        else:
            self.hosted.setEditText(hosted)
        self.key.show_key(res.get("keys", {}).get("openrouter", False), "OpenRouter",
                          " (needed for a hosted model)")
        self.keep.setChecked(bool(res.get("keep_recordings")))
        now = res.get("stt_language_now", "")
        self.language.setItemText(0, f"Your native language ({now})" if now else "Your native language")
        self.language.setCurrentIndex(max(0, self.language.findData(res.get("stt_language", "native"))))
        if not self.vocabulary.hasFocus():
            self._vocab_shown = "\n".join(res.get("stt_vocabulary") or [])
            self.vocabulary.setPlainText(self._vocab_shown)
        self.vocab_library.setChecked(bool(res.get("stt_vocabulary_library")))
        assembly = res.get("stt_hosted_model", "") in agent_stt.USES_VOCABULARY
        self.vocab_state.setText(f"{res.get('stt_vocabulary_count', 0)} names sent with each command"
                                 if assembly else "Used by AssemblyAI only: the chosen hosted model doesn't take a list")
        self.show_all.setText(f"Show all ({res.get('stt_vocabulary_count', 0)})…")
        if self._checked != hosted:
            self._checked = hosted
            self.panel.bg.run(lambda: openrouter_check(hosted, "transcription"),
                              lambda r, e: self.hosted_check.setText(
                                  f"<span style='color:{GREEN if r and r[0] else AMBER}'>{'✓' if r and r[0] else '✗'}"
                                  f" {(r or (False, str(e)))[1]}</span>"))

    def _show_download_button(self):
        key = self.local.currentData()
        m = agent_stt.MODELS.get(key)
        self.download.setVisible(bool(m) and not agent_stt.installed(key))
        if m:
            self.download.setText(f"Download ({m.size / 1e6:.0f} MB)")

    # ---- changing ----

    def selected_hosted(self) -> str:
        text = self.hosted.currentText().strip()
        i = self.hosted.findText(text)
        return self.hosted.itemData(i) if i >= 0 else text

    def where_changed(self):
        where = self.where.currentData()
        if where == self.state.get("stt_where"):
            return
        change = {"stt_where": where}
        keys = self.state.get("keys", {})
        if where != "local" and not keys.get("openrouter"):
            QMessageBox.information(self, "TV Agent", "A hosted model needs the OpenRouter key: Settings › API keys. "
                                    "Until then this laptop's model is used.")
        loaded = bool(self.state.get("stt_loaded"))
        if where == "hosted" and loaded:
            box = QMessageBox(QMessageBox.Icon.Question, "TV Agent",
                              "This laptop's speech model is loaded in the voice agent (about 1 GB of memory) and "
                              "isn't used by this choice. Unload it?", parent=self)
            unload = box.addButton("Unload it", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Keep it loaded", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            change["stt_loaded"] = box.clickedButton() is not unload
        elif where != "hosted" and not loaded:
            if not agent_stt.installed(self.local.currentData()):
                QMessageBox.information(self, "TV Agent", "This choice uses this laptop's speech model, which isn't "
                                        "downloaded yet: use Download next to it.")
                change["stt_loaded"] = False
            else:
                role = "as the backup when the hosted one fails" if where == "hosted-first" else "for every command"
                box = QMessageBox(QMessageBox.Icon.Question, "TV Agent",
                                  f"This choice uses this laptop's speech model {role}, and it isn't loaded. Load it "
                                  "now (about 1 GB of memory, a few seconds)? Otherwise it loads the first time it's "
                                  "needed, and that command waits for it.", parent=self)
                load = box.addButton("Load it", QMessageBox.ButtonRole.AcceptRole)
                box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
                box.exec()
                change["stt_loaded"] = box.clickedButton() is load
                if change["stt_loaded"]:
                    self.local_state.setText("Loading the local speech model…")
        self.decisions.set_pipeline(change)

    def local_changed(self):
        key = self.local.currentData()
        self._show_download_button()
        if agent_stt.installed(key):
            self.local_state.setText("Loading the local speech model…")
            self.decisions.set_pipeline({"stt_model": key})

    def hosted_changed(self):
        model = self.selected_hosted()
        if model and model != self.state.get("stt_hosted_model"):
            self.decisions.set_pipeline({"stt_hosted_model": model})

    def show_vocabulary(self):
        dlg = VocabularyDialog(self)
        dlg.exec()
        dlg.deleteLater()

    def field_names(self) -> list[str]:
        return clean_names(self.vocabulary.toPlainText().replace(",", "\n").splitlines())

    def save_vocabulary(self):
        self.decisions.set_pipeline({"stt_vocabulary": self.field_names()})

    def typed_names(self) -> list[str] | None:
        """The names in the field if they were edited and not saved (the window carries on from them), else None."""
        return self.field_names() if self.vocabulary.toPlainText() != self._vocab_shown else None

    def show_saved(self, names: list[str], library: bool, count: int):
        """Saved into config.toml while the voice agent is stopped: shown as written."""
        self._vocab_shown = "\n".join(names)
        self.vocabulary.setPlainText(self._vocab_shown)
        self.vocab_library.setChecked(library)
        self.vocab_state.setText("Saved in the settings file: the voice agent uses it when it starts")
        self.show_all.setText(f"Show all ({count})…")

    def keep_changed(self, on: bool):
        self.decisions.set_pipeline({"keep_recordings": on})
        if not on:
            offer_to_delete_recordings(self)

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "A sherpa-onnx NeMo transducer model's folder")
        if folder:
            if not agent_stt.installed(folder):
                QMessageBox.warning(self, "TV Agent", f"That folder needs {', '.join(agent_stt.FILES)}.")
                return
            self.decisions.set_pipeline({"stt_model": folder})

    def download_model(self):
        key = self.local.currentData()
        m = agent_stt.MODELS[key]
        if QMessageBox.question(self, "TV Agent", f"Download {m.label} ({m.size / 1e6:.0f} MB) from GitHub "
                                f"(k2-fsa/sherpa-onnx), check it against its published SHA-256 and use it?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._dl_status = "Starting the download…"
        self.download.setEnabled(False)
        self._timer.start(400)

        def run():
            p = subprocess.Popen([str(TV_AGENT), "setup", "stt", key], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                if line.strip() and " INFO " not in line:
                    self._dl_status = line.strip()
            return p.wait()

        def done(code, err):
            self._timer.stop()
            self._dl_status = None
            self.download.setEnabled(True)
            if err or code:
                self.local_state.setText(f"Download failed: {err or 'see the panel log'}")
                return
            self.decisions.set_pipeline({"stt_model": key})

        self.panel.bg.run(run, done)

    def _show_download(self):
        if self._dl_status:
            self.local_state.setText(self._dl_status)


class VocabularyDialog(QDialog):
    """Everything the hosted speech model is sent with each command: your names (find, add, remove), then what the
    library switch adds (Twitch favourites and your library's titles, read-only here). Save sends what changed; with
    the voice agent stopped it goes into config.toml, used when the agent starts."""
    GROUPS = {"own": "Your names", "twitch": "Twitch favourites", "titles": "Your library's titles"}

    def __init__(self, speech: SpeechToText):
        super().__init__(speech)
        self.speech, self.panel = speech, speech.panel
        self.setWindowTitle("Speech-to-text vocabulary")
        self.resize(720, 780)
        self.saved: dict = {}                   # as the agent (or config.toml) has it
        self.start: tuple = ()                  # as this window opened: to ask before throwing edits away
        self.own: list[str] = []
        self.twitch: list[str] = []
        self.titles: list[str] | None = None    # the library's titles as sent now (None: not known)
        self.running = True
        self.hosted = ""
        self.groups: dict[str, tuple] = {}      # key: (header item, note item, name items)
        self.saving = self.closed = False
        v = QVBoxLayout(self)
        v.addWidget(dim("Names the hosted speech-to-text should hear as they're written (AssemblyAI models use "
                        "them). Yours go first; few and distinctive works best."))
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.hide()
        v.addWidget(self.message)
        self.library = QCheckBox("Also your library's titles and Twitch favourites")
        self.library.clicked.connect(lambda _on: self.switched())
        v.addWidget(self.library)
        row = QHBoxLayout()
        self.field = QLineEdit()
        self.field.setPlaceholderText("Add a name, or type to find one")
        self.field.setClearButtonEnabled(True)
        self.field.textChanged.connect(lambda _t: self.filter())
        self.field.returnPressed.connect(self.add)
        self.add_btn = QPushButton("Add")
        self.add_btn.setAutoDefault(False)
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self.add)
        row.addWidget(self.field, 1)
        row.addWidget(self.add_btn)
        v.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        probe = QToolButton()
        probe.setText("✕")
        self.line = QSize(0, probe.sizeHint().height())    # every line as tall as one with a remove button
        v.addWidget(self.tree, 1)
        self.total = QLabel("Reading the vocabulary…")
        self.total.setWordWrap(True)
        v.addWidget(self.total)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_btn = bb.button(QDialogButtonBox.StandardButton.Save)
        self.save_btn.setEnabled(False)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.field.setFocus()

        def fetch():
            cfg = load_cfg()
            try:
                return cfg, api("/v1/pipeline", timeout=5), None
            except (OSError, ApiError) as e:
                return cfg, None, e
        self.panel.bg.run(fetch, self.loaded)

    # ---- showing ----

    def loaded(self, r, err):
        if self.closed:                         # closed before the agent answered
            return
        if err:
            self.show_message(RED, f"Couldn't read the settings: {err}")
            self.total.setText("")
            return
        cfg, res, problem = r
        self.twitch = clean_names(cfg.twitch_favorites)
        if res:
            self.saved = {"stt_vocabulary": list(res.get("stt_vocabulary") or []),
                          "stt_vocabulary_library": bool(res.get("stt_vocabulary_library"))}
            self.hosted = res.get("stt_hosted_model", "")
            sent = res.get("stt_vocabulary_sent")
            if self.saved["stt_vocabulary_library"] and sent is not None:
                skip = {n.lower() for n in self.saved["stt_vocabulary"] + self.twitch}
                self.titles = [n for n in sent if n.lower() not in skip]
        else:
            self.running = False
            self.saved = {"stt_vocabulary": list(cfg.stt_vocabulary),
                          "stt_vocabulary_library": bool(cfg.stt_vocabulary_library)}
            self.hosted = cfg.stt_hosted_model
            if isinstance(problem, OSError):
                self.show_message(AMBER, "The voice agent isn't running. Your names are read from the settings file, "
                                         "and Save writes them there: the agent uses them when it starts. Your "
                                         "library's titles show here while it runs.")
            else:
                self.show_message(AMBER, f"The voice agent answered: {problem}. Your names are read from the settings "
                                         "file.")
        typed = self.speech.typed_names()
        self.own = typed if typed is not None else list(self.saved["stt_vocabulary"])
        self.library.setChecked(self.saved["stt_vocabulary_library"])
        self.start = (list(self.own), self.library.isChecked())
        self.fill()
        self.save_btn.setEnabled(True)

    def show_message(self, color: str, text: str):
        self.message.setText(f"<span style='color:{color}'>{html.escape(text, quote=False)}</span>")
        self.message.show()

    def fill(self):
        """The three groups, as known now; the field then filters them."""
        self.tree.clear()
        bold, italic = QFont(self.tree.font()), QFont(self.tree.font())
        bold.setBold(True)
        italic.setItalic(True)
        for key, names in (("own", self.own), ("twitch", self.twitch), ("titles", self.titles or [])):
            group = QTreeWidgetItem(self.tree)
            group.setSizeHint(0, self.line)
            group.setFont(0, bold)
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            group.setFirstColumnSpanned(True)
            note = QTreeWidgetItem(group)       # what the group is, or why it's empty
            note.setSizeHint(0, self.line)
            note.setFont(0, italic)
            note.setFlags(Qt.ItemFlag.NoItemFlags)
            note.setFirstColumnSpanned(True)
            self.groups[key] = (group, note, [])
            for name in names:
                self._item(key, name)
            group.setExpanded(True)
        self.switched()

    def _item(self, key: str, name: str, at: int | None = None) -> QTreeWidgetItem:
        group, _, rows = self.groups[key]
        item = QTreeWidgetItem([name])
        item.setSizeHint(0, self.line)
        at = len(rows) if at is None else at
        group.insertChild(at + 1, item)         # after the note
        rows.insert(at, item)
        if key == "own":
            remove = QToolButton()
            remove.setText("✕")
            remove.setAutoRaise(True)
            remove.setToolTip(f"Remove {name}")
            remove.clicked.connect(lambda _=False, name=name: self.remove(name))
            self.tree.setItemWidget(item, 1, remove)
        return item

    def switched(self):
        """The library switch: Twitch favourites and the titles greyed out while it's off."""
        on = self.library.isChecked()
        for key in ("twitch", "titles"):
            for item in self.groups[key][2]:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled if on else Qt.ItemFlag.NoItemFlags)
        self.filter()

    def filter(self):
        text = " ".join(self.field.text().split()).lower()
        self.add_btn.setEnabled(bool(text and self.groups))
        if not self.groups:
            return
        for key, (group, note, rows) in self.groups.items():
            shown = 0
            for item in rows:
                item.setHidden(bool(text) and text not in item.text(0).lower())
                shown += not item.isHidden()
            known = key != "titles" or self.titles is not None
            count = f" ({shown} of {len(rows)})" if text and rows else f" ({len(rows)})" if known else ""
            group.setText(0, self.GROUPS[key] + count)
            why = self.note(key, bool(text), shown, len(rows))
            note.setText(0, why)
            note.setHidden(not why)
        self.show_total()

    def note(self, key: str, filtering: bool, shown: int, n: int) -> str:
        """The line under a group's header: what it is, or why it's empty ("": none)."""
        if key == "own":
            if not n:
                return "None yet: type one above and press Enter"
            return "None like that yet: Enter adds it" if filtering and not shown else ""
        if not self.library.isChecked():
            return "Not sent while the switch above is off"
        if key == "twitch":
            return ("From Settings › Twitch favourites: change them there" if n
                    else "None: add them in Settings › Twitch favourites")
        if self.titles is None:
            if not self.running:
                return "Read from your library while the voice agent runs"
            if not self.saved.get("stt_vocabulary_library"):
                return "Read from your library once this is saved with the switch on"
            return "This voice agent doesn't list them: update it"
        return ("Films and series you watched most recently, read from your library every 10 minutes" if n
                else "None yet: the agent reads your library in the background, look again in a moment")

    def sent(self) -> int:
        """How many names would go with each command as set here (before the cap)."""
        more = self.twitch + (self.titles or []) if self.library.isChecked() else []
        return len(clean_names(self.own + more))

    def show_total(self):
        on = self.library.isChecked()
        n = self.sent()
        if n > VOCABULARY_MAX:
            text = (f"<b>{VOCABULARY_MAX} of these {n} names sent with each command</b>: the hosted model takes up "
                    f"to {VOCABULARY_MAX}, so the last {n - VOCABULARY_MAX} are left out")
        else:
            text = (f"<b>{n} names sent with each command</b>; the hosted model takes up to {VOCABULARY_MAX}"
                    + (", plus your library's titles" if on and self.titles is None else ""))
        if self.hosted not in agent_stt.USES_VOCABULARY:
            text += (f"<br><span style='color:{AMBER}'>Used by AssemblyAI only: the chosen hosted model doesn't take "
                     "a list</span>")
        self.total.setText(text)

    # ---- changing ----

    def add(self):
        name = " ".join(self.field.text().split())[:100]
        if not name or not self.groups:
            return
        same = next((n for n in self.own if n.lower() == name.lower()), None)
        if same is None:
            self.own.insert(0, name)            # first, where you'll see it
            self._item("own", name, 0)
        self.field.clear()
        item = self.groups["own"][2][self.own.index(same or name)]
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)

    def remove(self, name: str):
        i = self.own.index(name)
        group, _, rows = self.groups["own"]
        del self.own[i]
        group.removeChild(rows.pop(i))
        self.filter()

    def changes(self) -> dict:
        out = {}
        if self.own != self.saved["stt_vocabulary"]:
            out["stt_vocabulary"] = list(self.own)
        if self.library.isChecked() != self.saved["stt_vocabulary_library"]:
            out["stt_vocabulary_library"] = self.library.isChecked()
        return out

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.field.hasFocus():
            return                              # Enter in the field adds the name; it doesn't save
        super().keyPressEvent(e)

    def reject(self):
        if self.saving:
            return
        if self.start and (list(self.own), self.library.isChecked()) != self.start and QMessageBox.question(
                self, "TV Agent", "Throw away the changes to the vocabulary?") != QMessageBox.StandardButton.Yes:
            return
        super().reject()

    def done(self, result):
        self.closed = True
        super().done(result)

    def save(self):
        change = self.changes()
        if not change:
            self.accept()
            return
        self.save_btn.setEnabled(False)
        self.saving = True
        count = min(self.sent(), VOCABULARY_MAX)

        def run():
            try:
                return api("/v1/pipeline", change, timeout=10)
            except urllib.error.URLError as e:
                if not isinstance(e.reason, ConnectionRefusedError):
                    raise
            cfg = load_cfg()                    # the voice agent isn't running: written as it would write them
            for key, value in change.items():
                agent_config.set_value(cfg, key, value)
            return None

        def saved(res, err):
            self.saving = False
            if err:
                self.save_btn.setEnabled(True)
                self.show_message(RED, f"Not saved: {err}")
                return
            if res:
                self.speech.decisions.show_pipeline(res, None)
                self.panel.say(f"<span style='color:{GREEN}'>✓ Vocabulary saved · live now</span>")
                if change.get("stt_vocabulary_library"):        # the library is read in the background
                    QTimer.singleShot(3000, self.speech.decisions.load_pipeline)
            else:
                self.speech.show_saved(self.own, self.library.isChecked(), count)
                self.panel.say("Vocabulary saved in the settings file: the voice agent uses it when it starts")
            self.accept()

        self.panel.bg.run(run, saved)
