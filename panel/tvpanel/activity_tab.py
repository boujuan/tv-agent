"""Activity tab: give a command (typed, or spoken into the laptop's mic), see what was said and done, and
export, import or erase that history (~/.local/share/tv-agent/activity.jsonl). Commands marked “that's wrong” (by
voice or here) can be taught the right action: that sentence then does it (learned.json)."""
import csv
import json
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .common import AMBER, GREEN, RED, LevelBar, agent_activity, api, card, dim, journal_history

SOURCES = {"tv": "📺 Remote", "laptop": "💻 Laptop mic", "text": "⌨ Typed", "mcp": "🤖 Claude", "unknown": "·"}
FIELDS = ["t", "source", "heard", "type", "via", "rule", "confidence", "ok", "say", "ms"]


def describe_how(e: dict) -> str:
    via = {"phrase": "fixed phrase", "pattern": "pattern", "d1": "d1 model", "grammar": "fixed phrase",
           "cue": "pattern"}.get(e.get("via", ""), e.get("via", ""))
    rule = f" {e['rule']}" if e.get("rule") and e.get("via") != "d1" else ""
    conf = f" {e.get('confidence', 0):.2f}" if e.get("via") == "d1" else ""
    return f"{str(e.get('type', '')).replace('_', ' ')} · {via}{rule}{conf}"


class Activity(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.rows: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        tryit = card()
        t = QVBoxLayout(tryit)
        t.setContentsMargins(16, 12, 16, 12)
        t.addWidget(QLabel("<b>Give a command</b> <span style='color:gray'>· typed, or spoken into this laptop's "
                           "microphone: it does the same as the remote's mic button</span>"))
        row = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("e.g. pause · volume 15 · play the bear season 3 episode 2 · go home")
        self.cmd.returnPressed.connect(self.send)
        send = QPushButton("Send")
        send.clicked.connect(self.send)
        self.speak = QPushButton("🎤  Speak")
        self.speak.setToolTip("Records until you stop talking (press again to send right away)")
        self.speak.clicked.connect(panel.mic.start)
        row.addWidget(self.cmd, 1)
        row.addWidget(send)
        row.addWidget(self.speak)
        self.level = LevelBar()
        self.level.setVisible(False)
        self.reply = QLabel("")
        self.reply.setWordWrap(True)
        self.reply.setTextFormat(Qt.TextFormat.RichText)
        t.addLayout(row)
        t.addWidget(self.level)
        t.addWidget(self.reply)
        lay.addWidget(tryit)
        panel.mic.level.connect(self.level.set)
        panel.mic.status.connect(self.mic_status)
        panel.mic.result.connect(self.show_result)

        hist = card()
        h = QVBoxLayout(hist)
        h.setContentsMargins(16, 12, 16, 12)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>History</b>"))
        self.count = dim("")
        top.addWidget(self.count)
        top.addStretch()
        self.only_wrong = QCheckBox("Only wrong or not understood")
        self.only_wrong.setToolTip("Commands you said “that's wrong” to, and ones it didn't get: teach them "
                                   "(right-click)")
        self.only_wrong.toggled.connect(lambda _: self.load())
        top.addWidget(self.only_wrong)
        for label, fn, tip in (("Refresh", self.load, ""), ("Export…", self.export, "Save as JSON lines or CSV"),
                               ("Import…", self.import_, "Add entries from an exported file"),
                               ("Erase…", self.erase, "Delete the whole history")):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            top.addWidget(b)
        h.addLayout(top)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["When", "From", "You said", "Understood as", "Result"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.row_menu)
        self.table.doubleClicked.connect(lambda idx: self.explain_row(idx.row()))
        hh = self.table.horizontalHeader()
        for c, mode in enumerate((QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.ResizeToContents,
                                  QHeaderView.ResizeMode.Stretch, QHeaderView.ResizeMode.ResizeToContents,
                                  QHeaderView.ResizeMode.Stretch)):
            hh.setSectionResizeMode(c, mode)
        h.addWidget(self.table)
        h.addWidget(dim("Double-click a line to see in Decisions how it was decided. Right-click to mark it "
                        "wrong or teach it the right action (saying “that's wrong” marks the last one)."))
        lay.addWidget(hist, 3)

        lat = card()
        lg = QVBoxLayout(lat)
        lg.setContentsMargins(16, 12, 16, 12)
        lg.addWidget(QLabel("<b>Response times</b> <span style='color:gray'>· since the voice agent started</span>"))
        self.stats = QLabel("–")
        self.stats.setTextFormat(Qt.TextFormat.RichText)
        lg.addWidget(self.stats)
        lay.addWidget(lat, 1)
        self.migrate_once()

    # ---- giving commands ----

    def send(self):
        text = self.cmd.text().strip()
        if not text:
            return
        self.reply.setText("…")
        self.panel.bg.run(lambda: api("/v1/text", {"text": text}, timeout=90), lambda r, e: self.show_result(
            r if r else {"error": "The voice agent isn't answering. Is it running?" if isinstance(e, OSError)
                         else str(e)}))

    def mic_status(self, s: str):
        self.level.setVisible(s == "listening")
        self.speak.setText({"listening": "■  Send now", "sending": "…"}.get(s, "🎤  Speak"))
        if s == "listening":
            self.reply.setText("<span style='color:gray'>Listening… speak now (it stops when you stop)</span>")
        elif s == "sending":
            self.reply.setText("<span style='color:gray'>Understanding…</span>")

    def show_result(self, r: dict):
        if r.get("error"):
            self.reply.setText(f"<span style='color:{RED}'>{r['error']}</span>")
        else:
            heard = f"“{r['heard']}” → " if r.get("heard") else ""
            color = GREEN if r.get("ok") else AMBER
            self.reply.setText(f"{heard}<span style='color:{color}'><b>{r.get('say', '')}</b></span> "
                               f"<span style='color:gray'>· {describe_how(r | {'type': r['action']['type']})}"
                               f"</span>")
        self.load()

    # ---- history ----

    def migrate_once(self):
        """The first time, bring over commands from before this log existed (the service journal)."""
        if agent_activity.FILE.exists():
            self.load()
            return

        def done(rows, err):
            if rows:
                agent_activity.merge(rows)
            else:
                agent_activity.erase()       # creates the empty file, so this happens once
            self.load()

        self.panel.bg.run(journal_history, done)

    def load(self):
        def done(rows, err):
            rows = rows or []
            if self.only_wrong.isChecked():
                rows = [e for e in rows if (e.get("wrong") or e.get("type") == "none") and e.get("heard", "").strip()]
            self.rows = rows
            self.count.setText(f"· {len(self.rows)} command{'s' if len(self.rows) != 1 else ''}")
            shown = self.rows[:500]
            self.table.setRowCount(len(shown))
            for i, e in enumerate(shown):
                when = time.strftime("%a %d %b %H:%M:%S", time.localtime(e["t"]))
                result = (e.get("say") or "") + (f"  ({e['ms']} ms)" if e.get("ms") else "")
                heard = e.get("heard", "")
                if e.get("taught"):
                    heard = f"✓ {heard}   (taught: {e['taught'].replace('_', ' ')})"
                elif e.get("wrong"):
                    heard = f"✗ {heard}"
                vals = (when, SOURCES.get(e.get("source", "unknown"), e.get("source", "")), heard,
                        describe_how(e), result)
                for j, v in enumerate(vals):
                    item = QTableWidgetItem(v)
                    if j == 4 and e.get("ok") is False:
                        item.setForeground(QColor(AMBER))
                    if j == 2 and e.get("wrong"):
                        item.setForeground(QColor(RED))
                    self.table.setItem(i, j, item)

        self.panel.bg.run(agent_activity.read, done)

        def stats_done(res, err):
            if err or not res:
                self.stats.setText("<span style='color:gray'>No data yet (the voice agent is stopped or hasn't "
                                   "been used since it started)</span>")
                return
            names = {"stt": "Speech to text", "decide:phrase": "Decide · fixed phrase", "decide:pattern":
                     "Decide · pattern rule", "decide:d1": "Decide · d1 model", "decide:none": "Decide · nothing"}
            rows = "".join(
                f"<tr><td>{names.get(k, k.replace('execute:', 'Do · ').replace('_', ' '))}</td>"
                f"<td align=right>&nbsp;&nbsp;{v['n']}×</td><td align=right>&nbsp;&nbsp;{v['p50']:.0f} ms</td>"
                f"<td align=right>&nbsp;&nbsp;{v['p95']:.0f} ms</td></tr>" for k, v in sorted(res.items()))
            self.stats.setText("<table><tr><td></td><td align=right style='color:gray'>&nbsp;&nbsp;count</td>"
                               "<td align=right style='color:gray'>&nbsp;&nbsp;typical</td>"
                               f"<td align=right style='color:gray'>&nbsp;&nbsp;slowest 5%</td></tr>{rows}</table>")

        self.panel.bg.run(lambda: api("/v1/stats", timeout=3), stats_done)

    def row_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.rows):
            return
        m = QMenu(self)
        explain = QAction("Explain in Decisions", m)
        explain.triggered.connect(lambda: self.explain_row(row))
        again = QAction("Run it again", m)
        again.triggered.connect(lambda: (self.cmd.setText(self.rows[row].get("heard", "")), self.send()))
        copy = QAction("Copy what was said", m)
        copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(self.rows[row].get("heard", "")))
        e = self.rows[row]
        teach = QAction("Teach the right action…", m)
        teach.triggered.connect(lambda: self.teach(e))
        wrong = QAction("Not wrong after all" if e.get("wrong") else "Mark as wrong", m)
        wrong.triggered.connect(lambda: (agent_activity.mark(float(e["t"]), not e.get("wrong"), heard=e.get("heard")),
                                         self.load()))
        forget = QAction("Forget what it was taught", m)
        forget.triggered.connect(lambda: self.forget(e))
        for a in (explain, again, copy):
            m.addAction(a)
        m.addSeparator()
        for a in (teach, wrong) + ((forget,) if e.get("taught") else ()):
            m.addAction(a)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def teach(self, e: dict):
        """“When I say this, do what that command does”: the agent decides the command typed here, and once you
        agree, the sentence said is saved with that action (learned.json, checked before the rules)."""
        heard = e.get("heard", "").strip()
        if not heard:
            return
        meant, ok = QInputDialog.getText(self, "Teach the right action",
                                         f"When I say “{heard}”, do what this command does\n"
                                         "(say it the way the agent understands, e.g. “pause”, "
                                         "“play the bear season 3”):")
        if not ok or not meant.strip():
            return
        try:
            d = api("/v1/decide", {"text": meant.strip()})
        except Exception as err:
            QMessageBox.warning(self, "TV Agent", f"The voice agent isn't answering: {err}")
            return
        if d["action"]["type"] == "none":
            QMessageBox.information(self, "TV Agent", f"“{meant}” isn't understood either; say it another way.")
            return
        if QMessageBox.question(self, "TV Agent", f"From now on, “{heard}” will do: {d.get('describe', '')} "
                                f"({d['action']['type'].replace('_', ' ')}). Save?") != QMessageBox.StandardButton.Yes:
            return
        try:
            api("/v1/learn", {"say": heard, "do": d["action"], "t": e["t"], "heard": e.get("heard")})
        except Exception as err:
            QMessageBox.warning(self, "TV Agent", f"Not saved: {err}")
            return
        self.reply.setText(f"<span style='color:{GREEN}'>Taught: “{heard}” → {d.get('describe', '')}</span>")
        self.load()

    def forget(self, e: dict):
        try:
            api("/v1/learned/remove", {"say": e.get("heard", "")})
        except Exception as err:
            QMessageBox.warning(self, "TV Agent", f"Not removed: {err}")
            return
        agent_activity.mark(float(e["t"]), False, heard=e.get("heard"), taught=None)
        self.load()

    def explain_row(self, row: int):
        if 0 <= row < len(self.rows):
            self.panel.show_decisions(self.rows[row].get("heard", ""))

    def export(self):
        path, kind = QFileDialog.getSaveFileName(self, "Export history", "tv-agent-activity.jsonl",
                                                 "JSON lines (*.jsonl);;CSV (*.csv)")
        if not path:
            return
        rows = agent_activity.read()
        if path.endswith(".csv") or "CSV" in kind:
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                w.writeheader()
                for e in rows:
                    w.writerow({**e, "t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["t"]))})
        else:
            with open(path, "w") as f:
                for e in reversed(rows):
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        self.reply.setText(f"<span style='color:gray'>Exported {len(rows)} commands to {path}</span>")

    def import_(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import history", "", "History (*.jsonl *.json *.csv)")
        if not path:
            return
        try:
            entries = read_any(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "TV Agent", f"Couldn't read that file: {e}")
            return
        added = agent_activity.merge(entries)
        self.reply.setText(f"<span style='color:gray'>Imported {added} new of {len(entries)} commands</span>")
        self.load()

    def erase(self):
        if QMessageBox.question(self, "TV Agent", f"Delete all {len(self.rows)} commands from the history?\n"
                                "(Export first if you want a copy.)") != QMessageBox.StandardButton.Yes:
            return
        agent_activity.erase()
        self.load()


def read_any(path: str) -> list[dict]:
    """Entries from an export: JSON lines, a JSON array, or the CSV this panel writes."""
    text = open(path, encoding="utf-8").read()
    if path.endswith(".csv"):
        out = []
        for r in csv.DictReader(text.splitlines()):
            e = {k: v for k, v in r.items() if k in FIELDS and v not in ("", None)}
            e["t"] = time.mktime(time.strptime(r["t"], "%Y-%m-%d %H:%M:%S"))
            for k in ("confidence",):
                if k in e:
                    e[k] = float(e[k])
            if "ms" in e:
                e["ms"] = int(float(e["ms"]))
            if "ok" in e:
                e["ok"] = e["ok"] == "True"
            out.append(e)
        return out
    text = text.strip()
    if text.startswith("["):
        return [e for e in json.loads(text) if isinstance(e, dict) and "t" in e]
    return [json.loads(line) for line in text.splitlines() if line.strip()]
