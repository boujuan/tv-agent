"""Commands tab: everything you can say, by section, as patterns with ‹placeholders› (tv_agent/commands.py, the same
list the TV shows for "what can you do?"), in English or Spanish; the ones the LLM step decides are marked. Type to
filter; "try" fills a pattern in with an example you can edit, then explains it (nothing sent) or does it on the
TV."""
import html
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QTextBrowser, QVBoxLayout, QWidget)

from .common import BLUE, GREEN, GREY, PURPLE, RED, api, card, dim

try:
    from tv_agent.commands import LEGEND, SECTIONS, expand
except ImportError:                       # an agent from before the list
    LEGEND, SECTIONS, expand = {"en": "", "es": ""}, [], None


def pattern_html(text: str) -> str:
    """A pattern with its placeholders and optional parts set apart."""
    out = html.escape(text)
    out = re.sub(r"‹([^›]+)›", rf"<span style='color:{BLUE}'><i>‹\1›</i></span>", out)
    out = re.sub(r"\[([^\]]*)\]", rf"<span style='color:{GREY}'>[\1]</span>", out)
    return out.replace(" · ", f" <span style='color:{GREY}'>·</span> ")


class TryDialog(QDialog):
    """A command filled in with an example: edit it, then explain it or do it."""

    def __init__(self, parent: QWidget, pattern: str, examples: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Try a command")
        self.choice = ""
        v = QVBoxLayout(self)
        v.addWidget(QLabel(pattern_html(pattern)))
        self.text = QComboBox()
        self.text.setEditable(True)
        self.text.addItems(examples)
        self.text.setMinimumWidth(460)
        v.addWidget(self.text)
        v.addWidget(dim("Edit it as you like (another title, another number…)."))
        bb = QDialogButtonBox()
        explain = bb.addButton("Explain (nothing is sent)", QDialogButtonBox.ButtonRole.AcceptRole)
        do = bb.addButton("Do it on the TV", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        explain.clicked.connect(lambda: self.done_with("explain"))
        do.clicked.connect(lambda: self.done_with("do"))
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def done_with(self, what: str):
        self.choice = what
        self.accept()


class Commands(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.lang = "en"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)
        box = card()
        v = QVBoxLayout(box)
        v.setContentsMargins(16, 12, 16, 12)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>What you can say</b>"))
        self.langs = QButtonGroup(self)
        for code, label in (("en", "English"), ("es", "Español")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(code == "en")
            b.clicked.connect(lambda _=False, c=code: self.set_lang(c))
            self.langs.addButton(b)
            top.addWidget(b)
        top.addStretch()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter: a word in English or Spanish")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.render)
        top.addWidget(self.filter, 1)
        v.addLayout(top)
        self.legend = dim("")
        v.addWidget(self.legend)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self.try_it)
        v.addWidget(self.view, 1)
        footer = dim("Say it after pressing the remote's mic button, or the phone's. Spanish needs a speech model "
                     "that knows it: a hosted one, or Parakeet v3 on this laptop (<a href='stt'>Decisions › Speech to "
                     "text</a>). Other phrasings often work too: the LLM and the decision model decide what the rules "
                     f"don't; <span style='color:{PURPLE}'>LLM</span> marks the ones only the LLM decides (it needs "
                     "the OpenRouter key). On the TV, “what can you do?” shows this list in the language you ask in.")
        footer.setTextFormat(Qt.TextFormat.RichText)
        footer.linkActivated.connect(lambda _link: self.open_speech())
        v.addWidget(footer)
        self.reply = QLabel("")
        self.reply.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.reply)
        lay.addWidget(box, 1)
        self.render()

    def set_lang(self, lang: str):
        self.lang = lang
        for b, code in zip(self.langs.buttons(), ("en", "es")):
            b.setChecked(code == lang)
        self.render()

    def render(self, *_):
        want = self.filter.text().strip().lower()
        lang = self.lang
        self.legend.setText(LEGEND[lang])
        rows = []
        for si, section in enumerate(SECTIONS):
            shown = []
            for ci, c in enumerate(section.commands):
                words = " ".join((c.en, c.es, c.does_en, c.does_es, section.en, section.es)).lower()
                if want and want not in words:
                    continue
                does = html.escape(c.does(lang))
                if getattr(c, "by", "rules") == "llm":
                    does += f" <span style='color:{PURPLE}'>LLM</span>"
                shown.append(f"<tr><td style='padding:3px 18px 3px 0'>{pattern_html(c.say(lang))}</td>"
                             f"<td style='padding:3px 12px 3px 0; color:{GREY}'>{does}</td>"
                             f"<td style='padding:3px 0'><a href='try:{si}:{ci}'>try</a></td></tr>")
            if shown:
                rows.append(f"<tr><td colspan='3' style='padding:12px 0 4px 0'><b>{html.escape(section.name(lang))}"
                            f"</b></td></tr>" + "".join(shown))
        body = "".join(rows) or f"<tr><td style='color:{GREY}'>Nothing matches “{html.escape(want)}”.</td></tr>"
        self.view.setHtml(f"<table cellspacing='0' width='100%'>{body}</table>")

    def open_speech(self):
        """Decisions › Speech to text, where the speech models are chosen."""
        d = self.panel.decisions
        self.panel.tabs.setCurrentWidget(d)
        if not d.loaded:
            d.load()
        d.node_clicked("stt")

    def try_it(self, url):
        _, si, ci = url.toString().split(":")
        c = SECTIONS[int(si)].commands[int(ci)]
        examples = expand(c.say(self.lang), self.lang) if expand else [c.say(self.lang)]
        dlg = TryDialog(self, c.say(self.lang), examples)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.text.currentText().strip():
            return
        text = dlg.text.currentText().strip()
        if dlg.choice == "explain":
            self.panel.show_decisions(text)
            return
        self.reply.setText("…")

        def done(r, err):
            if err or not r:
                self.reply.setText(f"<span style='color:{RED}'>The voice agent isn't answering</span>")
                return
            color = GREEN if r.get("ok") else RED
            self.reply.setText(f"“{html.escape(text)}” → <span style='color:{color}'><b>{html.escape(r.get('say', ''))}"
                               "</b></span>")

        self.panel.bg.run(lambda: api("/v1/text", {"text": text, "source": "text"}, timeout=90), done)
