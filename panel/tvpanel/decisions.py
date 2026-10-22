"""Decisions tab: how a command becomes an action, drawn as a decision tree; a tester that lights up the path a
sentence takes; and editors for every rule (clean-up, fixed phrases, patterns, the d1 model, apps and hooks).
Saving sends the rules to the agent, which checks them and uses them at once (no restart)."""
import copy
import json
import subprocess

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from .common import AMBER, BLUE, GREEN, GREY, RED, ApiError, api, card, dim

# ---------------------------------------------------------------- the diagram

NODES = {   # id: (column, row, title)   on a 5 x 5 grid
    "input": (0, 0, "Your words"), "stt": (1, 0, "Speech to text"), "clean": (2, 0, "Clean-up"),
    "phrase": (2, 1, "① Fixed phrases"), "pattern": (2, 2, "② Pattern rules"), "model": (2, 3, "③ d1 model"),
    "unclear": (2, 4, "Didn't get that"), "action": (4, 2, "Action"), "tv": (4, 3, "TV"),
}
EDGES = [("input", "stt", ""), ("stt", "clean", ""), ("clean", "phrase", ""), ("phrase", "pattern", "no match"),
         ("pattern", "model", "no match"), ("model", "unclear", "unsure / not a command"),
         ("phrase", "action", "exact match"), ("pattern", "action", "first rule that fits"),
         ("model", "action", "confident"), ("action", "tv", "")]
TAB_FOR = {"clean": 0, "phrase": 1, "pattern": 2, "model": 3, "action": 4, "tv": 4, "unclear": 3}


class FlowDiagram(QWidget):
    clicked = Signal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(250)
        self.sub = {k: "" for k in NODES}
        self.path: set[str] = set()
        self.taken: set[tuple[str, str]] = set()
        self.hit = ""                  # the node that decided
        self.setMouseTracking(True)

    def set_counts(self, counts: dict, model: dict):
        self.sub.update({"input": "remote · laptop mic · typed", "stt": "Parakeet, on this laptop",
                         "clean": "fillers, mishearing fixes", "phrase": f"{counts.get('phrases', 0)} sentences",
                         "pattern": f"{counts.get('patterns', 0)} rules, in order",
                         "model": f"{counts.get('intents', 0)} intents · ≥ {model.get('min_confidence', 0.3):.2f}",
                         "unclear": "nothing happens", "action": "one typed action",
                         "tv": "player · app · volume"})
        self.update()

    def show_trace(self, path: list[str], taken: list[tuple[str, str]], hit: str, notes: dict):
        self.path, self.taken, self.hit = set(path), set(taken), hit
        for k, v in notes.items():
            self.sub[k] = v
        self.update()

    def clear(self):
        self.path, self.taken, self.hit = set(), set(), ""
        self.update()

    def _rects(self) -> dict[str, QRectF]:
        w, h = self.width(), self.height()
        cw, rh = w / 5, h / 5
        bw, bh = min(168.0, cw * 0.9), min(44.0, rh * 0.82)
        out = {}
        for k, (c, r, _) in NODES.items():
            cx, cy = cw * c + cw / 2, rh * r + rh / 2
            out[k] = QRectF(cx - bw / 2, cy - bh / 2, bw, bh)
        return out

    def mousePressEvent(self, e):
        for k, r in self._rects().items():
            if r.contains(e.position()):
                self.clicked.emit(k)
                return

    def mouseMoveEvent(self, e):
        over = any(r.contains(e.position()) for r in self._rects().values())
        self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rects = self._rects()
        text = self.palette().text().color()
        faint = QColor(text)
        faint.setAlpha(70)
        tracing = bool(self.path)
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        for a, b, label in EDGES:
            ra, rb = rects[a], rects[b]
            on = (a, b) in self.taken
            color = QColor(GREEN if on and b == "action" else BLUE) if on else (faint if tracing else QColor(GREY))
            p.setPen(QPen(color, 2.4 if on else 1.3))
            if NODES[a][0] == NODES[b][0]:        # same column: straight down
                start, end = QPointF(ra.center().x(), ra.bottom()), QPointF(rb.center().x(), rb.top())
                path = QPainterPath(start)
                path.lineTo(end)
            elif NODES[a][1] == NODES[b][1]:      # same row: straight across
                start, end = QPointF(ra.right(), ra.center().y()), QPointF(rb.left(), rb.center().y())
                path = QPainterPath(start)
                path.lineTo(end)
            else:                                 # to the action column: a curve
                start, end = QPointF(ra.right(), ra.center().y()), QPointF(rb.left(), rb.center().y())
                mid = (start.x() + end.x()) / 2
                path = QPainterPath(start)
                path.cubicTo(QPointF(mid, start.y()), QPointF(mid, end.y()), end)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            ang = path.angleAtPercent(1.0)
            tip = path.pointAtPercent(1.0)
            p.save()
            p.translate(tip)
            p.rotate(-ang)
            p.setBrush(color)
            p.drawPolygon([QPointF(0, 0), QPointF(-7, -4), QPointF(-7, 4)])
            p.restore()
            if label:
                p.setFont(small)
                p.setPen(color if on else (faint if tracing else QColor(GREY)))
                mid_pt = path.pointAtPercent(0.5)
                if NODES[a][0] == NODES[b][0]:
                    p.drawText(QPointF(mid_pt.x() + 8, mid_pt.y() + 4), label)
                else:
                    p.drawText(QPointF(mid_pt.x() - p.fontMetrics().horizontalAdvance(label) / 2, mid_pt.y() - 5),
                               label)
        for k, r in rects.items():
            visited = k in self.path
            fill = QColor(self.palette().base().color())
            border = QColor(GREY)
            if k == self.hit:
                fill, border = QColor(GREEN), QColor(GREEN)
                fill.setAlpha(60)
            elif k == "unclear" and k in self.path:
                fill, border = QColor(AMBER), QColor(AMBER)
                fill.setAlpha(60)
            elif visited:
                fill, border = QColor(BLUE), QColor(BLUE)
                fill.setAlpha(35)
            elif tracing:
                border = faint
            p.setPen(QPen(border, 1.6 if visited or k == self.hit else 1.0))
            p.setBrush(fill)
            p.drawRoundedRect(r, 9, 9)
            p.setPen(text if (visited or not tracing) else faint)
            bold = QFont(self.font())
            bold.setBold(True)
            p.setFont(bold)
            p.drawText(r.adjusted(6, 4, -6, -r.height() / 2), Qt.AlignmentFlag.AlignCenter, NODES[k][2])
            p.setFont(small)
            sub = QColor(text)
            sub.setAlpha(170 if (visited or not tracing) else 60)
            p.setPen(sub)
            p.drawText(r.adjusted(6, r.height() / 2 - 2, -6, -3), Qt.AlignmentFlag.AlignCenter,
                       p.fontMetrics().elidedText(self.sub.get(k, ""), Qt.TextElideMode.ElideRight, int(r.width() - 10)))


# ---------------------------------------------------------------- a generic table editor

class TableEditor(QWidget):
    """Rows of dicts. Column kinds: text, list (comma-separated), json (one line), choice, bool."""
    changed = Signal()

    def __init__(self, columns: list[tuple], movable: bool = True, add_label: str = "Add"):
        super().__init__()
        self.columns = columns                    # (header, key, kind, stretch, extra)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels([c[0] for c in columns])
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        for i, c in enumerate(columns):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch if c[3] else
                                    QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(lambda *_: self.changed.emit())
        lay.addWidget(self.table)
        btns = QHBoxLayout()
        self.add_btn = QPushButton(add_label)
        self.add_btn.clicked.connect(self.add_row)
        rm = QPushButton("Remove")
        rm.clicked.connect(self.remove_row)
        btns.addWidget(self.add_btn)
        btns.addWidget(rm)
        if movable:
            for text, d in (("▲ Earlier", -1), ("▼ Later", 1)):
                b = QPushButton(text)
                b.clicked.connect(lambda _=False, d=d: self.move(d))
                btns.addWidget(b)
        btns.addStretch()
        self.extra = btns
        lay.addLayout(btns)
        self.locked_rows: set[int] = set()

    def _cell(self, value, kind) -> QTableWidgetItem:
        if kind == "list":
            text = ", ".join(value or [])
        elif kind == "json":
            text = json.dumps(value, ensure_ascii=False) if value not in (None, "") else ""
        elif kind == "bool":
            item = QTableWidgetItem("")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
            return item
        else:
            text = "" if value is None else str(value)
        return QTableWidgetItem(text)

    def set_rows(self, rows: list[dict], locked: set[int] | None = None):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for r in rows:
            self._append(r)
        self.locked_rows = locked or set()
        for i in self.locked_rows:
            for j, c in enumerate(self.columns):
                if c[2] != "bool" and c[1] in ("key", "do"):
                    it = self.table.item(i, j)
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    it.setForeground(QColor(GREY))
        self.table.blockSignals(False)

    def _append(self, r: dict):
        i = self.table.rowCount()
        self.table.insertRow(i)
        for j, (_, key, kind, _, *rest) in enumerate(self.columns):
            self.table.setItem(i, j, self._cell(r.get(key), kind))

    def add_row(self, row: dict | None = None):
        self._append(row or {})
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)
        self.table.editItem(self.table.item(self.table.rowCount() - 1, 0))
        self.changed.emit()

    def remove_row(self):
        i = self.table.currentRow()
        if i < 0:
            return
        if i in self.locked_rows:
            QMessageBox.information(self, "TV Agent", "Built-in entries can be switched off but not removed.")
            return
        self.table.removeRow(i)
        self.locked_rows = {r - (r > i) for r in self.locked_rows if r != i}
        self.changed.emit()

    def move(self, d: int):
        i = self.table.currentRow()
        j = i + d
        if i < 0 or not 0 <= j < self.table.rowCount():
            return
        self.table.blockSignals(True)
        for c in range(self.table.columnCount()):
            a, b = self.table.takeItem(i, c), self.table.takeItem(j, c)
            self.table.setItem(i, c, b)
            self.table.setItem(j, c, a)
        self.table.blockSignals(False)
        self.locked_rows = {(j if r == i else i if r == j else r) for r in self.locked_rows}
        self.table.setCurrentCell(j, self.table.currentColumn())
        self.changed.emit()

    def rows(self, what: str) -> list[dict]:
        out = []
        for i in range(self.table.rowCount()):
            r = {}
            for j, (header, key, kind, _, *rest) in enumerate(self.columns):
                it = self.table.item(i, j)
                if kind == "bool":
                    r[key] = it.checkState() == Qt.CheckState.Checked
                    continue
                text = (it.text() if it else "").strip()
                if kind == "list":
                    r[key] = [x.strip() for x in text.split(",") if x.strip()]
                elif kind == "json":
                    if not text or text == "built-in":
                        continue
                    try:
                        r[key] = json.loads(text)
                    except ValueError as e:
                        raise ValueError(f"{what}, row {i + 1}, {header}: not valid JSON ({e.msg})") from None
                elif text:
                    r[key] = text
            out.append(r)
        return out

    def select_where(self, key: str, value: str):
        col = next((j for j, c in enumerate(self.columns) if c[1] == key), None)
        if col is None:
            return
        for i in range(self.table.rowCount()):
            if self.table.item(i, col) and self.table.item(i, col).text() == value:
                self.table.setCurrentCell(i, col)
                self.table.scrollToItem(self.table.item(i, col))
                return


# ---------------------------------------------------------------- the tab

PATTERN_HELP = (
    "<b>How a pattern rule works.</b> Its <i>pattern</i> is a regular expression matched against the cleaned "
    "sentence; name the parts you need with <code>(?P&lt;name&gt;…)</code>. Its <i>action</i> is JSON: a value "
    "<code>\"{name|converter}\"</code> fills that field from the part; add <code>!</code> "
    "(<code>\"{amount|seconds!}\"</code>) so the rule doesn't match when the part can't be converted, and the next "
    "rule gets a try. <code>\"*\": \"{part|title}\"</code> fills several fields at once. Rules are tried top to "
    "bottom; the first that fits wins.")


class Decisions(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.server: dict = {}
        self.raw: dict = {}
        self.dirty = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 10)
        outer.setSpacing(10)
        split = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(split, 1)

        # top: diagram + tester
        top = card()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(14, 10, 14, 10)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>How a command is decided</b> <span style='color:gray'>· click a box to edit "
                              "that step</span>"))
        head.addStretch()
        tl.addLayout(head)
        self.diagram = FlowDiagram()
        self.diagram.clicked.connect(self.node_clicked)
        tl.addWidget(self.diagram, 1)
        trow = QHBoxLayout()
        self.test = QLineEdit()
        self.test.setPlaceholderText("Try a sentence to see its path (nothing is sent to the TV)")
        self.test.returnPressed.connect(self.explain)
        go = QPushButton("Explain")
        go.clicked.connect(self.explain)
        self.add_phrase_btn = QPushButton("Add as fixed phrase…")
        self.add_phrase_btn.setToolTip("Make this exact sentence always do a given action")
        self.add_phrase_btn.clicked.connect(self.add_phrase_for_test)
        trow.addWidget(self.test, 1)
        trow.addWidget(go)
        trow.addWidget(self.add_phrase_btn)
        tl.addLayout(trow)
        self.explanation = QLabel("")
        self.explanation.setWordWrap(True)
        self.explanation.setTextFormat(Qt.TextFormat.RichText)
        self.explanation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tl.addWidget(self.explanation)
        split.addWidget(top)

        # bottom: editors
        bottom = card()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(14, 10, 14, 10)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        bl.addWidget(self.tabs, 1)

        # clean-up
        cw = QWidget()
        cf = QVBoxLayout(cw)
        form = QFormLayout()
        self.fillers = QLineEdit()
        self.fillers.setToolTip("Removed from the start of what you say")
        self.remove_words = QLineEdit()
        self.remove_words.setToolTip("Removed wherever they appear")
        form.addRow("Fillers at the start", self.fillers)
        form.addRow("Words removed anywhere", self.remove_words)
        cf.addLayout(form)
        cf.addWidget(dim("Fixes for common mishearings: a regular expression and what to put instead."))
        self.fixes = TableEditor([("Heard (regex)", "from", "text", True), ("Becomes", "to", "text", True),
                                  ("Note", "note", "text", True)], movable=True, add_label="Add fix")
        cf.addWidget(self.fixes)
        self.tabs.addTab(cw, "Clean-up")

        # fixed phrases
        self.phrases = TableEditor([("ID", "id", "text", False), ("Sentences (comma-separated)", "say", "list", True),
                                    ("Action (JSON)", "do", "json", True), ("Note", "note", "text", False)],
                                   add_label="Add phrase")
        pw = QWidget()
        pl = QVBoxLayout(pw)
        pl.addWidget(dim("The whole sentence must match exactly (after clean-up). Checked before anything else."))
        pl.addWidget(self.phrases)
        self.tabs.addTab(pw, "① Fixed phrases")

        # patterns
        self.patterns = TableEditor([("ID", "id", "text", False), ("Group", "stage", "text", False),
                                     ("Pattern (regex)", "match", "text", True), ("Action template (JSON)", "do",
                                                                                  "json", True),
                                     ("Note", "note", "text", False)], add_label="Add rule")
        aw = QWidget()
        al = QVBoxLayout(aw)
        al.addWidget(self.patterns, 1)
        self.pattern_help = QLabel(PATTERN_HELP)
        self.pattern_help.setWordWrap(True)
        self.pattern_help.setTextFormat(Qt.TextFormat.RichText)
        self.pattern_help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        al.addWidget(self.pattern_help)
        self.tabs.addTab(aw, "② Pattern rules")

        # model
        mw = QWidget()
        ml = QVBoxLayout(mw)
        mf = QFormLayout()
        self.question = QLineEdit()
        self.min_conf = QDoubleSpinBox()
        self.min_conf.setRange(0, 1)
        self.min_conf.setSingleStep(0.05)
        self.min_conf.setToolTip("d1's confidence (0 = all options equally likely). Below it: “Didn't get that”.")
        self.fallback = QComboBox()
        self.title_q = QLineEdit()
        self.cands = QSpinBox()
        self.cands.setRange(2, 8)
        for w in (self.question, self.title_q):
            w.textEdited.connect(self.mark_dirty)
        self.min_conf.valueChanged.connect(self.mark_dirty)
        self.cands.valueChanged.connect(self.mark_dirty)
        self.fallback.currentIndexChanged.connect(self.mark_dirty)
        mf.addRow("Question d1 answers", self.question)
        mf.addRow("Act only above confidence", self.min_conf)
        mf.addRow("If d1 is offline, treat as", self.fallback)
        mf.addRow("Title-choice question", self.title_q)
        mf.addRow("Title candidates shown to d1", self.cands)
        ml.addLayout(mf)
        ml.addWidget(dim("Intents are the options d1 chooses between; it reads only their descriptions. Built-in "
                         "intents use code to pull out titles and numbers; your own intents do a fixed action, or use "
                         "\"{text|converter}\" on the whole sentence."))
        self.intents = TableEditor([("On", "enabled", "bool", False), ("Intent", "key", "text", False),
                                    ("What d1 reads", "describe", "text", True),
                                    ("Action (JSON, your own intents)", "do", "json", True)],
                                   movable=False, add_label="Add intent")
        ml.addWidget(self.intents, 1)
        self.tabs.addTab(mw, "③ d1 model")

        # apps & hooks
        hw = QWidget()
        hl = QVBoxLayout(hw)
        hl.addWidget(dim("Apps the agent may open (“open prime video”), with the names you might say. The agent only "
                         "ever starts apps on this list."))
        self.apps = TableEditor([("Key", "key", "text", False), ("Name", "label", "text", False),
                                 ("Package", "package", "text", True), ("Activity", "activity", "text", True),
                                 ("Spoken names", "aliases", "list", True)], movable=False, add_label="Add app")
        hl.addWidget(self.apps, 1)
        hf = QFormLayout()
        self.hook_fields = {}
        for hook, title, links in (("youtube", "YouTube", ("link",)), ("twitch", "Twitch", ("link",)),
                                   ("stremio", "Stremio page", ("link", "link_episode"))):
            row = QHBoxLayout()
            app = QComboBox()
            app.currentIndexChanged.connect(self.mark_dirty)
            row.addWidget(app)
            edits = {}
            for k in links:
                e = QLineEdit()
                e.setPlaceholderText(k)
                e.textEdited.connect(self.mark_dirty)
                row.addWidget(e, 2)
                edits[k] = e
            self.hook_fields[hook] = (app, edits)
            hf.addRow(f"{title} opens in", row)
        hl.addLayout(hf)
        hl.addWidget(dim("Links: {id} the video or IMDb id, {channel} the Twitch login, {type} movie/series, {video} "
                         "the episode id. Films and series themselves play in the player chosen in Settings."))
        self.tabs.addTab(hw, "Apps && hooks")

        for ed in (self.fixes, self.phrases, self.patterns, self.intents, self.apps):
            ed.changed.connect(self.mark_dirty)
        for w in (self.fillers, self.remove_words):
            w.textEdited.connect(self.mark_dirty)
        split.addWidget(bottom)
        split.setSizes([360, 480])

        bar = QHBoxLayout()
        self.status = dim("")
        tests = QPushButton("Run tests")
        tests.setToolTip("Try these rules (saved or not) on the labelled sentences in eval/utterances.jsonl")
        tests.clicked.connect(self.run_tests)
        revert = QPushButton("Revert")
        revert.clicked.connect(self.load)
        reset = QPushButton("Reset to defaults…")
        reset.clicked.connect(self.reset)
        open_file = QPushButton("Open file")
        open_file.clicked.connect(self.open_file)
        self.save_btn = QPushButton("Save · live now")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save)
        bar.addWidget(self.status, 1)
        for b in (tests, open_file, revert, reset, self.save_btn):
            bar.addWidget(b)
        outer.addLayout(bar)
        self.loaded = False

    # ---- loading and saving ----

    def mark_dirty(self, *_):
        if not self.dirty:
            self.dirty = True
            self.status.setText("<span style='color:#e0a030'>● Unsaved changes</span>")

    def load(self):
        def done(res, err):
            if err:
                self.status.setText(f"<span style='color:{RED}'>The voice agent isn't running: start it to see and "
                                    "edit the rules.</span>")
                return
            self.server = res
            self.raw = copy.deepcopy(res["rules"])
            self.fill_editors(self.raw)
            self.dirty = False
            self.loaded = True
            src = "your rules.json" if res["source"].endswith("rules.json") else res["source"]
            err_text = f" · <span style='color:{RED}'>{res['error']}</span>" if res.get("error") else ""
            self.status.setText(f"Using {src}: " + ", ".join(f"{v} {k}" for k, v in res["counts"].items()) + err_text)
            self.diagram.set_counts(res["counts"], res["rules"].get("model", {}))
            self.pattern_help.setText(PATTERN_HELP + "<br><b>Converters:</b> " + " · ".join(
                f"<code>{k}</code> {v}" for k, v in res["converters"].items()) + "<br><b>Actions and their fields:</b> "
                + " · ".join(f"<code>{k}</code>({', '.join(v)})" for k, v in res["fields"].items()))

        self.panel.bg.run(lambda: api("/v1/rules", timeout=5), done)

    def fill_editors(self, raw: dict):
        n = raw.get("normalize", {})
        self.fillers.setText(", ".join(n.get("fillers", [])))
        self.remove_words.setText(", ".join(n.get("remove_words", [])))
        self.fixes.set_rows(n.get("fixes", []))
        self.phrases.set_rows(raw.get("phrases", []))
        self.patterns.set_rows(raw.get("patterns", []))
        m = raw.get("model", {})
        self.question.setText(m.get("question", ""))
        self.min_conf.setValue(float(m.get("min_confidence", 0.3)))
        self.title_q.setText(m.get("title_question", ""))
        self.cands.setValue(int(m.get("title_candidates", 4)))
        builtin = set(self.server.get("builtin_intents", []))
        rows, locked = [], set()
        for i, (k, v) in enumerate(m.get("intents", {}).items()):
            v = v if isinstance(v, dict) else {"describe": str(v)}
            rows.append({"enabled": v.get("enabled", True), "key": k, "describe": v.get("describe", ""),
                         "do": "built-in" if k in builtin else v.get("do")})
            if k in builtin:
                locked.add(i)
        self.intents.set_rows(rows, locked)
        for i in locked:          # show "built-in" in the action column
            self.intents.table.item(i, 3).setText("built-in")
        self.fallback.blockSignals(True)
        self.fallback.clear()
        for k in m.get("intents", {}):
            if k != "none":
                self.fallback.addItem(k, k)
        self.fallback.setCurrentIndex(max(0, self.fallback.findData(m.get("offline_fallback", "play_title"))))
        self.fallback.blockSignals(False)
        self.apps.set_rows(raw.get("apps", []))
        keys = [a.get("key") for a in raw.get("apps", [])]
        for hook, (combo, edits) in self.hook_fields.items():
            h = raw.get("hooks", {}).get(hook, {})
            combo.blockSignals(True)
            combo.clear()
            for k in keys:
                combo.addItem(k, k)
            combo.setCurrentIndex(max(0, combo.findData(h.get("app"))))
            combo.blockSignals(False)
            for k, e in edits.items():
                e.setText(h.get(k, ""))

    def draft(self) -> dict:
        """The rules as edited (raises ValueError with where the problem is)."""
        raw = copy.deepcopy(self.raw) if self.raw else {}
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
        raw["normalize"] = {"fillers": split(self.fillers.text()), "remove_words": split(self.remove_words.text()),
                            "fixes": self.fixes.rows("Fixes")}
        raw["phrases"] = self.phrases.rows("Fixed phrases")
        raw["patterns"] = self.patterns.rows("Pattern rules")
        builtin = set(self.server.get("builtin_intents", []))
        intents = {}
        for r in self.intents.rows("Intents"):
            key = r.get("key", "").strip()
            if not key:
                continue
            it = {"describe": r.get("describe", "")}
            if not r.get("enabled", True):
                it["enabled"] = False
            if key not in builtin and "do" in r:
                it["do"] = r["do"]
            intents[key] = it
        raw["model"] = {**raw.get("model", {}), "question": self.question.text().strip(),
                        "min_confidence": round(self.min_conf.value(), 3),
                        "offline_fallback": self.fallback.currentData() or "play_title",
                        "title_question": self.title_q.text().strip(), "title_candidates": self.cands.value(),
                        "intents": intents}
        raw["apps"] = self.apps.rows("Apps")
        hooks = {}
        for hook, (combo, edits) in self.hook_fields.items():
            hooks[hook] = {"app": combo.currentData() or combo.currentText()}
            for k, e in edits.items():
                if e.text().strip():
                    hooks[hook][k] = e.text().strip()
        raw["hooks"] = hooks
        return raw

    def save(self):
        try:
            raw = self.draft()
        except ValueError as e:
            QMessageBox.warning(self, "TV Agent", str(e))
            return

        def done(res, err):
            if err:
                QMessageBox.warning(self, "TV Agent", f"Not saved: {err}")
                return
            self.status.setText(f"<span style='color:{GREEN}'>Saved · live now</span>")
            self.dirty = False
            self.load()

        self.panel.bg.run(lambda: api("/v1/rules", raw, timeout=10), done)

    def reset(self):
        if QMessageBox.question(self, "TV Agent", "Throw away your rules and go back to the defaults?\n"
                                "(Your rules.json is deleted.)") != QMessageBox.StandardButton.Yes:
            return
        self.panel.bg.run(lambda: api("/v1/rules/reset", {}, timeout=10), lambda r, e: (
            QMessageBox.warning(self, "TV Agent", str(e)) if e else self.load()))

    def open_file(self):
        from .common import agent_config
        path = agent_config.CONFIG_DIR / "rules.json"
        if not path.exists():
            QMessageBox.information(self, "TV Agent", "You're using the built-in defaults; save once to create "
                                    f"{path}.")
            return
        subprocess.Popen(["xdg-open", str(path)])

    def run_tests(self):
        try:
            raw = self.draft()
        except ValueError as e:
            QMessageBox.warning(self, "TV Agent", str(e))
            return
        self.status.setText("Running the labelled sentences…")

        def done(res, err):
            if err:
                self.status.setText("")
                QMessageBox.warning(self, "TV Agent", f"Couldn't run the tests: {err}")
                return
            fails = [r for r in res["results"] if not r["ok"]]
            color = GREEN if not fails else AMBER
            self.status.setText(f"<span style='color:{color}'>Tests: {res['full_ok']}/{res['n']} right</span>"
                                + (" · unsaved rules" if self.dirty else ""))
            if fails:
                text = "\n".join(f"✗ “{r['text']}”\n    expected {json.dumps(r['expect'])}\n    got      "
                                 f"{json.dumps(r['got'])} ({r['via']} {r['rule']})" for r in fails)
                dlg = QDialog(self)
                dlg.setWindowTitle(f"{len(fails)} sentences decided differently")
                v = QVBoxLayout(dlg)
                box = QPlainTextEdit(text)
                box.setReadOnly(True)
                box.setMinimumSize(720, 360)
                v.addWidget(box)
                bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
                bb.rejected.connect(dlg.reject)
                v.addWidget(bb)
                dlg.exec()

        self.panel.bg.run(lambda: api("/v1/eval", {"rules": raw}, timeout=180), done)

    # ---- the tester ----

    def node_clicked(self, node: str):
        if node in TAB_FOR:
            self.tabs.setCurrentIndex(TAB_FOR[node])

    def explain_text(self, text: str):
        self.test.setText(text)
        self.explain()

    def explain(self):
        text = self.test.text().strip()
        if not text:
            self.diagram.clear()
            self.explanation.setText("")
            return
        self.explanation.setText("…")
        self.panel.bg.run(lambda: api("/v1/decide", {"text": text}, timeout=20), self.show_decision)

    def show_decision(self, res, err):
        if err:
            why = str(err) if isinstance(err, ApiError) else "The voice agent isn’t running."
            self.explanation.setText(f"<span style='color:{RED}'>{why}</span>")
            return
        self.last = res
        trace = res["trace"]
        path, taken, notes, lines = ["input", "clean"], [("input", "stt"), ("stt", "clean")], {}, []
        path.append("stt")
        cleaned = trace[0].get("text", "")
        lines.append(f"Cleaned to “<b>{cleaned}</b>”" if cleaned else "Nothing left after clean-up")
        hit = ""
        prev = "clean"
        for step in trace[1:]:
            st = step["stage"]
            if st == "phrase":
                path.append("phrase")
                taken.append((prev, "phrase"))
                prev = "phrase"
                if step.get("matched"):
                    hit = "phrase"
                    notes["phrase"] = f"matched: {step.get('rule')}"
                    lines.append(f"① Fixed phrase <b>{step.get('rule')}</b> matches exactly")
                else:
                    notes["phrase"] = "no exact match"
                    lines.append("① No fixed phrase is exactly this")
            elif st == "pattern":
                if "pattern" not in path:
                    path.append("pattern")
                    taken.append((prev, "pattern"))
                    prev = "pattern"
                if step.get("matched"):
                    hit = "pattern"
                    parts = ", ".join(f"{k} = “{v}”" for k, v in (step.get("parts") or {}).items())
                    notes["pattern"] = f"matched: {step.get('rule')}"
                    lines.append(f"② Pattern rule <b>{step.get('rule')}</b> fits" + (f" ({parts})" if parts else ""))
                elif step.get("rule"):
                    lines.append(f"② <span style='color:gray'>{step['rule']}: the pattern matched but "
                                 f"{step.get('why', 'it was skipped')}</span>")
                else:
                    notes.setdefault("pattern", "no rule fits")
                    if not any(s.get("matched") for s in trace if s["stage"] == "pattern"):
                        lines.append("② No pattern rule fits")
            elif st == "model":
                path.append("model")
                taken.append((prev, "model"))
                prev = "model"
                if step.get("available") is False:
                    lines.append(f"③ <span style='color:{AMBER}'>d1 isn't running</span>; tried the offline fallback")
                    continue
                top = " · ".join(f"{k} {p:.2f}" for k, p in step.get("top", []))
                lines.append(f"③ d1 picked <b>{step.get('intent')}</b> with confidence "
                             f"{step.get('confidence', 0):.2f} (needs ≥ {step.get('threshold', 0):.2f}) — top: {top}")
                if step.get("accepted"):
                    hit = "model"
                    notes["model"] = f"{step.get('intent')} {step.get('confidence', 0):.2f}"
                else:
                    notes["model"] = f"{step.get('intent')} {step.get('confidence', 0):.2f}: not acting"
        a = res["action"]
        if a["type"] == "none":
            path.append("unclear")
            taken.append((prev, "unclear"))
            lines.append(f"→ <span style='color:{AMBER}'><b>Nothing happens</b></span> (“Didn't get that”)")
        else:
            path += ["action", "tv"]
            taken += [(hit or prev, "action"), ("action", "tv")]
            fields = ", ".join(f"{k} {v}" for k, v in a.items() if k != "type" and v not in (None, False, ""))
            notes["action"] = a["type"].replace("_", " ")
            notes["tv"] = res.get("describe", "")
            lines.append(f"→ <span style='color:{GREEN}'><b>{a['type'].replace('_', ' ')}</b></span> {fields} — "
                         f"the TV would show “{res.get('describe', '')}”")
        self.diagram.show_trace(path, taken, hit, notes)
        self.explanation.setText("<br>".join(lines))
        rule = res.get("rule")
        if res.get("via") == "pattern":
            self.patterns.select_where("id", rule)
        elif res.get("via") == "phrase":
            self.phrases.select_where("id", rule)

    def add_phrase_for_test(self):
        text = self.test.text().strip().lower()
        if not text:
            return
        action = getattr(self, "last", {}).get("action") or {"type": "transport", "op": "pause"}
        action = {k: v for k, v in action.items() if v not in (None, False, "") or k == "type"}
        if action.get("type") == "none":
            action = {"type": "transport", "op": "pause"}
        self.tabs.setCurrentIndex(1)
        self.phrases.add_row({"id": "_".join(text.split()[:3]), "say": [text], "do": action,
                              "note": "added from the tester"})
        self.status.setText("<span style='color:#e0a030'>● Added a phrase: check its action, then Save</span>")
