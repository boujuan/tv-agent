"""Decisions tab: how a command becomes an action, drawn as a diagram whose decision steps can be dragged into another
order and switched on or off (live, /v1/pipeline); a tester that lights up the path a sentence takes; and editors
for every rule (clean-up, fixed phrases, patterns, the LLM, the d1 model, apps and hooks). Saving sends the rules to
the agent, which checks them and uses them at once (no restart)."""
import copy
import json
import subprocess

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QToolButton, QVBoxLayout, QWidget)

from .common import AMBER, BLUE, GREEN, GREY, RED, ApiError, api, card, dim, openrouter_check, systemctl

# ---------------------------------------------------------------- the diagram

LLM_MODELS = ["deepseek/deepseek-v4.1-flash-1017", "google/gemini-3.5-flash-lite", "google/gemini-3.1-flash-lite",
              "google/gemini-2.5-flash-lite", "reflection/beam-1.0-501b-a23b", "anthropic/claude-haiku-5.5",
              "openai/gpt-5.6-luna", "z-ai/glm-5.3-flash"]
# The d1 step's hosted model: Liquid's own API, or any of OpenRouter's decision models
DECISION_MODELS = ["d1", "d1-ultra", "liquid/d1", "liquid/d1-ultra", "typesafe/jev-1.14", "cloudflare/clef",
                   "cloudflare/clef-flash", "nace-ai/drex-v2.0", "perplexity/pplx-decider-v1.2", "inception/mercury-decide"]

FIXED = {"input": "Your words", "stt": "Speech to text", "clean": "Clean-up", "unclear": "Didn't get that",
         "action": "Action", "tv": "TV"}
STEP_TITLE = {"phrases": "Fixed phrases", "patterns": "Pattern rules", "llm": "LLM", "d1": "d1 model"}
STEP_EDGE = {"phrases": "exact match", "patterns": "a rule fits", "llm": "answered", "d1": "confident"}
WHERE = [("local-first", "Local, then hosted"), ("hosted-first", "Hosted, then local"), ("local", "Local only"),
         ("hosted", "Hosted only")]
NUMS = "①②③④"
TAB_FOR = {"clean": 0, "phrases": 1, "patterns": 2, "llm": 3, "d1": 4, "unclear": 4, "action": 5, "tv": 5}


class StepCard(QFrame):
    """One decision step in the diagram: drag it (or ▲▼) to change the order, tick it on or off."""
    toggled = Signal(str, bool)
    nudged = Signal(str, int)            # step, -1 earlier / +1 later
    dragged = Signal(str, float)         # step, the card's centre (y) while it's dragged
    dropped = Signal(str)
    clicked = Signal(str)
    where_changed = Signal(str)

    def __init__(self, step: str, parent: QWidget):
        super().__init__(parent)
        self.step, self.state, self._press, self._dragging = step, "", None, False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 6, 3)
        lay.setSpacing(6)
        self.handle = QLabel("⠿")
        self.handle.setToolTip("Drag to change the order")
        self.handle.setCursor(Qt.CursorShape.OpenHandCursor)
        self.num = QLabel("")
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = QLabel(f"<b>{STEP_TITLE[step]}</b>")
        self.sub = QLabel("")
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        self.sub.setFont(small)
        texts.addWidget(self.title)
        texts.addWidget(self.sub)
        lay.addWidget(self.handle)
        lay.addWidget(self.num)
        lay.addLayout(texts, 1)
        self.where = None
        if step == "d1":
            self.where = QComboBox()
            for key, label in WHERE:
                self.where.addItem(label, key)
            self.where.setToolTip("The d1 on this laptop (llama-d1), Liquid's hosted one, or one of them first and "
                                  "the other when it doesn't answer")
            self.where.activated.connect(lambda i: self.where_changed.emit(self.where.itemData(i)))
            lay.addWidget(self.where)
        self.on = QCheckBox("On")
        self.on.clicked.connect(lambda checked: (self.set_state(self.state), self.toggled.emit(step, checked)))
        lay.addWidget(self.on)
        for arrow, d, tip in ((Qt.ArrowType.UpArrow, -1, "Earlier"), (Qt.ArrowType.DownArrow, 1, "Later")):
            b = QToolButton()
            b.setArrowType(arrow)
            b.setAutoRaise(True)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, d=d: self.nudged.emit(step, d))
            lay.addWidget(b)
        self.set_state("")

    def set_state(self, state: str):
        """"" normal, "visited" asked without an answer, "hit" decided, "off" skipped, "out" didn't answer."""
        self.state = state
        off = not self.on.isChecked()
        color = {"hit": GREEN, "visited": BLUE, "out": AMBER}.get(state, GREY)
        fill = {"hit": "rgba(47,177,112,0.18)", "visited": "rgba(76,141,255,0.12)",
                "out": "rgba(224,160,48,0.15)"}.get(state, "palette(base)")
        style = "dashed" if off else "solid"
        self.setStyleSheet(f"StepCard {{ border: {1.6 if state else 1}px {style} {color}; border-radius: 9px; "
                           f"background: {fill}; }}")
        for w in (self.title, self.sub, self.num):
            w.setEnabled(not off)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press, self._start, self._dragging = e.globalPosition(), self.y(), False

    def mouseMoveEvent(self, e):
        if self._press is None:
            return
        dy = e.globalPosition().y() - self._press.y()
        if self._dragging or abs(dy) > 5:
            self._dragging = True
            self.raise_()
            self.handle.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.move(self.x(), int(self._start + dy))
            self.dragged.emit(self.step, self.y() + self.height() / 2)

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self.handle.setCursor(Qt.CursorShape.OpenHandCursor)
            self.dropped.emit(self.step)
        elif self._press is not None:
            self.clicked.emit(self.step)
        self._press, self._dragging = None, False


class FlowDiagram(QWidget):
    """How a command is decided: the fixed nodes are drawn; the decision steps are StepCards in the middle column, in
    their order. A traced sentence lights up the path it took."""
    clicked = Signal(str)                 # a node or step: open its editor
    order_changed = Signal(list)
    step_toggled = Signal(str, bool)
    where_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(390)
        self.order = ["phrases", "patterns", "llm", "d1"]
        self.cards = {}
        for step in self.order:
            c = StepCard(step, self)
            c.toggled.connect(self.step_toggled)
            c.nudged.connect(self._nudge)
            c.dragged.connect(self._drag)
            c.dropped.connect(lambda _s: self._drop())
            c.clicked.connect(self.clicked)
            c.where_changed.connect(self.where_changed)
            self.cards[step] = c
        self.sub = {k: "" for k in FIXED}
        self.steps_sub: dict[str, str] = {}
        self.path: set[str] = set()       # fixed nodes on the traced path
        self.walked: list[str] = []       # steps the sentence went through, in order
        self.hit = ""                     # the step that decided ("fallback": the offline guess)
        self._dragging_step = ""
        self.setMouseTracking(True)

    # ---- state ----

    def set_pipeline(self, order: list[str], off: set[str], where: str, subs: dict[str, str]):
        self.order = list(order)
        for step, c in self.cards.items():
            c.on.setChecked(step not in off)
            if c.where is not None:
                c.where.setCurrentIndex(max(0, c.where.findData(where)))
        self.steps_sub = subs
        self._number()
        self._place()
        self.clear()

    def set_counts(self, counts: dict):
        self.sub.update({"input": "remote · laptop mic · typed", "stt": "Parakeet, on this laptop",
                         "clean": "fillers, mishearing fixes", "unclear": "nothing happens",
                         "action": "one typed action (or two)", "tv": "player · app · volume"})
        self.update()

    def show_trace(self, path: list[str], walked: list[str], states: dict[str, str], hit: str, notes: dict):
        self.path, self.walked, self.hit = set(path), walked, hit
        for step, c in self.cards.items():
            c.set_state(states.get(step, ""))
            c.sub.setText(notes.get(step) or self.steps_sub.get(step, ""))
        for k, v in notes.items():
            if k in self.sub:
                self.sub[k] = v
        self.update()

    def clear(self):
        self.path, self.walked, self.hit = set(), [], ""
        for step, c in self.cards.items():
            c.set_state("")
            c.sub.setText(self.steps_sub.get(step, ""))
        self.update()

    def _number(self):
        for i, step in enumerate(self.order):
            self.cards[step].num.setText(NUMS[i])

    # ---- reordering ----

    def _nudge(self, step: str, d: int):
        i = self.order.index(step)
        j = i + d
        if 0 <= j < len(self.order):
            self.order[i], self.order[j] = self.order[j], self.order[i]
            self._number()
            self._place()
            self.order_changed.emit(list(self.order))

    def _drag(self, step: str, cy: float):
        slots = self._slots()
        target = min(range(len(slots)), key=lambda i: abs(slots[i].center().y() - cy))
        self._dragging_step = step
        if self.order.index(step) != target:
            self.order.remove(step)
            self.order.insert(target, step)
            self._number()
            self._place(skip=step)
        self.update()

    def _drop(self):
        moved = bool(self._dragging_step)
        self._dragging_step = ""
        self._place()
        if moved:
            self.order_changed.emit(list(self.order))

    # ---- geometry ----

    def _frame(self) -> tuple[float, float, float, float, float]:
        """(steps column x, its width, node width, node height, card height)."""
        w = self.width()
        nw = min(160.0, w * 0.17)
        sw = min(470.0, w * 0.46)
        return w * 0.27, sw, nw, 42.0, 44.0

    def _slots(self) -> list[QRectF]:
        sx, sw, _, nh, ch = self._frame()
        top = 8 + nh + 24
        return [QRectF(sx, top + i * (ch + 14), sw, ch) for i in range(len(self.order))]

    def _rects(self) -> dict[str, QRectF]:
        sx, sw, nw, nh, ch = self._frame()
        cx = sx + sw / 2
        slots = self._slots()
        r = {"clean": QRectF(cx - nw / 2, 8, nw, nh)}
        r["stt"] = QRectF(max(8.0, cx - nw / 2 - nw - 40), 8, nw, nh)
        r["input"] = QRectF(max(8.0, r["stt"].left() - nw - 40), 8, nw, nh)
        if r["input"].right() + 20 > r["stt"].left():              # narrow window: squeeze the first two
            r["input"] = QRectF(8, 8, nw * 0.8, nh)
            r["stt"] = QRectF(r["input"].right() + 18, 8, nw * 0.8, nh)
        r["unclear"] = QRectF(cx - nw / 2, slots[-1].bottom() + 34, nw, nh)
        aw = min(170.0, self.width() * 0.17)
        ax = self.width() - aw - 10
        mid = (slots[0].top() + slots[-1].bottom()) / 2
        r["action"] = QRectF(ax, mid - nh / 2, aw, nh)
        r["tv"] = QRectF(ax, r["action"].bottom() + 26, aw, nh)
        return r

    def _place(self, skip: str = ""):
        for step, rect in zip(self.order, self._slots()):
            if step != skip:
                self.cards[step].setGeometry(rect.toRect())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place()

    def mousePressEvent(self, e):
        for k, r in self._rects().items():
            if r.contains(e.position()):
                self.clicked.emit(k)
                return

    def mouseMoveEvent(self, e):
        over = any(r.contains(e.position()) for r in self._rects().values())
        self.setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)

    # ---- drawing ----

    def _arrow(self, p: QPainter, path: QPainterPath, color: QColor, width: float, label: str = "",
               label_at: float = 0.5, dashed: bool = False):
        pen = QPen(color, width)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.save()
        p.translate(path.pointAtPercent(1.0))
        p.rotate(-path.angleAtPercent(1.0))
        p.setPen(QPen(color, 1))
        p.setBrush(color)
        p.drawPolygon([QPointF(0, 0), QPointF(-7, -4), QPointF(-7, 4)])
        p.restore()
        if label:
            pt = path.pointAtPercent(label_at)
            p.setPen(color)
            p.drawText(QPointF(pt.x() + 6, pt.y() - 4), label)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rects = self._rects()
        slots = {step: QRectF(self.cards[step].geometry()) for step in self.order}
        text = self.palette().text().color()
        faint = QColor(text)
        faint.setAlpha(70)
        tracing = bool(self.path)
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)

        def color(on: bool) -> QColor:
            return QColor(BLUE) if on else (faint if tracing else QColor(GREY))

        def line(a: QPointF, b: QPointF) -> QPainterPath:
            path = QPainterPath(a)
            path.lineTo(b)
            return path

        # the top row and into the steps
        for a, b in (("input", "stt"), ("stt", "clean")):
            on = a in self.path and b in self.path
            self._arrow(p, line(QPointF(rects[a].right(), rects[a].center().y()),
                                QPointF(rects[b].left(), rects[b].center().y())), color(on), 2.4 if on else 1.3)
        first = slots[self.order[0]]
        on = bool(self.walked)
        self._arrow(p, line(QPointF(rects["clean"].center().x(), rects["clean"].bottom()),
                            QPointF(rects["clean"].center().x(), first.top())), color(on), 2.4 if on else 1.3)
        # down the steps: "no match", "not sure", "off"
        for a, b in zip(self.order, self.order[1:]):
            ra, rb = slots[a], slots[b]
            on = a in self.walked and b in self.walked
            x = ra.left() + 22
            self._arrow(p, line(QPointF(x, ra.bottom()), QPointF(x, rb.top())), color(on), 2.4 if on else 1.3)
        last = slots[self.order[-1]]
        on = "unclear" in self.path
        self._arrow(p, line(QPointF(rects["unclear"].center().x(), last.bottom()),
                            QPointF(rects["unclear"].center().x(), rects["unclear"].top())),
                    QColor(AMBER) if on else color(False), 2.4 if on else 1.3, "nothing decided", 0.95)
        # each step that's on, to the action
        for step in self.order:
            r = slots[step]
            off = not self.cards[step].on.isChecked()
            on = self.hit == step
            start = QPointF(r.right(), r.center().y())
            end = QPointF(rects["action"].left(), rects["action"].center().y())
            mid = (start.x() + end.x()) / 2
            path = QPainterPath(start)
            path.cubicTo(QPointF(mid, start.y()), QPointF(mid, end.y()), end)
            c = QColor(GREEN) if on else color(False)
            if off:
                c.setAlpha(45)
            self._arrow(p, path, c, 2.4 if on else 1.3, "" if off else STEP_EDGE[step], 0.18, dashed=off)
        on = "tv" in self.path
        self._arrow(p, line(QPointF(rects["action"].center().x(), rects["action"].bottom()),
                            QPointF(rects["tv"].center().x(), rects["tv"].top())), color(on), 2.4 if on else 1.3)
        # the fixed nodes
        bold = QFont(self.font())
        bold.setBold(True)
        for k, r in rects.items():
            visited = k in self.path
            fill = QColor(self.palette().base().color())
            border = QColor(GREY)
            if k == "action" and visited:
                fill, border = QColor(GREEN), QColor(GREEN)
                fill.setAlpha(60)
            elif k == "unclear" and visited:
                fill, border = QColor(AMBER), QColor(AMBER)
                fill.setAlpha(60)
            elif visited:
                fill, border = QColor(BLUE), QColor(BLUE)
                fill.setAlpha(35)
            elif tracing:
                border = faint
            p.setPen(QPen(border, 1.6 if visited else 1.0))
            p.setBrush(fill)
            p.drawRoundedRect(r, 9, 9)
            p.setPen(text if (visited or not tracing) else faint)
            p.setFont(bold)
            p.drawText(r.adjusted(6, 3, -6, -r.height() / 2), Qt.AlignmentFlag.AlignCenter, FIXED[k])
            p.setFont(small)
            sub = QColor(text)
            sub.setAlpha(170 if (visited or not tracing) else 60)
            p.setPen(sub)
            p.drawText(r.adjusted(6, r.height() / 2 - 2, -6, -3), Qt.AlignmentFlag.AlignCenter,
                       p.fontMetrics().elidedText(self.sub.get(k, ""), Qt.TextElideMode.ElideRight, int(r.width() - 10)))


# ---------------------------------------------------------------- a generic table editor

class TableEditor(QWidget):
    """Rows of dicts. Column kinds: text (trimmed), raw (exactly as typed: a clean-up fix's spaces matter, "pon " →
    "play "), list (comma-separated), json (one line), choice, bool."""
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
            if c[3] is True:
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            elif c[3]:                                   # a width to start at, then yours to drag
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
                self.table.setColumnWidth(i, c[3])
            else:
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
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
                if kind == "raw":
                    r[key] = it.text() if it else ""
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
        head.addWidget(QLabel("<b>How a command is decided</b> <span style='color:gray'>· drag the steps into "
                              "another order, switch them on or off (live at once) · click one to edit it</span>"))
        head.addStretch()
        tl.addLayout(head)
        self.diagram = FlowDiagram()
        self.diagram.clicked.connect(self.node_clicked)
        self.diagram.order_changed.connect(lambda order: self.set_pipeline({"steps": order}))
        self.diagram.step_toggled.connect(self.step_toggled)
        self.diagram.where_changed.connect(self.where_changed)
        self.pipeline: dict = {}
        self._checked: dict[str, str] = {}             # the model ids last checked with OpenRouter
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
        self.fixes = TableEditor([("Heard (regex)", "from", "raw", True), ("Becomes", "to", "raw", True),
                                  ("Note", "note", "text", True)], movable=True, add_label="Add fix")
        cf.addWidget(self.fixes)
        self.tabs.addTab(cw, "Clean-up")

        # fixed phrases
        self.phrases = TableEditor([("ID", "id", "text", False), ("Sentences (comma-separated)", "say", "list", True),
                                    ("Action (JSON)", "do", "json", True), ("Note", "note", "text", 200)],
                                   add_label="Add phrase")
        pw = QWidget()
        pl = QVBoxLayout(pw)
        pl.addWidget(dim("The whole sentence must match exactly (after clean-up). The sentences you taught in the "
                         "Activity tab count as fixed phrases too."))
        pl.addWidget(self.phrases)
        self.tabs.addTab(pw, "Fixed phrases")

        # patterns
        self.patterns = TableEditor([("ID", "id", "text", False), ("Group", "stage", "text", False),
                                     ("Pattern (regex)", "match", "text", True), ("Action template (JSON)", "do",
                                                                                  "json", True),
                                     ("Note", "note", "text", 200)], add_label="Add rule")
        aw = QWidget()
        al = QVBoxLayout(aw)
        al.addWidget(self.patterns, 1)
        self.help_toggle = QToolButton()
        self.help_toggle.setText("How a pattern rule works")
        self.help_toggle.setCheckable(True)
        self.help_toggle.setAutoRaise(True)
        self.help_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.help_toggle.setArrowType(Qt.ArrowType.RightArrow)
        al.addWidget(self.help_toggle)
        self.pattern_help = QLabel(PATTERN_HELP)
        self.pattern_help.setWordWrap(True)
        self.pattern_help.setTextFormat(Qt.TextFormat.RichText)
        self.pattern_help.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pattern_help.setVisible(False)
        self.help_toggle.toggled.connect(lambda on: (self.pattern_help.setVisible(on), self.help_toggle.setArrowType(
            Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)))
        al.addWidget(self.pattern_help)
        self.tabs.addTab(aw, "Pattern rules")

        # the LLM (applies at once, like the steps above)
        lw = QWidget()
        ll = QVBoxLayout(lw)
        lf = QFormLayout()
        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)               # any OpenRouter model id
        self.llm_model.addItems(LLM_MODELS)
        self.llm_model.setToolTip("Any model id from openrouter.ai/models that supports structured outputs")
        self.llm_model.lineEdit().editingFinished.connect(self.llm_changed)
        self.llm_model.activated.connect(lambda _i: self.llm_changed())
        self.llm_reasoning = QComboBox()
        for label, v in (("None (reasoning off)", "none"), ("Minimal", "minimal"), ("Low", "low"),
                         ("Medium", "medium"), ("Don't send a setting", "")):
            self.llm_reasoning.addItem(label, v)
        self.llm_reasoning.setToolTip("How much the model thinks before it answers: more is slower and dearer. "
                                      "None turns it off where the model allows (else its lowest is used); "
                                      "“Don't send” is for models that take no setting")
        self.llm_reasoning.activated.connect(lambda _i: self.llm_changed())
        self.llm_private = QCheckBox("Only providers that keep no data")
        self.llm_private.setToolTip("Zero data retention providers only: fewer to choose from, often slower")
        self.llm_private.clicked.connect(lambda _c: self.llm_changed())
        lf.addRow("Model", self.llm_model)
        self.llm_check = dim("")
        lf.addRow("", self.llm_check)
        lf.addRow("Reasoning", self.llm_reasoning)
        lf.addRow("", self.llm_private)
        ll.addLayout(lf)
        self.llm_note = dim("")
        ll.addWidget(self.llm_note)
        ll.addWidget(dim("It gets what you said, what's playing, the app names and up to 10 of your library's titles "
                         "that sound like what you said. Its answer is checked like every other: a title it names "
                         "still has to be found before anything plays. It also gets titles the search can't find, to "
                         "say what they were misheard from. Measured models: docs/research-2026-10-22.md."))
        ll.addStretch()
        self.tabs.addTab(lw, "LLM")

        # model
        mw = QWidget()
        ml = QVBoxLayout(mw)
        mf = QFormLayout()
        self.question = QLineEdit()
        self.min_conf = QDoubleSpinBox()
        self.min_conf.setRange(0, 1)
        self.min_conf.setSingleStep(0.05)
        self.min_conf.setToolTip("d1's confidence (0 = all options equally likely). Below it, the next step decides "
                                 "(or “Didn't get that”).")
        self.fallback_note = "the LLM and d1 off or unreachable: what the rules don't catch is treated as this"
        self.fallback = QComboBox()
        self.title_q = QLineEdit()
        self.cands = QSpinBox()
        self.cands.setRange(2, 8)
        for w in (self.question, self.title_q):
            w.textEdited.connect(self.mark_dirty)
        self.min_conf.valueChanged.connect(self.mark_dirty)
        self.cands.valueChanged.connect(self.mark_dirty)
        self.fallback.currentIndexChanged.connect(self.mark_dirty)
        self.hosted_model = QComboBox()
        self.hosted_model.setEditable(True)            # "d1", "d1-ultra" (Liquid's API) or an OpenRouter decision model
        self.hosted_model.addItems(DECISION_MODELS)
        self.hosted_model.setToolTip("d1 or d1-ultra through Liquid's API (Liquid key), or any of OpenRouter's "
                                     "decision models by id (OpenRouter key)")
        self.hosted_model.activated.connect(lambda _i: self.hosted_changed())
        self.hosted_model.lineEdit().editingFinished.connect(self.hosted_changed)
        mf.addRow("Hosted decision model", self.hosted_model)
        self.hosted_check = dim("")
        mf.addRow("", self.hosted_check)
        mf.addRow("Question d1 answers", self.question)
        mf.addRow("Act only above confidence", self.min_conf)
        mf.addRow("When no model answers", self.fallback)
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
        self.tabs.addTab(mw, "d1 model")

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
            self.diagram.set_counts(res["counts"])
            self.load_pipeline()
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
        self.fallback.addItem("nothing (“Didn't get that”)", "none")
        for k in m.get("intents", {}):
            if k != "none":
                self.fallback.addItem(k.replace("play_title", "play_title (play it as a title)"), k)
        self.fallback.setToolTip(self.fallback_note)
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

    # ---- the steps: order, on/off, which d1, the LLM (live at once) ----

    def load_pipeline(self):
        self.panel.bg.run(lambda: api("/v1/pipeline", timeout=5), self.show_pipeline)

    def show_pipeline(self, res, err):
        if err or not res:
            return
        self.pipeline = res
        c = res.get("counts", {})
        taught = f" + {res['learned']} taught" if res.get("learned") else ""
        subs = {"phrases": f"{c.get('phrases', 0)} sentences{taught}",
                "patterns": f"{c.get('patterns', 0)} rules, in order",
                "llm": res["llm_model"].split("/")[-1] if res["keys"]["openrouter"] else "no OpenRouter key (Settings)",
                "d1": f"{c.get('intents', 0)} intents" + (
                    f" · hosted: {res['hosted_d1_model']}" if res["d1_where"] != "local" else "")}
        self.diagram.set_pipeline(res["steps"], set(res["off"]), res["d1_where"], subs)
        self.llm_model.setCurrentText(res["llm_model"])
        self.llm_reasoning.setCurrentIndex(max(0, self.llm_reasoning.findData(res["llm_reasoning"])))
        self.llm_private.setChecked(res["llm_private"])
        self.hosted_model.setCurrentText(res["hosted_d1_model"])
        self.llm_note.setText("OpenRouter key set." if res["keys"]["openrouter"] else
                              f"<span style='color:{AMBER}'>No OpenRouter key yet: Settings › Set OpenRouter key."
                              "</span>")
        self.check_models(res["llm_model"], res["hosted_d1_model"])

    def check_models(self, llm: str, hosted: str):
        """Whether OpenRouter has the typed models in a form the agent can use, under each field."""
        def show(label, ok_text):
            ok, text = ok_text
            label.setText(f"<span style='color:{GREEN if ok else AMBER}'>{'✓' if ok else '✗'} {text}</span>")
        if self._checked.get("llm") != llm:
            self._checked["llm"] = llm
            self.llm_check.setText("Checking with OpenRouter…")
            self.panel.bg.run(lambda: openrouter_check(llm, "llm"), lambda r, e: show(self.llm_check, r or (False, str(e))))
        if self._checked.get("hosted") != hosted:
            self._checked["hosted"] = hosted
            if "/" not in hosted:
                self.hosted_check.setText("Liquid's own API (the Liquid key, Settings)")
            else:
                self.panel.bg.run(lambda: openrouter_check(hosted, "decision"),
                                  lambda r, e: show(self.hosted_check, r or (False, str(e))))

    def hosted_changed(self):
        model = self.hosted_model.currentText().strip()
        if model and model != self.pipeline.get("hosted_d1_model"):
            self.set_pipeline({"hosted_d1_model": model})

    def set_pipeline(self, change: dict, then=None):
        def done(res, err):
            if err:
                QMessageBox.warning(self, "TV Agent", f"Not changed: {err}")
                self.load_pipeline()
                return
            self.show_pipeline(res, None)
            self.status.setText(f"<span style='color:{GREEN}'>Steps saved · live now</span>")
            if then:
                then()

        self.panel.bg.run(lambda: api("/v1/pipeline", change, timeout=10), done)

    def _d1_server(self) -> str:
        """The local d1 server's state: "active", "inactive", "failed"…"""
        return (getattr(self.panel, "units", {}) or {}).get("llama-d1", {}).get("ActiveState", "unknown")

    def _offer_local(self):
        """The local d1 is about to be asked: offer to start its server when it isn't running."""
        if self._d1_server() in ("active", "activating", "reloading"):
            return
        box = QMessageBox(QMessageBox.Icon.Warning, "TV Agent",
                          "The local d1 server (llama-d1) isn't running, so the local d1 won't answer.",
                          parent=self)
        start = box.addButton("Start it", QMessageBox.ButtonRole.AcceptRole)
        boot = box.addButton("Start it, and at every boot", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() in (start, boot):
            verb = ("enable", "--now") if box.clickedButton() is boot else ("start",)
            self.panel.bg.run(lambda: systemctl(*verb, "llama-d1.service"), lambda *_: self.panel.refresh_units())

    def _offer_stop(self, why: str):
        """The local d1 isn't needed any more: offer to stop its server (frees its memory; a step to running the
        agent without this laptop's GPU)."""
        if self._d1_server() not in ("active", "activating"):
            return
        box = QMessageBox(QMessageBox.Icon.Question, "TV Agent",
                          f"{why} The local d1 server (llama-d1) isn't needed now. Stop it to free its memory?",
                          parent=self)
        stop = box.addButton("Stop it", QMessageBox.ButtonRole.AcceptRole)
        boot = box.addButton("Stop it, and don't start it at boot", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Keep it running", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() in (stop, boot):
            verb = ("disable", "--now") if box.clickedButton() is boot else ("stop",)
            self.panel.bg.run(lambda: systemctl(*verb, "llama-d1.service"), lambda *_: self.panel.refresh_units())

    def step_toggled(self, step: str, on: bool):
        keys = self.pipeline.get("keys", {})
        if on and step == "llm" and not keys.get("openrouter"):
            QMessageBox.information(self, "TV Agent", "The LLM step needs an OpenRouter key: Settings › Set "
                                    "OpenRouter key. Until then it's skipped.")
        off = set(self.pipeline.get("off", []))
        off = off - {step} if on else off | {step}
        if len(off) == 4:
            QMessageBox.information(self, "TV Agent", "Every step is off now: nothing will be understood until you "
                                    "switch one back on.")
        where = self.pipeline.get("d1_where", "local-first")
        then = None
        if step == "d1" and on and where != "hosted":
            then = self._offer_local
        elif step == "d1" and not on:
            then = lambda: self._offer_stop("The d1 step is off.")        # noqa: E731
        self.set_pipeline({"off": sorted(off)}, then)

    def where_changed(self, where: str):
        keys = self.pipeline.get("keys", {})
        if where != "local" and not keys.get("liquid"):
            QMessageBox.information(self, "TV Agent", "Liquid's hosted d1 needs a Liquid key: Settings › Set Liquid "
                                    "key. Until then only the local d1 is asked.")
        d1_on = "d1" not in self.pipeline.get("off", [])
        then = None
        if d1_on and where in ("local-first", "local", "hosted-first"):
            then = self._offer_local
        elif where == "hosted":
            then = lambda: self._offer_stop("Only Liquid's hosted d1 is asked now.")        # noqa: E731
        self.set_pipeline({"d1_where": where}, then)

    def llm_changed(self):
        change = {"llm_model": self.llm_model.currentText().strip(), "llm_reasoning": self.llm_reasoning.currentData(),
                  "llm_private": self.llm_private.isChecked()}
        if self.pipeline and all(self.pipeline.get(k) == v for k, v in change.items()):
            return
        self.set_pipeline(change)

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
        order = self.diagram.order
        num = lambda step: NUMS[order.index(step)] if step in order else ""      # noqa: E731
        step_of = {"phrase": "phrases", "pattern": "patterns", "llm": "llm", "model": "d1"}
        path, walked, states, notes, lines = ["input", "stt", "clean"], [], {}, {}, []
        skipped: list[str] = []          # pattern rules whose regex matched but whose parts didn't convert
        cleaned = trace[0].get("text", "")
        lines.append(f"Cleaned to “<b>{cleaned}</b>”" if cleaned else "Nothing left after clean-up")
        hit = ""
        for t in trace[1:]:
            st = t["stage"]
            if st == "split":
                left, right = t["parts"]
                lines.append(f"Two commands: “<b>{left}</b>” and “<b>{right}</b>”; “{t['known']}” is the rule "
                             f"<b>{t['rule']}</b>, the other goes through the steps:")
                continue
            if st == "fallback":
                if t.get("accepted"):
                    hit = "fallback"
                    lines.append(f"No model answered: treated as <b>{t['intent']}</b> (the d1 tab's “When no model "
                                 "answers”)")
                continue
            step = step_of.get(st)
            if not step:
                continue
            if step not in walked:
                walked.append(step)
            n = num(step)
            if t.get("off"):
                states[step] = "off"
                notes[step] = "off" if t.get("why") == "off" else "no OpenRouter key"
                lines.append(f"{n} <span style='color:gray'>{STEP_TITLE[step]}: "
                             f"{'off' if t.get('why') == 'off' else 'no key, skipped'}</span>")
            elif st == "phrase":
                if t.get("matched"):
                    hit, states[step] = step, "hit"
                    taught = t.get("rule") == "learned"
                    notes[step] = "matched: taught" if taught else f"matched: {t.get('rule')}"
                    lines.append(f"{n} " + ("A sentence you taught (Activity tab) matches exactly" if taught else
                                            f"Fixed phrase <b>{t.get('rule')}</b> matches exactly"))
                else:
                    states[step], notes[step] = "visited", "no exact match"
                    lines.append(f"{n} No fixed phrase is exactly this")
            elif st == "pattern":
                if not t.get("rule") or t.get("matched"):
                    if skipped:
                        lines.append(f"{n} <span style='color:gray'>{len(skipped)} rule{'s' * (len(skipped) > 1)} "
                                     f"matched the words but a part didn't convert: {', '.join(skipped)}</span>")
                        skipped.clear()
                if t.get("matched"):
                    hit, states[step] = step, "hit"
                    parts = ", ".join(f"{k} = “{v}”" for k, v in (t.get("parts") or {}).items())
                    notes[step] = f"matched: {t.get('rule')}"
                    lines.append(f"{n} Pattern rule <b>{t.get('rule')}</b> fits" + (f" ({parts})" if parts else ""))
                elif t.get("rule"):
                    skipped.append(t["rule"])
                elif states.get(step) != "hit":
                    states[step], notes[step] = "visited", "no rule fits"
                    lines.append(f"{n} No pattern rule fits")
            elif st == "llm":
                model = str(t.get("model", "")).split("/")[-1]
                if t.get("reached") is False:
                    states[step], notes[step] = "out", "didn't answer"
                    lines.append(f"{n} <span style='color:{AMBER}'>The LLM ({model}) didn't answer</span>; the next "
                                 "step decides")
                    continue
                said = str(t.get("action", "none")).replace("_", " ")
                if t.get("action") not in (None, "none"):
                    hit, states[step], notes[step] = step, "hit", f"answered: {said}"
                else:
                    states[step], notes[step] = "visited", "not a command"
                lines.append(f"{n} The LLM ({model}) answered <b>{said}</b>, checked like the rest")
            elif st == "model":
                if t.get("available") is False:
                    states[step], notes[step] = "out", "didn't answer"
                    lines.append(f"{n} <span style='color:{AMBER}'>d1 didn't answer</span> "
                                 f"({dict(WHERE).get(t.get('where'), '').lower()})")
                    continue
                top = " · ".join(f"{k} {p:.2f}" for k, p in t.get("top", []))
                who = "Liquid's hosted d1" if t.get("source") == "hosted" else "the local d1"
                lines.append(f"{n} {who} picked <b>{t.get('intent')}</b> with confidence "
                             f"{t.get('confidence', 0):.2f} (needs ≥ {t.get('threshold', 0):.2f}) — top: {top}")
                if t.get("accepted"):
                    hit, states[step] = step, "hit"
                    notes[step] = f"{t.get('intent')} {t.get('confidence', 0):.2f}"
                else:
                    states[step] = "visited"
                    notes[step] = f"{t.get('intent')} {t.get('confidence', 0):.2f}: not sure enough"
        a = res["action"]
        if a["type"] == "none":
            path.append("unclear")
            lines.append(f"→ <span style='color:{AMBER}'><b>Nothing happens</b></span> (“Didn't get that”)")
        else:
            path += ["action", "tv"]
            if a["type"] == "sequence":
                what = " + ".join(x["type"].replace("_", " ") for x in a.get("steps", []))
                fields = ""
            else:
                what = a["type"].replace("_", " ")
                fields = ", ".join(f"{k} {v}" for k, v in a.items() if k != "type" and v not in (None, False, ""))
            notes["action"] = what
            notes["tv"] = res.get("describe", "")
            lines.append(f"→ <span style='color:{GREEN}'><b>{what}</b></span> {fields} — the TV would show "
                         f"“{res.get('describe', '')}”")
        self.diagram.show_trace(path, walked, states, hit, notes)
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
        self.tabs.setCurrentIndex(TAB_FOR["phrases"])
        self.phrases.add_row({"id": "_".join(text.split()[:3]), "say": [text], "do": action,
                              "note": "added from the tester"})
        self.status.setText("<span style='color:#e0a030'>● Added a phrase: check its action, then Save</span>")
