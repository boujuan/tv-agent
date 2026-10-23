"""Updates card (Settings tab): the newest releases on GitHub against this computer's code, the TV app and the
phones. The voice agent asks GitHub (its updates.py, every few hours) and installs the TV app's release over ADB;
this computer's copy is moved up to a newer release tag here, only as a fast-forward of a clean main (never over
work that isn't on GitHub); each phone updates itself (the phone app's More › About)."""
import html
import os
import shutil
import subprocess
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QWidget

from . import __version__
from .common import AMBER, GREEN, REPO, api, card, dim
from .phones import ago

MODES = (("auto", "Install updates by themselves"), ("notify", "Check, and tell me"), ("off", "Don't check"))
RELEASE_TAGS = ("server-v*", "panel-v*")


def git(*args: str, timeout: float = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, type(e).__name__
    return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()


def checkout() -> dict:
    """This copy against the release tags on GitHub: the newest one it doesn't have yet ("tag"), and why it can't be
    moved there by itself ("why": None when it can)."""
    if git("rev-parse", "--git-dir")[0]:
        return {"error": "this copy isn't a git checkout: update it the way it was installed"}
    code, out = git("fetch", "--quiet", "--tags", "origin", "main", timeout=60)
    if code:
        return {"error": f"can't reach GitHub ({out.splitlines()[-1] if out else 'git fetch failed'})"}
    _, tags = git("tag", "--list", *RELEASE_TAGS, "--no-merged", "HEAD")      # not in this copy yet
    released = [t for t in tags.split() if git("merge-base", "--is-ancestor", t, "origin/main")[0] == 0]
    if not released:
        return {"tag": None}
    tag = max(released, key=lambda t: int(git("log", "-1", "--format=%ct", t)[1] or 0))
    branch = git("branch", "--show-current")[1]
    why = None
    if branch != "main":
        why = f"this copy is on the branch {branch or '(none)'}, not main"
    elif git("status", "--porcelain", "--untracked-files=no")[1]:
        why = "this copy has uncommitted changes"
    elif git("merge-base", "--is-ancestor", "HEAD", tag)[0]:
        why = "this copy has commits that aren't on GitHub"
    return {"tag": tag, "tags": sorted(released), "why": why}


def update_checkout(tag: str) -> str:
    """Fast-forward to the release tag, then the voice agent's packages as its uv.lock says."""
    code, out = git("merge", "--ff-only", tag, timeout=60)
    if code:
        raise RuntimeError(out.splitlines()[-1] if out else "git merge failed")
    uv = shutil.which("uv")
    if uv:
        r = subprocess.run([uv, "sync", "--quiet"], cwd=REPO / "server", capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise RuntimeError(f"updated to {tag}, but uv sync failed: {(r.stderr.strip().splitlines() or ['?'])[-1]}")
    return tag


class UpdatesCard(QWidget):
    def __init__(self, settings, panel):
        super().__init__()
        self.settings, self.panel = settings, panel
        self.state: dict = {}           # the agent's /v1/updates
        self.copy: dict = {}            # checkout()
        self.updated_to = None          # a tag this panel moved to (it runs the old code until restarted)
        self.checked = 0.0              # when this card last asked (showing the tab again within a minute doesn't)
        box = card()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(box)
        f = QFormLayout(box)
        f.setContentsMargins(16, 14, 16, 14)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Updates</b> <span style='color:gray'>· new releases on <a href='https://github.com/"
                              "boujuan/tv-agent/releases'>GitHub</a></span>"), 1)
        head.itemAt(0).widget().setOpenExternalLinks(True)
        self.check_button = QPushButton("Check now")
        self.check_button.clicked.connect(lambda: self.check(force=True))
        head.addWidget(self.check_button)
        f.addRow(head)
        self.mode = QComboBox()                 # saved with the window's Save (config.toml's updates)
        for value, label in MODES:
            self.mode.addItem(label, value)
        self.mode.setToolTip("By themselves: the voice agent installs the TV app's new release when it isn't "
                             "listening, and this computer moves to a new release tag when nothing here is unsaved "
                             "or unpushed. Phones have their own switch (More › About in the app).")
        f.addRow("Updates", self.mode)
        self.rows = {}
        for key, label, button, fn in (("computer", "This computer", "Update this computer", self.update_computer),
                                       ("tv", "TV app", "Update the TV app", self.update_tv),
                                       ("phone", "Phone app", None, None)):
            line = QHBoxLayout()
            text = QLabel("…")
            text.setWordWrap(True)
            line.addWidget(text, 1)
            b = None
            if button:
                b = QPushButton(button)
                b.clicked.connect(fn)
                b.setVisible(False)
                line.addWidget(b)
            self.rows[key] = (text, b)
            f.addRow(label, line)
        self.status = dim("")
        f.addRow(self.status)

    def check(self, force: bool = False, quiet: bool = False):
        """The agent's answer (it asks GitHub at most once a minute) and this checkout's tags; with mode "auto" a
        newer release is installed here. `quiet`: the panel's own check at start, said only when there's news."""
        if self.mode.currentData() == "off" and not force:
            self.status.setText("Not checking (Updates: Don't check)")
            return
        if not force and time.time() - self.checked < 60:
            return
        self.checked = time.time()
        self.check_button.setEnabled(False)
        self.status.setText("Checking GitHub…")

        def run():
            try:
                state = api("/v1/updates", timeout=10)
                if force or not state.get("checked"):      # the agent's own first check is 2 min after it starts
                    state = api("/v1/updates/check", {}, timeout=40)
            except Exception as e:                  # the agent stopped, or older than updates
                state = {"error": f"the voice agent didn't answer ({e})"}
            return state, checkout()

        def done(res, err):
            self.check_button.setEnabled(True)
            self.state, self.copy = res if res else ({"error": str(err)}, {"error": str(err)})
            self.show_state()
            if self.mode.currentData() == "auto" and self.copy.get("tag") and not self.copy.get("why") \
                    and not self.updated_to:
                self.update_computer(ask=False)
            elif quiet and self.news():
                self.panel.say(f"Updates: {self.news()} (Settings › Updates)", 30000)
                self.panel.notify("TV Agent updates", self.news())

        self.panel.bg.run(run, done)

    def news(self) -> str:
        parts = []
        if self.copy.get("tag"):
            parts.append(f"{self.copy['tag']} for this computer")
        tv = (self.state.get("components") or {}).get("tv") or {}
        if tv.get("newer"):
            parts.append(f"TV app {tv['latest']}")
        return ", ".join(parts)

    def show_state(self):
        comps, err = self.state.get("components") or {}, self.state.get("error")
        text, b = self.rows["computer"]
        server, panel = comps.get("server") or {}, comps.get("panel") or {}
        here = f"voice agent {server.get('current') or '?'}, panel {__version__}"
        if self.updated_to:
            text.setText(f"<span style='color:{GREEN}'>✔</span> {here} · moved to {self.updated_to}: restart the panel "
                         "to use it")
            b.setText("Restart the panel")
            b.setVisible(True)
        elif self.copy.get("error"):
            text.setText(f"{here} · <span style='color:gray'>{html.escape(self.copy['error'])}</span>")
            b.setVisible(False)
        elif self.copy.get("tag"):
            why = self.copy.get("why")
            text.setText(f"<span style='color:{AMBER}'>⬆</span> {here} · <b>{self.copy['tag']}</b> is out"
                         + (f" <span style='color:gray'>(can't update from here: {html.escape(why)})</span>" if why else ""))
            b.setText("Update this computer")
            b.setVisible(not why)
        else:
            latest = ", ".join(f"{n} {c['latest']}" for n, c in (("voice agent", server), ("panel", panel))
                               if c.get("latest"))
            text.setText(f"<span style='color:{GREEN}'>✔</span> {here} · has every release"
                         + (f" <span style='color:gray'>(latest: {latest})</span>" if latest else ""))
            b.setVisible(False)
        text, b = self.rows["tv"]
        tv = comps.get("tv") or {}
        if self.state.get("installing"):
            text.setText("Installing the TV app's update…")
        elif tv.get("newer"):
            text.setText(f"<span style='color:{AMBER}'>⬆</span> {tv.get('current') or 'unknown'} on the TV · "
                         f"<b>{tv['latest']}</b> is out")
        elif tv.get("latest"):
            text.setText(f"<span style='color:{GREEN}'>✔</span> {tv.get('current')} on the TV, the latest")
        else:
            text.setText("<span style='color:gray'>not checked yet</span>")
        b.setVisible(bool(tv.get("newer")) and not self.state.get("installing"))
        text, _ = self.rows["phone"]
        phone = comps.get("phone") or {}
        text.setText((f"latest {phone['latest']} · " if phone.get("latest") else "")
                     + "<span style='color:gray'>each phone updates itself (More › About in the app); their versions "
                       "are under Phones below</span>")
        checked = self.state.get("checked")
        self.status.setText(f"<span style='color:{AMBER}'>{html.escape(err)}</span>" if err else
                            f"Checked {ago(checked)}" if checked else "")

    def update_tv(self):
        tv = (self.state.get("components") or {}).get("tv") or {}
        if QMessageBox.question(self, "TV Agent", f"Install the TV app {tv.get('latest')} from GitHub on the TV? It "
                                "takes a few seconds and pairs it again.") != QMessageBox.StandardButton.Yes:
            return
        self.rows["tv"][1].setEnabled(False)
        self.status.setText("Downloading and installing the TV app…")

        def done(res, err):
            self.rows["tv"][1].setEnabled(True)
            self.status.setText(f"<span style='color:{AMBER}'>Not updated: {html.escape(str(err))}</span>" if err else
                                f"<span style='color:{GREEN}'>✓ {html.escape(res.get('say', 'Done'))}</span>")
            self.check()
        self.panel.bg.run(lambda: api("/v1/updates/tv", {}, timeout=180), done)

    def update_computer(self, ask: bool = True):
        if self.updated_to:                         # the button now restarts the panel
            if self.panel.settings.dirty or self.panel.decisions.dirty:
                QMessageBox.information(self, "TV Agent", "Save or revert your unsaved changes first.")
                return
            os.execv(sys.executable, [sys.executable, *sys.argv])
        tag = self.copy.get("tag")
        if not tag or self.copy.get("why"):
            return
        if ask and QMessageBox.question(self, "TV Agent", f"Move this computer's copy to {tag} (git, a fast-forward) "
                                        "and restart the voice agent?") != QMessageBox.StandardButton.Yes:
            return
        self.status.setText(f"Updating this computer to {tag}…")

        def done(res, err):
            if err:
                self.status.setText(f"<span style='color:{AMBER}'>Not updated: {html.escape(str(err))}</span>")
                return
            self.updated_to = res
            agent = self.panel.overview.cards["tv-agent"]
            if agent.active:
                agent.act("restart")
            self.show_state()
            self.status.setText(f"<span style='color:{GREEN}'>✓ Updated to {res}</span> · the voice agent restarted; "
                                "restart the panel to use its new version")
            self.panel.say(f"Updated to {res}: restart the panel to use it", 30000)
            self.panel.notify("TV Agent updated", f"This computer is on {res}. Restart the panel to use it.")
            QTimer.singleShot(4000, self.check)

        self.panel.bg.run(update_checkout, done, tag)
