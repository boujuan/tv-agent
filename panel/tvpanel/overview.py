"""Overview tab: is voice control on, what's playing, the two services with live resource graphs, processes."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .common import (AMBER, BLUE, GREEN, GREY, ICON, ICON_SIZE, PURPLE, RED, SERVICES, card, dim, human, metric,
                     set_badge, systemctl, uptime)


class ServiceCard(QFrame):
    def __init__(self, panel, unit: str, title: str, about: str):
        super().__init__()
        self.setObjectName("card")
        self.panel, self.unit = panel, unit
        self.info: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        names = QVBoxLayout()
        names.setSpacing(1)
        t = QLabel(f"<b>{title}</b> <span style='color:gray'>· {unit}.service</span>")
        t.setObjectName("cardTitle")
        names.addWidget(t)
        names.addWidget(dim(about))
        self.state = QLabel()
        top.addLayout(names, 1)
        top.addWidget(self.state, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        cpu_box, self.cpu, self.cpu_spark = metric("CPU", BLUE, 2.0)
        mem_box, self.mem, self.mem_spark = metric("Memory", GREEN, 50.0)
        grid.addWidget(cpu_box, 0, 0)
        grid.addWidget(mem_box, 0, 1)
        self.gpu = None
        if unit == "llama-d1":
            gpu_box, self.gpu, self.gpu_spark = metric("GPU memory · busy", PURPLE, 2.0)
            grid.addWidget(gpu_box, 0, 2)
        for c in range(grid.columnCount()):
            grid.setColumnStretch(c, 1)
        lay.addLayout(grid)

        bottom = QHBoxLayout()
        self.detail = dim("–")
        self.boot = QCheckBox("Start at boot")
        self.boot.setToolTip("systemctl --user enable/disable (linger is on, so it starts without logging in)")
        self.boot.clicked.connect(self.toggle_boot)
        self.toggle = QPushButton("Stop")
        self.toggle.clicked.connect(self.start_stop)
        self.restart = QPushButton("Restart")
        self.restart.clicked.connect(lambda: self.act("restart"))
        bottom.addWidget(self.detail, 1)
        bottom.addWidget(self.boot)
        bottom.addSpacing(8)
        bottom.addWidget(self.restart)
        bottom.addWidget(self.toggle)
        lay.addLayout(bottom)

    @property
    def active(self) -> bool:
        return self.info.get("ActiveState") == "active"

    def show_info(self, info: dict):
        self.info = info
        state = info.get("ActiveState", "?")
        color = {"active": GREEN, "activating": AMBER, "deactivating": AMBER, "failed": RED}.get(state, GREY)
        label = {"active": "Running", "inactive": "Stopped", "failed": "Failed", "activating": "Starting…",
                 "deactivating": "Stopping…"}.get(state, state)
        set_badge(self.state, label, color)
        self.toggle.setEnabled(True)
        self.toggle.setText("Stop" if state in ("active", "activating") else "Start")
        self.restart.setEnabled(state == "active")
        self.boot.setChecked(info.get("UnitFileState") == "enabled")
        if state == "active":
            n = len(info.get("pids", []))
            self.detail.setText(f"{uptime(info.get('ActiveEnterTimestamp', ''))} · PID {info.get('MainPID')} · "
                                f"{n} process{'es' if n != 1 else ''}")
        else:
            self.detail.setText("Not running: uses no CPU, memory or GPU")
            for w in (self.cpu, self.mem) + ((self.gpu,) if self.gpu else ()):
                w.setText("–")

    def show_usage(self, cpu: float, rss: int, gpu_mem: int | None, gpu_busy: float | None):
        self.cpu.setText(f"{cpu:.1f} %")
        self.cpu.setToolTip("Share of all CPU threads on this laptop")
        self.cpu_spark.push(cpu)
        self.mem.setText(human(rss))
        self.mem_spark.push(rss / 2**20)
        if self.gpu is not None and gpu_mem is not None:
            self.gpu.setText(f"{human(gpu_mem)} · {gpu_busy:.0f} %")
            self.gpu_spark.push(gpu_busy or 0)

    def start_stop(self):
        self.act("stop" if self.active or self.info.get("ActiveState") == "activating" else "start")

    def act(self, verb: str):
        self.toggle.setEnabled(False)
        self.restart.setEnabled(False)
        set_badge(self.state, {"stop": "Stopping…", "start": "Starting…", "restart": "Restarting…"}[verb], AMBER)

        def done(res, err):
            ok, out = res if res else (False, str(err))
            if not ok:
                QMessageBox.warning(self, "TV Agent", f"systemctl --user {verb} {self.unit} failed:\n\n{out}")
            self.panel.refresh_units()

        self.panel.bg.run(systemctl, done, verb, f"{self.unit}.service")

    def toggle_boot(self, checked: bool):
        def done(res, err):
            ok, out = res if res else (False, str(err))
            if not ok:
                QMessageBox.warning(self, "TV Agent", out)
            self.panel.refresh_units()

        self.panel.bg.run(systemctl, done, "enable" if checked else "disable", f"{self.unit}.service")


class Overview(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        banner = card()
        b = QHBoxLayout(banner)
        b.setContentsMargins(18, 14, 18, 14)
        icon = QLabel()
        icon.setPixmap(QIcon(str(ICON)).pixmap(ICON_SIZE))
        text = QVBoxLayout()
        text.setSpacing(2)
        self.headline = QLabel("…")
        self.headline.setObjectName("headline")
        self.tv = dim("")
        self.playing = QLabel("")
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setVisible(False)
        for w in (self.headline, self.tv, self.playing, self.warning):
            text.addWidget(w)
        btns = QVBoxLayout()
        self.start_all = QPushButton("Start all")
        self.stop_all = QPushButton("Stop all · free resources")
        self.start_all.clicked.connect(lambda: self.all("start"))
        self.stop_all.clicked.connect(lambda: self.all("stop"))
        btns.addWidget(self.start_all)
        btns.addWidget(self.stop_all)
        b.addWidget(icon)
        b.addSpacing(10)
        b.addLayout(text, 1)
        b.addLayout(btns)
        lay.addWidget(banner)

        self.cards = {unit: ServiceCard(panel, unit, title, about) for unit, title, about in SERVICES}
        for c in self.cards.values():
            lay.addWidget(c)

        procs = card()
        p = QVBoxLayout(procs)
        p.setContentsMargins(16, 12, 16, 12)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Service", "Process", "PID", "CPU", "Memory", "GPU memory"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setFixedHeight(120)
        p.addWidget(QLabel("<b>Processes</b>"))
        p.addWidget(self.table)
        lay.addWidget(procs)
        lay.addStretch()
        self.footer = dim("")
        lay.addWidget(self.footer)

    def all(self, verb: str):
        self.panel.all_services(verb)

    def show_headline(self):
        states = [c.active for c in self.cards.values()]
        self.headline.setText("Voice control is on" if all(states) else
                              "Voice control is partly on" if any(states) else "Voice control is off")
        self.stop_all.setEnabled(any(states))
        self.start_all.setEnabled(not all(states))
        if not self.cards["tv-agent"].active:
            self.tv.setText("The mic button shows “Agent offline” while the voice agent is stopped")
            self.playing.setText("")

    def show_rows(self, rows: list[tuple]):
        t = self.table
        if not rows:
            t.setRowCount(1)
            t.setItem(0, 0, QTableWidgetItem("Nothing running"))
            for j in range(1, 6):
                t.setItem(0, j, QTableWidgetItem(""))
            return
        t.setRowCount(len(rows))
        for i, (unit, name, pid, c, m, g) in enumerate(rows):
            for j, v in enumerate((unit, name, str(pid), f"{c:.1f} %", human(m), human(g) if g else "–")):
                item = QTableWidgetItem(v)
                if j >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                t.setItem(i, j, item)
