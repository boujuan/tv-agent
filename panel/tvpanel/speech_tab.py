"""Decisions › Speech to text: on this laptop (a Parakeet model) or hosted (an OpenRouter transcription model), or
either one with the other as backup; the local model (downloaded here), the hosted one, and keeping recordings for
debugging in the Activity tab. Live at once (/v1/pipeline)."""
import subprocess

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from .common import (AMBER, GREEN, TV_AGENT, WHERE, KeyLine, agent_stt, dim, offer_to_delete_recordings,
                     openrouter_check)


class SpeechToText(QWidget):
    def __init__(self, panel, decisions):
        super().__init__()
        self.panel, self.decisions = panel, decisions
        self.state: dict = {}
        self._checked = ""
        self._dl_status = None
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
        hrow.addWidget(self.vocab_library, 1)
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
            self.vocabulary.setPlainText("\n".join(res.get("stt_vocabulary") or []))
        self.vocab_library.setChecked(bool(res.get("stt_vocabulary_library")))
        assembly = res.get("stt_hosted_model", "") in agent_stt.USES_VOCABULARY
        self.vocab_state.setText(f"{res.get('stt_vocabulary_count', 0)} names sent with each command"
                                 if assembly else "Used by AssemblyAI only: the chosen hosted model doesn't take a list")
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

    def save_vocabulary(self):
        words = [w.strip() for w in self.vocabulary.toPlainText().replace(",", "\n").splitlines() if w.strip()]
        self.decisions.set_pipeline({"stt_vocabulary": words})

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
