"""Commands tab: everything you can say, by topic, in English and Spanish: tv_agent/commands.py, the same list the TV
shows for "what can you do?". Type to filter; right-click an example to copy it or try it on the TV."""
import textwrap

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont, QGuiApplication
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .common import GREEN, RED, api, card, dim

try:
    from tv_agent.commands import COMMANDS
except ImportError:                       # an agent from before the list
    COMMANDS = []


class Commands(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)
        box = card()
        v = QVBoxLayout(box)
        v.setContentsMargins(16, 12, 16, 12)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>What you can say</b>"))
        top.addStretch()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter: a word in English or Spanish")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.apply_filter)
        top.addWidget(self.filter, 1)
        v.addLayout(top)
        v.addWidget(dim("Say it after pressing the remote's mic button, or try it here (right-click). Spanish needs "
                        "the multilingual speech model (Settings › Voice). Other phrasings often work too: d1 and, "
                        "when set up, the LLM step decide what the rules don't. On the TV: “what can you do?”."))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["You say", "En español", "What it does"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(False)          # rows as tall as their lines (wrapped below, not by Qt)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.menu)
        hh = self.tree.header()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        bold = QFont()
        bold.setBold(True)
        for topic, rows in COMMANDS:
            head = QTreeWidgetItem([topic])
            head.setFont(0, bold)
            head.setFirstColumnSpanned(True)
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(head)
            for en, es, does in rows:
                # One example a line, the description wrapped: rows grow to fit (Qt doesn't wrap them itself)
                item = QTreeWidgetItem(["\n".join(en), "\n".join(es), textwrap.fill(does, 38)])
                item.setData(0, Qt.ItemDataRole.UserRole, (en, es))
                for c in range(3):
                    item.setToolTip(c, item.text(c))
                head.addChild(item)
        self.tree.expandAll()
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 300)
        v.addWidget(self.tree, 1)
        self.reply = QLabel("")
        self.reply.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.reply)
        lay.addWidget(box, 1)

    def apply_filter(self, text: str):
        want = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            shown = 0
            for j in range(head.childCount()):
                child = head.child(j)
                hit = not want or want in head.text(0).lower() or any(want in child.text(c).lower() for c in range(3))
                child.setHidden(not hit)
                shown += hit
            head.setHidden(shown == 0)

    def menu(self, pos):
        item = self.tree.itemAt(pos)
        examples = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not examples:
            return
        m = QMenu(self)
        for text in examples[0] + examples[1]:
            sub = m.addMenu(f"“{text}”")
            copy = QAction("Copy", sub)
            copy.triggered.connect(lambda _=False, t=text: QGuiApplication.clipboard().setText(t))
            run = QAction("Try it on the TV", sub)
            run.triggered.connect(lambda _=False, t=text: self.try_it(t))
            sub.addAction(copy)
            sub.addAction(run)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def try_it(self, text: str):
        if QMessageBox.question(self, "TV Agent", f"Do “{text}” on the TV now?") != QMessageBox.StandardButton.Yes:
            return
        self.reply.setText("…")

        def done(r, err):
            if err or not r:
                self.reply.setText(f"<span style='color:{RED}'>The voice agent isn't answering</span>")
                return
            color = GREEN if r.get("ok") else RED
            self.reply.setText(f"“{text}” → <span style='color:{color}'><b>{r.get('say', '')}</b></span>")

        self.panel.bg.run(lambda: api("/v1/text", {"text": text, "source": "text"}, timeout=90), done)
