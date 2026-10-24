"""Updates card (Settings tab): the newest releases on GitHub against this computer's code, the TV app, TV Agent
Player and each paired phone. The voice agent asks GitHub (its updates.py, every few hours) and installs the TV
app's and the player's releases over ADB (the player also when it isn't on the TV yet); this computer's copy is
moved up to a newer release tag here, only as a fast-forward of a clean main (never over work that isn't on GitHub);
each phone updates itself (the phone app's More › About)."""
import html
import os
import re
import shutil
import subprocess
import sys
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QWidget

from . import __version__
from .common import AMBER, GREEN, REPO, api, card, dim
from .phones import ago, newer

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


def versions_here() -> dict:
    """The voice agent's and the panel's versions in this copy as it is on disk now: each runs the code it started
    with, so after an update (or a pull) they say whether a restart is due."""
    out = {}
    for key, path, rx in (("server", REPO / "server/pyproject.toml", r'^version = "([^"]+)"'),
                          ("panel", REPO / "panel/tvpanel/__init__.py", r'^__version__ = "([^"]+)"')):
        try:
            m = re.search(rx, path.read_text(), re.M)
        except OSError:
            m = None
        out[key] = m.group(1) if m else None
    return out


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
        self.here: dict = {}            # versions_here()
        self.updated_to = None          # a tag this panel moved to (it runs the old code until restarted)
        self.checked = 0.0              # when this card last asked (showing the tab again within a minute doesn't)
        self.phones: list = []          # the paired phones (/v1/clients), for their app versions
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
        self.mode.setToolTip("By themselves: the voice agent installs the TV app's and TV Agent Player's new "
                             "releases when they're idle, and this computer moves to a new release tag when nothing "
                             "here is unsaved or unpushed. Phones have their own switch (More › About in the app).")
        f.addRow("Updates", self.mode)
        self.rows = {}
        # Each part's button is always there: what it would do now ("Update to 0.8.0", "Restart to use 0.8.0"), or
        # greyed out when there's nothing to do
        for key, label, fn in (("server", "Voice agent", lambda: self.act("server")),
                               ("panel", "Panel", lambda: self.act("panel")),
                               ("tv", "TV app", lambda: self.update_app("tv")),
                               ("tvplayer", "TV Agent Player", lambda: self.update_app("tvplayer")),
                               ("phone", "Phone app", None)):
            line = QHBoxLayout()
            text = QLabel("…")
            text.setWordWrap(True)
            line.addWidget(text, 1)
            b = None
            if fn:
                b = QPushButton("Up to date")
                b.setEnabled(False)
                b.setMinimumWidth(170)
                b.clicked.connect(fn)
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
            try:
                state["phones"] = api("/v1/clients", timeout=10).get("devices") or []
            except Exception:
                state["phones"] = []
            return state, checkout(), versions_here()

        def done(res, err):
            self.check_button.setEnabled(True)
            self.state, self.copy, self.here = res if res else ({"error": str(err)}, {"error": str(err)}, {})
            self.phones = self.state.pop("phones", []) or []
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
        for key, name in (("tv", "TV app"), ("tvplayer", "TV Agent Player")):
            c = (self.state.get("components") or {}).get(key) or {}
            if c.get("newer") and c.get("current"):
                parts.append(f"{name} {c['latest']}")
        latest_phone = ((self.state.get("components") or {}).get("phone") or {}).get("latest")
        behind = [d["name"] for d in self.phones if d.get("app_version") and newer(latest_phone, d["app_version"])]
        if behind:
            parts.append(f"phone app {latest_phone} ({', '.join(behind)})")
        return ", ".join(parts)

    def show_state(self):
        comps, err = self.state.get("components") or {}, self.state.get("error")
        why = self.copy.get("why")
        for key in ("server", "panel"):             # this computer's two parts, from the one copy of the repo
            text, b = self.rows[key]
            # this part's newest release not in this copy yet (the update moves the copy to the newest of any)
            mine = [t for t in self.copy.get("tags") or [] if t.startswith(f"{key}-v")]
            tag = max(mine, key=lambda t: [int(n) for n in re.findall(r"\d+", t)]) if mine else None
            c = comps.get(key) or {}
            running = (c.get("current") if key == "server" else __version__) or "?"
            on_disk, latest = self.here.get(key), c.get("latest")
            name = "voice agent" if key == "server" else "panel"
            if key == "server" and err:
                line = f"<span style='color:gray'>not running ({html.escape(err)})</span>"
            elif tag:
                line = (f"<span style='color:{AMBER}'>⬆</span> {running} running · <b>{tag}</b> is out"
                        + (f" <span style='color:gray'>(can't update from here: {html.escape(why)})</span>"
                           if why else ""))
            elif on_disk and running != on_disk:
                line = (f"<span style='color:{AMBER}'>↻</span> {running} running · this computer has {on_disk}: "
                        f"restart the {name} to use it")
            else:
                line = (f"<span style='color:{GREEN}'>✔</span> {running}"
                        + (", the latest" if latest == running else f" <span style='color:gray'>(latest release: "
                           f"{latest})</span>" if latest else ""))
            if key == "server" and self.copy.get("error"):
                line += f" <span style='color:gray'>· {html.escape(self.copy['error'])}</span>"
            text.setText(line)
            if tag and not why:
                b.setText(f"Update to {tag.split('-v', 1)[1]}")
            elif on_disk and running not in (on_disk, "?") and not (key == "server" and err):
                b.setText(f"Restart to use {on_disk}")
            else:
                b.setText("Not running" if key == "server" and err else "Can't update here" if tag else "Up to date")
            b.setEnabled(b.text().startswith(("Update to", "Restart")))
        for key in ("tv", "tvplayer"):             # the apps the agent installs on the TV
            text, b = self.rows[key]
            c = comps.get(key) or {}
            if self.state.get("installing"):
                text.setText("Installing an update on the TV…")
            elif key == "tvplayer" and c.get("installed") is False:
                text.setText(f"<span style='color:{AMBER}'>○</span> Not on the TV"
                             + (f" · <b>{c['latest']}</b> can be installed" if c.get("latest") else
                                " <span style='color:gray'>· no release on GitHub yet</span>"))
                b.setText("Install TV Agent Player")
            elif c.get("newer"):
                text.setText(f"<span style='color:{AMBER}'>⬆</span> {c.get('current') or 'not installed'} on the TV · "
                             f"<b>{c['latest']}</b> is out")
            elif c.get("latest"):
                text.setText(f"<span style='color:{GREEN}'>✔</span> {c.get('current')} on the TV, the latest")
            elif c.get("current"):
                text.setText(f"{c['current']} on the TV <span style='color:gray'>· no release on GitHub yet</span>")
            else:
                text.setText("<span style='color:gray'>not checked yet</span>")
            if c.get("ahead"):
                text.setText(f"{c.get('current')} on the TV <span style='color:gray'>· a test build, newer than the "
                             f"latest release ({c.get('latest')})</span>")
            if key == "tvplayer" and c.get("installed") is False and c.get("latest"):
                b.setText(f"Install {c['latest']}")
            elif c.get("newer") and c.get("installed") is not False:
                b.setText(f"Update to {c['latest']}")
            else:
                b.setText("Installing…" if self.state.get("installing") else "Up to date")
            b.setEnabled(b.text().startswith(("Update to", "Install ")) and not self.state.get("installing"))
        text, _ = self.rows["phone"]
        latest = (comps.get("phone") or {}).get("latest")
        known = [d for d in self.phones if d.get("app_version")]
        behind = [d for d in known if newer(latest, d["app_version"])]
        if not latest:
            text.setText("<span style='color:gray'>not checked yet</span>")
        elif not self.phones:
            text.setText(f"latest {latest} · <span style='color:gray'>no phones paired</span>")
        elif behind:
            text.setText(f"<span style='color:{AMBER}'>⬆</span> <b>{latest}</b> is out · "
                         + ", ".join(f"{html.escape(d['name'])} has {html.escape(d['app_version'])}" for d in behind)
                         + " <span style='color:gray'>(each phone updates itself: More › About in the app)</span>")
        elif known:
            text.setText(f"<span style='color:{GREEN}'>✔</span> {latest} on "
                         + ", ".join(html.escape(d["name"]) for d in known) + ", the latest")
        else:
            text.setText(f"latest {latest} · <span style='color:gray'>the phones haven't said their app's version "
                         "yet</span>")
        checked = self.state.get("checked")
        self.status.setText(f"<span style='color:{AMBER}'>{html.escape(err)}</span>" if err else
                            f"Checked {ago(checked)}" if checked else "")

    def update_app(self, key: str):
        """The TV app's or TV Agent Player's latest release installed on the TV through the agent."""
        name = {"tv": "the TV app", "tvplayer": "TV Agent Player"}[key]
        c = (self.state.get("components") or {}).get(key) or {}
        if QMessageBox.question(self, "TV Agent", f"Install {name} {c.get('latest')} from GitHub on the TV? It takes a "
                                "few seconds and pairs it again." + ("\n\nIt isn't replaced while it plays."
                                                                     if key == "tvplayer" else "")
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.rows[key][1].setEnabled(False)
        self.status.setText(f"Downloading and installing {name}…")

        def done(res, err):
            self.rows[key][1].setEnabled(True)
            self.status.setText(f"<span style='color:{AMBER}'>Not updated: {html.escape(str(err))}</span>" if err else
                                f"<span style='color:{GREEN}'>✓ {html.escape(res.get('say', 'Done'))}</span>")
            self.check(force=True)
        self.panel.bg.run(lambda: api(f"/v1/updates/{key}", {}, timeout=180), done)

    def act(self, key: str):
        """The voice agent's or the panel's button: update this computer to the newer release, or restart that part
        to use the code it has."""
        if self.copy.get("tag") and not self.copy.get("why"):
            self.update_computer()
        elif key == "panel":
            self.restart_panel()
        elif QMessageBox.question(self, "TV Agent", f"Restart the voice agent to use {self.here.get('server')}? "
                                  "Whatever plays goes on; a command being decided right now is lost."
                                  ) == QMessageBox.StandardButton.Yes:
            self.panel.overview.cards["tv-agent"].act("restart")
            self.status.setText("Restarting the voice agent…")
            QTimer.singleShot(6000, lambda: self.check(force=True))

    def restart_panel(self):
        if self.panel.settings.dirty or self.panel.decisions.dirty:
            QMessageBox.information(self, "TV Agent", "Save or revert your unsaved changes first.")
            return
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def update_computer(self, ask: bool = True):
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
            self.here = versions_here()
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
