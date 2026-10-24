"""Quality: how well the agent does, from the history's traces (over today, the last 7 days or all, from the TV, the
phones, typed…) and the nightly check (server/src/tv_agent/quality.py): each step's success and time, who decided and
how often rightly, the models' calls and cost, the cost per day, the commonest failures (double-click: an example in
Decisions), and the nightly runs against their baselines (regressions, fixes, cost; run it now, accept a run as the
baseline)."""
import html
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QScrollArea,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .common import AMBER, GREEN, RED, TV_AGENT, agent_activity, agent_quality, card, dim, systemctl

PERIODS = [("Today", 1), ("Last 7 days", 7), ("Last 30 days", 30), ("All", 0)]
SOURCES = [("All sources", None), ("TV", {"tv"}), ("Phone", {"phone"}), ("Typed", {"text"}),
           ("Laptop mic", {"laptop"})]


def _ms(v) -> str:
    return "" if v is None else f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms"


def _rate(v) -> str:
    return "" if v is None else f"{100 * v:.0f}%"


def table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for i in range(1, len(headers)):
        t.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    return t


def fill(t: QTableWidget, rows: list[list], colors: dict[int, str] | None = None):
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            item = QTableWidgetItem(str(v))
            if c:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if colors and r in colors:
                item.setForeground(Qt.GlobalColor.red if colors[r] == RED else Qt.GlobalColor.darkGreen)
            t.setItem(r, c, item)
    t.setFixedHeight(min(28 + 30 * max(len(rows), 1), 420))


class Quality(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.entries: dict[float, dict] = {}
        self.run_files: list[str] = []
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        top = QHBoxLayout()
        self.period = QComboBox()
        self.period.addItems([p for p, _ in PERIODS])
        self.period.setCurrentIndex(1)
        self.source = QComboBox()
        self.source.addItems([s for s, _ in SOURCES])
        for w in (self.period, self.source):
            w.currentIndexChanged.connect(self.load)
        again = QPushButton("Refresh")
        again.clicked.connect(self.load)
        self.counted = dim("")
        for w in (self.period, self.source, again):
            top.addWidget(w)
        top.addWidget(self.counted, 1)
        lay.addLayout(top)
        self.alert = QLabel("")
        self.alert.setWordWrap(True)
        lay.addWidget(self.alert)

        self.steps = table(["Step", "Right", "Asked", "Of", "Rate", "p50", "p95"])
        self.deciders = table(["Decided by", "Commands", "Right", "Rate"])
        self.models = table(["Model", "Calls", "Cost", "p50"])
        self.costs = table(["Day", "Cost"])
        self.failures = table(["Failure", "Count"])
        self.failures.setToolTip("Double-click: an example in Decisions, as it happened")
        self.failures.cellDoubleClicked.connect(self.open_failure)
        for title, about, w in (
                ("Each step", "How often each step went right, and how long it took. “Asked”: it showed a list to "
                              "pick from instead of guessing.", self.steps),
                ("Who decided", "The rule or model that decided each command, and how often that was right (it "
                                "worked and wasn't marked wrong).", self.deciders),
                ("Models", "Each model's calls, cost and typical time.", self.models),
                ("Cost per day", "", self.costs),
                ("Commonest failures", "What failed most; double-click one for an example.", self.failures)):
            box = card()
            bl = QVBoxLayout(box)
            bl.addWidget(QLabel(f"<b>{title}</b>"))
            if about:
                bl.addWidget(dim(about))
            bl.addWidget(w)
            lay.addWidget(box)

        box = card()
        bl = QVBoxLayout(box)
        bl.addWidget(QLabel("<b>Nightly check</b>"))
        bl.addWidget(dim("Every night at 04:30 (tv-agent-nightly.timer): your voice tests through the agent's own "
                         "speech to text, the sentences and chains, the episode and scene searches, each case against "
                         "the baseline. Nothing is sent to the TV; the models cost what the run shows. A regression "
                         "notifies you here and on paired phones."))
        self.runs = table(["Night", "Voice tests", "Sentences", "Episodes, scenes", "Regressions", "Fixed", "Cost"])
        self.runs.itemSelectionChanged.connect(self.show_run)
        bl.addWidget(self.runs)
        self.run_text = QLabel("")
        self.run_text.setWordWrap(True)
        self.run_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self.run_text)
        row = QHBoxLayout()
        self.run_now = QPushButton("Run it now")
        self.run_now.setToolTip("Starts tv-agent-nightly.service (a few minutes; nothing is sent to the TV)")
        self.run_now.clicked.connect(self.start_run)
        self.accept_btn = QPushButton("Accept as the baseline")
        self.accept_btn.setToolTip("After a change made on purpose: this run's results become what's compared with")
        self.accept_btn.clicked.connect(self.accept)
        self.setup_btn = QPushButton("Set up the nightly check")
        self.setup_btn.clicked.connect(self.set_up)
        for w in (self.run_now, self.accept_btn, self.setup_btn):
            row.addWidget(w)
        row.addStretch(1)
        bl.addLayout(row)
        lay.addWidget(box)
        lay.addStretch(1)

    def showEvent(self, event):
        super().showEvent(event)
        self.load()

    def load(self):
        days = PERIODS[self.period.currentIndex()][1]
        sources = SOURCES[self.source.currentIndex()][1]

        def work():
            rows = agent_activity.read()
            picked = agent_quality.pick(rows, time.time() - days * 86400 if days else None, sources)
            return picked, agent_quality.summary(picked), agent_quality.runs(), agent_quality.alert()
        self.panel.bg.run(work, self.display)

    def display(self, got, err=None):
        if err or not got:
            self.counted.setText(f"<span style='color:{RED}'>{html.escape(str(err))}</span>" if err else "")
            return
        picked, s, runs, alert = got
        self.entries = {e["t"]: e for e in picked}
        self.counted.setText(f"{s['n']} commands")
        fill(self.steps, [[x["label"], x["right"], x["asked"], x["n"], _rate(x["rate"]), _ms(x["p50"]), _ms(x["p95"])]
                          for x in s["steps"] if x["n"]])
        fill(self.deciders, [[x["key"], x["n"], x["right"], _rate(x["rate"])] for x in s["deciders"]])
        fill(self.models, [[x["model"].split("/")[-1], x["calls"], f"${x['cost']:.4f}", _ms(x["p50"])]
                           for x in s["models"]])
        fill(self.costs, [[d, f"${c:.4f}"] for d, c in s["cost_by_day"][:30]])
        self.failure_rows = s["failures"]
        fill(self.failures, [[x["reason"], x["n"]] for x in s["failures"][:30]])
        self.run_files = [r["file"] for r in runs]
        self.run_data = runs

        def bench(r, name):
            b = r["benches"].get(name) or {}
            return "error" if b.get("error") else f"{b['right']}/{b['of']}" if b else ""
        fill(self.runs, [[r["file"][:10] + " " + r["file"][11:13] + ":" + r["file"][13:15], bench(r, "voice"),
                          bench(r, "chains"), bench(r, "finding"), r.get("regressions", 0),
                          sum(len(b.get("fixes") or []) for b in r["benches"].values()), f"${r.get('cost', 0):.4f}"]
                         for r in runs], {i: RED for i, r in enumerate(runs) if r.get("regressions")})
        installed, _ = systemctl("cat", "tv-agent-nightly.timer", timeout=5)
        self.setup_btn.setVisible(not installed)
        self.run_now.setEnabled(installed)
        if alert:
            self.alert.setText(f"<span style='color:{RED}'><b>The nightly check of {alert['run'][:10]} found "
                               f"{alert['regressions']} regression{'s' * (alert['regressions'] > 1)}</b></span> "
                               "(below); fix them, or accept the run as the baseline if the change was on purpose.")
            agent_quality.seen()
        else:
            self.alert.setText("")

    def open_failure(self, row: int, _col: int):
        if 0 <= row < len(self.failure_rows) and (e := self.entries.get(self.failure_rows[row]["example"])):
            self.panel.show_recorded(e)

    def show_run(self):
        rows = {i.row() for i in self.runs.selectedItems()}
        if not rows:
            self.run_text.setText("")
            return
        r = self.run_data[min(rows)]
        parts = []
        for name, b in r["benches"].items():
            title = agent_quality.BENCHES.get(name, name)
            if b.get("error"):
                parts.append(f"<b>{title}</b>: <span style='color:{RED}'>{html.escape(b['error'])}</span>")
                continue
            parts.append(f"<b>{title}</b>: {b['right']}/{b['of']} right, ${b.get('cost', 0):.4f}, "
                         f"{b.get('seconds', 0)} s, against the baseline of {b.get('baseline', '?')}")
            for k in b.get("regressions") or []:
                parts.append(f"&nbsp;&nbsp;<span style='color:{RED}'>✗ now wrong: {html.escape(k)}</span>")
            for k in b.get("fixes") or []:
                parts.append(f"&nbsp;&nbsp;<span style='color:{GREEN}'>✓ now right: {html.escape(k)}</span>")
            if b.get("new"):
                parts.append(f"&nbsp;&nbsp;<span style='color:{AMBER}'>{len(b['new'])} new cases</span>")
        self.run_text.setText("<br>".join(parts))

    def start_run(self):
        ok, out = systemctl("start", "--no-block", "tv-agent-nightly.service")
        self.panel.say("The nightly check is running (a few minutes)" if ok else f"Not started: {out}", 8000)

    def accept(self):
        rows = {i.row() for i in self.runs.selectedItems()}
        if not rows:
            QMessageBox.information(self, "TV Agent", "Pick a night first.")
            return
        f = self.run_files[min(rows)]
        if QMessageBox.question(self, "TV Agent", f"Compare the next nights with {f[:10]}'s results?") \
                != QMessageBox.StandardButton.Yes:
            return
        done = agent_quality.accept(f)
        self.panel.say(f"Baseline now {f[:10]}'s: {', '.join(done)}", 6000)
        self.load()

    def set_up(self):
        import subprocess
        r = subprocess.run([str(TV_AGENT), "setup", "nightly", "--enable"], capture_output=True, text=True,
                           timeout=60)
        self.panel.say("The nightly check is set up (04:30)" if r.returncode == 0 else
                       f"Not set up: {(r.stdout + r.stderr).strip()[-200:]}", 8000)
        self.load()
