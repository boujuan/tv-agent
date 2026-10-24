"""Activity tab: give a command (typed, or spoken into the laptop's mic), see what was said and done, and
export, import or erase that history (~/.local/share/tv-agent/activity.jsonl). Commands marked “that's wrong” (by
voice or here) can be taught the right action: that sentence then does it (learned.json). Each command's timings
(speech-to-text, deciding, after your last word, doing it) and model costs are drawn below; with recordings kept,
a command's audio can be played and transcribed again by another model."""
import csv
import html
import json
import math
import statistics
import subprocess
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QToolTip,
                               QVBoxLayout, QWidget)

from .common import (AMBER, BLUE, GREEN, GREY, RED, LevelBar, agent_activity, agent_stt, api, card, dim,
                     journal_history, offer_to_delete_recordings)

SOURCES = {"tv": "📺 Remote", "laptop": "💻 Laptop mic", "phone": "📱 Phone", "text": "⌨ Typed", "mcp": "🤖 Claude",
           "unknown": "·"}
FIELDS = ["t", "source", "heard", "type", "via", "rule", "confidence", "ok", "say", "ms"]


def describe_how(e: dict) -> str:
    via = {"phrase": "fixed phrase", "pattern": "pattern", "d1": "d1 model", "grammar": "fixed phrase",
           "cue": "pattern", "llm": "LLM", "split": "two commands"}.get(e.get("via", ""), e.get("via", ""))
    rule = f" {e['rule']}" if e.get("rule") and e.get("via") != "d1" else ""
    conf = f" {e.get('confidence', 0):.2f}" if e.get("via") == "d1" else ""
    return f"{str(e.get('type', '')).replace('_', ' ')} · {via}{rule}{conf}"


def via_label(e: dict) -> str:
    return {"phrase": "rules", "pattern": "rules", "llm": "LLM", "d1": "decision model", "split": "two commands",
            "remote": "remote", "none": "nothing"}.get(e.get("via", ""), e.get("via", ""))


def stt_name(stt: str | None) -> str:
    return "this laptop" if stt == "local" else agent_stt.HOSTED.get(stt or "", stt or "?")


class TimingChart(QWidget):
    """Each command as two bars: understanding it (speech-to-text, deciding, waiting for your pause; from your last
    word when spoken) and doing it. Newest on the right; hover one for its numbers."""
    PARTS = (("stt", GREEN, "speech to text"), ("decide", BLUE, "deciding"), ("wait", GREY, "waiting for your pause"))

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.setMinimumSize(440, 170)
        self.setMouseTracking(True)

    def set_rows(self, rows: list[dict]):
        self.rows = rows[-48:]
        self.update()

    @staticmethod
    def parts(e: dict) -> dict:
        stt, decide = (e.get("stt_ms") or 0) + (e.get("stt_failed_ms") or 0), e.get("decide_ms") or 0
        after = e.get("after_speech_ms")
        wait = max(0, after - stt - decide) if after else 0
        return {"stt": stt, "decide": decide, "wait": wait}

    GAP = 56                                    # between the plots: room for the second one's axis labels

    def _geometry(self):
        w, h = self.width(), self.height()
        left, bottom, top = 46, 18, 16
        half = (w - left - self.GAP - 8) / 2
        return left, bottom, top, half, h - bottom - top

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = self.palette().text().color()
        faint = QColor(text)
        faint.setAlpha(60)
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)
        left, bottom, top, half, plot_h = self._geometry()
        if not self.rows:
            p.setPen(faint)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No commands yet")
            return
        understand = [sum(self.parts(e).values()) for e in self.rows]
        doing = [e.get("ms") or 0 for e in self.rows]
        for i, (title, values, x0) in enumerate((("Understanding it (ms)", understand, left),
                                                 ("Doing it (s)", doing, left + half + self.GAP))):
            top_val = max(values) or 1
            scale = 10 ** math.floor(math.log10(top_val))
            top_val = math.ceil(top_val / scale) * scale
            p.setPen(text)
            p.drawText(QPointF(x0, top - 4), title)
            for frac in (0, 0.5, 1):
                y = top + plot_h * (1 - frac)
                p.setPen(QPen(faint, 1))
                p.drawLine(QPointF(x0, y), QPointF(x0 + half, y))
                v = top_val * frac
                label = f"{v:.0f}" if i == 0 else f"{v / 1000:.1f}"
                p.setPen(faint if frac else text)
                p.drawText(QRectF(x0 - 44, y - 8, 40, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           label)
            bw = half / max(len(self.rows), 12)
            for j, e in enumerate(self.rows):
                x = x0 + j * bw + bw * 0.15
                y = top + plot_h
                if i == 0:
                    for key, color, _ in self.PARTS:
                        v = self.parts(e)[key]
                        hgt = plot_h * v / top_val
                        p.fillRect(QRectF(x, y - hgt, bw * 0.7, hgt), QColor(color))
                        y -= hgt
                else:
                    hgt = plot_h * doing[j] / top_val
                    p.fillRect(QRectF(x, y - hgt, bw * 0.7, hgt), QColor(GREEN if e.get("ok") else AMBER))
        x = left
        for key, color, label in self.PARTS:
            p.fillRect(QRectF(x, self.height() - 12, 9, 9), QColor(color))
            p.setPen(text)
            p.drawText(QPointF(x + 13, self.height() - 3), label)
            x += 14 + p.fontMetrics().horizontalAdvance(label) + 14

    def mouseMoveEvent(self, ev):
        left, bottom, top, half, plot_h = self._geometry()
        x = ev.position().x()
        for x0 in (left, left + half + self.GAP):
            if x0 <= x <= x0 + half and self.rows:
                bw = half / max(len(self.rows), 12)
                j = int((x - x0) // bw)
                if 0 <= j < len(self.rows):
                    e = self.rows[j]
                    q = self.parts(e)
                    lines = [f"“{e.get('heard', '')}”", time.strftime("%a %H:%M:%S", time.localtime(e["t"])),
                             f"decided by {via_label(e)} in {q['decide']:.0f} ms"]
                    if e.get("stt_ms"):
                        lines.append(f"speech to text {e['stt_ms']} ms ({stt_name(e.get('stt'))})")
                    if e.get("stt_error"):
                        lines.append(f"hosted speech-to-text failed after {e.get('stt_failed_ms', '?')} ms")
                    if e.get("after_speech_ms"):
                        lines.append(f"{e['after_speech_ms']} ms after your last word")
                    if e.get("ms") is not None:
                        lines.append(f"done in {e['ms'] / 1000:.1f} s: {e.get('say', '')}")
                    if e.get("cost") or e.get("stt_cost"):
                        lines.append(f"cost ${(e.get('cost') or 0) + (e.get('stt_cost') or 0):.5f}")
                    QToolTip.showText(ev.globalPosition().toPoint(), "\n".join(lines), self)
                    return
        QToolTip.hideText()


def percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[max(0, math.ceil(q * len(values)) - 1)]


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
        self.keep = QCheckBox("Keep recordings")
        self.keep.setToolTip("Each spoken command's audio is kept (the last 300): right-click a 🔊 line to play it "
                             "or transcribe it again with another model")
        self.keep.clicked.connect(self.keep_changed)
        top.addWidget(self.keep)
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
        h.addWidget(dim("Double-click a line to see in Decisions what happened: how it was decided, how its title "
                        "was found and how it started, step by step. Right-click to decide it again now, mark it "
                        "wrong or teach it the right action (saying “that's wrong” marks the last one)."))
        lay.addWidget(hist, 3)

        lat = card()
        lg = QVBoxLayout(lat)
        lg.setContentsMargins(16, 12, 16, 12)
        lg.addWidget(QLabel("<b>Response times</b> <span style='color:gray'>· the last commands in the history; "
                            "hover a bar for its numbers</span>"))
        body = QHBoxLayout()
        self.chart = TimingChart()
        body.addWidget(self.chart, 1)
        self.stats = QLabel("–")
        self.stats.setTextFormat(Qt.TextFormat.RichText)
        self.stats.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.addWidget(self.stats)
        lg.addLayout(body)
        lay.addWidget(lat, 2)
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
                if e.get("recording"):
                    heard = f"🔊 {heard}"
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

        self.panel.bg.run(lambda: agent_activity.read(limit=300), self.show_times)
        self.panel.bg.run(lambda: api("/v1/pipeline", timeout=3),
                          lambda r, e: self.keep.setChecked(bool(r and r.get("keep_recordings"))))

    def show_times(self, rows, err):
        rows = list(reversed(rows or []))          # oldest first
        self.chart.set_rows([e for e in rows if e.get("type") != "none" or e.get("heard")])
        if not rows:
            self.stats.setText("")                 # the chart says it
            return

        def line(label: str, values: list[float], unit="ms"):
            values = [v for v in values if v]
            if not values:
                return ""
            f = (lambda v: f"{v:.0f} ms") if unit == "ms" else (lambda v: f"{v / 1000:.1f} s")
            return (f"<tr><td>{label}</td><td align=right>&nbsp;{f(statistics.median(values))}</td>"
                    f"<td align=right>&nbsp;{f(percentile(values, 0.95))}</td>"
                    f"<td align=right style='color:gray'>&nbsp;{len(values)}</td></tr>")

        spoken = [e for e in rows if e.get("source") in ("tv", "laptop", "phone")]
        out = [line("After your last word", [e.get("after_speech_ms") for e in spoken]),
               line("Speech to text, this laptop", [e.get("stt_ms") for e in spoken if e.get("stt") == "local"]),
               line("Speech to text, hosted", [e.get("stt_ms") for e in spoken if e.get("stt") not in (None, "local")])]
        for who in ("rules", "LLM", "decision model", "two commands"):
            out.append(line(f"Deciding: {who}", [e.get("decide_ms") or 0.1 for e in rows if via_label(e) == who
                                                 and e.get("decide_ms") is not None]))
        kinds: dict[str, list] = {}
        for e in rows:
            if e.get("ms") is not None and e.get("type") != "none":
                kinds.setdefault(e["type"], []).append(e["ms"])
        for kind, values in sorted(kinds.items(), key=lambda kv: -len(kv[1]))[:5]:
            out.append(line(f"Doing it: {kind.replace('_', ' ')}", values, unit="s"))
        today = time.strftime("%Y%m%d")
        cost = [(e.get("cost") or 0) + (e.get("stt_cost") or 0) for e in rows]
        cost_today = sum(c for e, c in zip(rows, cost) if time.strftime("%Y%m%d", time.localtime(e["t"])) == today)
        heard = sum(1 for e in spoken if e.get("heard"))
        self.stats.setText(
            "<table><tr><td></td><td align=right style='color:gray'>&nbsp;typical</td>"
            "<td align=right style='color:gray'>&nbsp;slowest 5%</td><td align=right style='color:gray'>&nbsp;n</td>"
            f"</tr>{''.join(out)}</table><br>Cloud models: ${cost_today:.4f} today, ${sum(cost):.4f} in these "
            f"{len(rows)} commands<br>Understood {sum(1 for e in rows if e.get('type') != 'none')} of {len(rows)}"
            + (f" · heard something in {heard} of {len(spoken)} presses" if spoken else ""))

    def row_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.rows):
            return
        m = QMenu(self)
        explain = QAction("Explain in Decisions (what happened)", m)
        explain.triggered.connect(lambda: self.explain_row(row))
        decide = QAction("Decide it again now", m)
        decide.triggered.connect(lambda: self.panel.show_decisions(self.rows[row].get("heard", "")))
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
        for a in (explain, decide, again, copy):
            m.addAction(a)
        if e.get("recording") and agent_stt.recording(e["recording"]):
            m.addSeparator()
            play = QAction("Play the recording", m)
            play.triggered.connect(lambda: subprocess.Popen(["pw-play", str(agent_stt.recording(e["recording"]))]))
            m.addAction(play)
            sub = m.addMenu("Transcribe it again")
            for where, model, label in (("local", None, "On this laptop"),
                                        *(("hosted", k, v) for k, v in agent_stt.HOSTED.items())):
                a = QAction(label, sub)
                a.triggered.connect(lambda _=False, w=where, mo=model, la=label: self.transcribe_again(e, w, mo, la))
                sub.addAction(a)
        m.addSeparator()
        for a in (teach, wrong) + ((forget,) if e.get("taught") else ()):
            m.addAction(a)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def transcribe_again(self, e: dict, where: str, model: str | None, label: str):
        self.reply.setText(f"Transcribing with {label}…")

        def done(r, err):
            if err or not r:
                self.reply.setText(f"<span style='color:{RED}'>{err}</span>")
                return
            if r.get("stt_error"):
                self.reply.setText(f"{label}: <span style='color:{RED}'>{r['stt_error']}</span>")
                return
            cost = f" · ${r['stt_cost']:.5f}" if r.get("stt_cost") else ""
            self.reply.setText(f"{stt_name(r.get('stt'))}: “<b>{html.escape(r.get('text', ''))}</b>” · "
                               f"{r.get('stt_ms', '?')} ms{cost} <span style='color:gray'>(at the time: "
                               f"“{html.escape(e.get('heard', ''))}”)</span>")

        self.panel.bg.run(lambda: api("/v1/transcribe", {"recording": e["recording"], "where": where, "model": model},
                                      timeout=60), done)

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
        """The command as it happened: its recorded trace in the Decisions diagram, title and start steps too."""
        if 0 <= row < len(self.rows):
            self.panel.show_recorded(self.rows[row])

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

    def keep_changed(self, on: bool):
        self.panel.bg.run(lambda: api("/v1/pipeline", {"keep_recordings": on}, timeout=5))
        if not on:
            offer_to_delete_recordings(self)

    def erase(self):
        kept = len(agent_stt.recordings())
        if QMessageBox.question(self, "TV Agent", f"Delete all {len(self.rows)} commands from the history"
                                + (f" and the {kept} recordings kept" if kept else "") + "?\n"
                                "(Export first if you want a copy.)") != QMessageBox.StandardButton.Yes:
            return
        agent_activity.erase()
        agent_stt.delete_recordings()
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
