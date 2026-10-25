"""Backup card (Settings tab): the last backup of this computer's setup (the agent's backup.py: settings, API keys,
pairings, taught commands, voice tests), its folder and the daily timer, Back up now, and Restore… with the dry run's
list and a confirmation. The list and the dry run are read here; making and restoring one is `tv-agent backup`'s
(a restore backs up what's here first, and stops and starts the voice agent, only while nothing plays)."""
import html
import json
import subprocess
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                               QListWidget, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .common import AMBER, GREEN, RED, TV_AGENT, Say, agent_backup, card, dim, human, systemctl

TIMER = "tv-agent-backup.timer"


def when(t: float) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(t))


def tv_agent(*args: str, timeout: float = 300) -> dict:
    """`tv-agent backup …` with --json: its answer, or {"error"}."""
    r = subprocess.run([str(TV_AGENT), *args, "--json"], capture_output=True, text=True, timeout=timeout)
    for line in reversed(r.stdout.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"error": (r.stderr.strip().splitlines() or [f"tv-agent exited with {r.returncode}"])[-1]}


def state() -> dict:
    """What the card shows: the folder, its backups and their space, the timer, what else covers the folder."""
    s = agent_backup.settings()
    try:
        enabled = systemctl("is-enabled", TIMER, timeout=5)[1].strip() == "enabled"
    except (OSError, subprocess.TimeoutExpired):
        enabled = False
    return {"settings": s, "backups": agent_backup.backups(s["dir"]), "usage": agent_backup.usage(s["dir"]),
            "timer": enabled, "covered": agent_backup.covered(s["dir"])}


class BackupCard(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.note = Say(panel)
        self.found: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        box = card()
        lay.addWidget(box)
        v = QVBoxLayout(box)
        v.setContentsMargins(16, 14, 16, 14)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Backup</b> <span style='color:gray'>· settings, API keys, pairings, taught "
                              "commands and voice tests, on this computer</span>"), 1)
        self.now_button = QPushButton("Back up now")
        self.now_button.setToolTip("tv-agent backup: none when nothing changed since the last one")
        self.now_button.clicked.connect(self.back_up)
        self.restore_button = QPushButton("Restore…")
        self.restore_button.clicked.connect(self.restore)
        head.addWidget(self.now_button)
        head.addWidget(self.restore_button)
        v.addLayout(head)
        self.last = QLabel("")
        self.last.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.last)
        self.auto = QLabel("")
        self.auto.setTextFormat(Qt.TextFormat.RichText)
        self.auto.linkActivated.connect(lambda _l: self.turn_on())
        v.addWidget(self.auto)
        self.folder = QLabel("")
        self.folder.setTextFormat(Qt.TextFormat.RichText)
        self.folder.setWordWrap(True)
        self.folder.linkActivated.connect(self.folder_link)
        v.addWidget(self.folder)
        self.warning = QLabel("")
        self.warning.setTextFormat(Qt.TextFormat.RichText)
        self.warning.setWordWrap(True)
        self.warning.setVisible(False)
        v.addWidget(self.warning)
        v.addWidget(dim("Private (the folder 700, its files 600): it holds the API keys, the pairing tokens, the "
                        "phones' TLS key and the Android signing key, so it's never uploaded anywhere. Left out: the "
                        "models and caches, downloaded or made again. What rarely changes, like the voice tests, is "
                        "kept once and linked from each backup."))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        self.panel.bg.run(state, self.show_state)

    def show_state(self, res, err=None):
        if err or not res:
            self.last.setText(f"<span style='color:gray'>Can't read the backups: {html.escape(str(err))}</span>")
            return
        s = res["settings"]
        self.found = res["backups"]
        regular = [b for b in self.found if b["reason"] != agent_backup.SAFETY]
        if regular:
            b = regular[0]
            self.last.setText(f"<span style='color:{GREEN}'>✔</span>&nbsp; Last backup <b>{when(b['t'])}</b> · "
                              f"{human(b['size'])}, {b['files']} files <span style='color:gray'>· {len(self.found)} "
                              f"kept, {human(res['usage'])} on disk</span>")
        else:
            self.last.setText(f"<span style='color:{AMBER}'>⚠</span>&nbsp; No backup yet")
        self.restore_button.setEnabled(bool(self.found))
        self.auto.setText(f"Every day at {agent_backup.DAILY_AT}, none when nothing changed "
                          f"<span style='color:gray'>({TIMER})</span>" if res["timer"] else
                          f"<span style='color:{AMBER}'>Not automatic</span> · <a href='on'>back up every day</a>")
        self.folder.setText(f"Folder: <a href='open'>{html.escape(str(s['dir']))}</a> · <a href='change'>Change…</a> "
                            f"<span style='color:gray'>· kept: the newest of each of the last {s['keep_daily']} days "
                            f"and {s['keep_weekly']} weeks, and the last 3 made before a restore</span>")
        self.warning.setVisible(bool(res["covered"]))
        self.warning.setText(f"<span style='color:{RED}'>⚠ {html.escape('; '.join(res['covered']))} covers this "
                             "folder: it could copy these keys off this computer. Choose a folder outside it.</span>")

    def folder_link(self, link: str):
        folder = agent_backup.settings()["dir"]
        if link == "open":
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        path = QFileDialog.getExistingDirectory(self, "Folder for the backups", str(folder))
        if path:
            agent_backup.save_settings(dir=path)
            self.note.setText(f"Backups go to {path} from now on (the ones in {folder} stay there)")
            self.refresh()

    def turn_on(self):
        def run():
            r = subprocess.run([str(TV_AGENT), "setup", "backups", "--enable"], capture_output=True, text=True,
                               timeout=60)
            return r.returncode, (r.stdout + r.stderr).strip().splitlines()[-1:]

        def done(res, err):
            code, out = res if res else (1, [str(err)])
            self.note.setText(f"Backups: every day at {agent_backup.DAILY_AT}" if code == 0 else
                              f"Not turned on: {(out or ['?'])[0]}")
            self.refresh()
        self.panel.bg.run(run, done)

    def back_up(self):
        self.now_button.setEnabled(False)
        self.note.setText("Backing up…")

        def done(res, err):
            self.now_button.setEnabled(True)
            res = res or {"error": str(err)}
            if res.get("error"):
                self.note.setText(f"Not backed up: {res['error']}")
            elif res.get("skipped"):
                self.note.setText(f"Nothing changed since the last backup ({when(res['last']['t'])}): it's current")
            else:
                self.note.setText(f"Backed up: {res['files']} files, {human(res['size'])} ({human(res['new'])} new) "
                                  f"in {res['folder']}")
            self.refresh()
        self.panel.bg.run(lambda: tv_agent("backup"), done)

    def restore(self):
        if self.found:
            RestoreDialog(self, self.found).exec()


class RestoreDialog(QDialog):
    """A backup and its parts, what restoring them would change (the dry run, read here), then the restore itself
    (tv-agent backup restore, after a confirmation)."""

    def __init__(self, card_: BackupCard, found: list[dict]):
        super().__init__(card_)
        self.card = card_
        self.plan: dict | None = None
        self.setWindowTitle("Restore a backup")
        self.resize(760, 560)
        v = QVBoxLayout(self)
        self.which = QComboBox()
        for b in found:
            why = " · made before a restore" if b["reason"] == agent_backup.SAFETY else ""
            self.which.addItem(f"{when(b['t'])} · {human(b['size'])}, {b['files']} files{why}", b["name"])
        v.addWidget(self.which)
        parts = QHBoxLayout()
        self.parts = {}
        for key, label in agent_backup.PARTS.items():
            box = QCheckBox(label.split(" (")[0])
            box.setToolTip(label)
            box.setChecked(True)
            box.toggled.connect(self.dry_run)
            self.parts[key] = box
            parts.addWidget(box)
        v.addLayout(parts)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.summary)
        self.list = QListWidget()
        v.addWidget(self.list, 1)
        v.addWidget(dim("Files made since the backup are left as they are. What's here now is backed up first "
                        "(“made before a restore”), so a restore can be undone the same way."))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.go = self.buttons.addButton("Restore", QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setEnabled(False)
        self.buttons.accepted.connect(self.confirm)
        self.buttons.rejected.connect(self.reject)
        v.addWidget(self.buttons)
        self.which.currentIndexChanged.connect(self.dry_run)
        self.dry_run()

    def chosen(self) -> list[str]:
        return [k for k, box in self.parts.items() if box.isChecked()]

    def dry_run(self, *_):
        self.go.setEnabled(False)
        self.list.clear()
        self.summary.setText("Comparing with what's here…")
        name, only = self.which.currentData(), self.chosen()
        if not only:
            self.summary.setText("Choose what to restore")
            return
        self.card.panel.bg.run(lambda: agent_backup.plan(name, only), self.show_plan)

    def show_plan(self, plan, err=None):
        if err or not plan:
            self.summary.setText(f"<span style='color:{RED}'>Can't read it: {html.escape(str(err))}</span>")
            return
        if plan["backup"] != self.which.currentData() or plan["only"] != self.chosen():
            return                      # a later choice's answer is on its way
        self.plan = plan
        for c in plan["changes"]:
            self.list.addItem(f"{'New' if c['change'] == 'new' else 'Changed'}   {c['path']}   ·   "
                              f"{c['what'] or 'file'}, {human(c['size'])}")
        v = plan["versions"]
        made = f"Made with agent {v.get('agent')}" + (f" ({v['commit']})" if v.get("commit") else "")
        kept = f"; {len(plan['kept'])} files made since stay as they are" if plan["kept"] else ""
        self.summary.setText(f"{made}. Restoring writes <b>{len(plan['changes'])} files</b> "
                             f"({plan['same']} are as they are here{kept})." if plan["changes"] else
                             f"{made}. Nothing to restore: the {plan['same']} files chosen are as they are here.")
        self.go.setEnabled(bool(plan["changes"]))

    def confirm(self):
        plan = self.plan
        if not plan or not plan["changes"]:
            return
        if QMessageBox.question(self, "TV Agent", f"Restore {len(plan['changes'])} files from the backup of "
                                f"{when(plan['t'])}? What's here now is backed up first, and the voice agent "
                                "restarts (not while something plays on the TV).") != QMessageBox.StandardButton.Yes:
            return
        self.go.setEnabled(False)
        self.summary.setText("Restoring…")
        name, only = plan["backup"], ",".join(self.chosen())
        self.card.panel.bg.run(lambda: tv_agent("backup", "restore", name, "--only", only, "--yes"), self.done)

    def done(self, res, err=None):
        res = res or {"error": str(err)}
        if res.get("error"):
            self.summary.setText(f"<span style='color:{RED}'>Not restored: {html.escape(res['error'])}</span>")
            self.go.setEnabled(True)
            return
        restored = res.get("restored") or []
        msg = (f"Restored {len(restored)} files, each checked against the backup; what was here is in "
               f"{res.get('safety')}. Voice agent: {res.get('agent')}.")
        if "config/panel.conf" in restored:
            msg += " Restart the panel for its restored preferences."
        self.card.note.setText(msg)
        self.card.refresh()
        settings = getattr(self.card.panel, "settings", None)
        if settings is not None and not settings.dirty:
            settings.load()
        QMessageBox.information(self, "TV Agent", msg)
        self.accept()
