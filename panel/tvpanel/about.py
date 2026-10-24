"""About TV Agent: each part's version in this copy, running or on the TV, and the latest on GitHub, with
checksums that tell a release from a local build (each folder's git tree against its release tag, the TV app's APK
against the release's); TV Agent Player and the phone app (their own repositories) as the agent sees them; the pinned
downloads (llama.cpp, the models), the models in use, and where things live."""
import html
import json
import platform
import re
import subprocess

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import Qt, qVersion
from PySide6.QtGui import QGuiApplication, QTextDocument
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QScrollArea, QVBoxLayout

from . import __version__
from .common import AMBER, GREEN, PLAYER_NAMES, REPO, WHERE, agent_config, agent_stt, android_version, api

AUTHOR = "boujuan"
GITHUB = "https://github.com/boujuan/tv-agent"
SLUG = "boujuan/tv-agent"
PARTS = (("panel", "Panel"), ("server", "Voice agent"), ("android", "TV app"))
APPS = (("tvplayer", "TV Agent Player", "https://github.com/boujuan/tv-agent-player"),      # their own repositories
        ("phone", "Phone app", "https://github.com/boujuan/tv-agent-phone"))


def git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _version_key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def versions() -> dict[str, str | None]:
    """Each part's version in this copy of the code."""
    try:
        server = re.search(r'^version\s*=\s*"([^"]+)"', (REPO / "server/pyproject.toml").read_text(), re.M).group(1)
    except (OSError, AttributeError):
        server = None
    return {"panel": __version__, "server": server, "android": android_version()}


def source_state(part: str, version: str) -> str:
    """This copy's folder against its release tag: identical, changed since, uncommitted edits; and its git tree id
    (a checksum of every file in it)."""
    here = git("rev-parse", f"HEAD:{part}")
    if here is None:
        return "not a git checkout"
    tag = f"{part}-v{version}"
    released = git("rev-parse", f"{tag}:{part}")
    if released is None:
        text = f"{tag} isn't released yet"
    elif released == here:
        text = f"identical to the release {tag}"
    else:
        text = f"{git('rev-list', '--count', f'{tag}..HEAD', '--', part) or '?'} commits since {tag}"
    if git("status", "--porcelain", "--", part):
        text += ", plus uncommitted changes"
    return f"{text} · tree {here[:12]}"


def github(parts: set[str]) -> dict:
    """The releases on GitHub (through gh, signed in): the newest of each part, and the TV app's APK digests."""
    try:
        r = subprocess.run(["gh", "release", "list", "--repo", SLUG, "--limit", "60", "--json", "tagName,publishedAt"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {"error": "gh (GitHub's command line) isn't installed"}
    if r.returncode:
        return {"error": (r.stderr.strip().splitlines() or ["gh failed"])[-1]}
    latest: dict = {}
    android_tags = []
    for rel in json.loads(r.stdout or "[]"):
        part, _, ver = rel["tagName"].partition("-v")
        if part not in parts or not ver:
            continue
        if part == "android":
            android_tags.append(rel["tagName"])
        if part not in latest or _version_key(ver) > _version_key(latest[part]["version"]):
            latest[part] = {"version": ver, "tag": rel["tagName"], "date": rel["publishedAt"][:10]}
    apks = {}
    for tag in android_tags[:6]:
        r = subprocess.run(["gh", "release", "view", tag, "--repo", SLUG, "--json", "assets"], capture_output=True,
                           text=True, timeout=20)
        for a in json.loads(r.stdout or "{}").get("assets", []) if r.returncode == 0 else []:
            if a["name"].endswith(".apk") and a.get("digest"):
                apks[a["digest"].removeprefix("sha256:")] = tag
    latest["apks"] = apks
    return latest


def pinned() -> list[tuple[str, str]]:
    """The downloads scripts/fetch-models.sh checks, and the speech models', each with its SHA-256."""
    out = []
    try:
        text = (REPO / "server/scripts/fetch-models.sh").read_text()
        for url, sha in re.findall(r"get (https://\S+)\s*\\\s*\n\s*\S+\s+([0-9a-f]{64})", text):
            out.append((url.rsplit("/", 1)[-1], sha))
    except OSError:
        pass
    for m in agent_stt.MODELS.values():
        out.append((f"{m.archive}.tar.bz2", m.sha256))
    return out


def collect() -> dict:
    """Everything the dialog shows (slow parts: the TV over ADB, GitHub): run in the background."""
    v = versions()
    info = {"versions": v, "source": {p: source_state(p, ver) for p, ver in v.items() if ver},
            "describe": git("describe", "--tags", "--always", "--dirty"), "branch": git("branch", "--show-current"),
            "pinned": pinned()}
    for key, path, timeout in (("agent", "/v1/about", 3), ("tv", "/v1/tv/packages", 25), ("updates", "/v1/updates", 5),
                               ("clients", "/v1/clients", 5)):
        try:
            info[key] = api(path, timeout=timeout)
        except Exception as e:                   # the agent or the TV not answering: said in the dialog
            info[key] = {"error": str(e)}
    info["github"] = github(set(v))
    return info


def render(info: dict | None) -> str:
    """The dialog's text (HTML)."""
    head = (f"<h2 style='margin-bottom:2px'>TV Agent</h2>Voice control for an Android TV: the remote's mic button or "
            f"a phone → this laptop (speech to text, decisions) → the TV.<br>By <b>{AUTHOR}</b> · "
            f"<a href='{GITHUB}'>{GITHUB.removeprefix('https://')}</a> · MIT License")
    if info is None:
        return head + "<p style='color:gray'>Checking the TV and GitHub…</p>"
    v, gh, tv, agent = info["versions"], info["github"], info.get("tv") or {}, info.get("agent") or {}
    rows = []
    for part, name in PARTS:
        here = v.get(part) or "?"
        if part == "panel":
            now = "this window"
        elif part == "server":
            now = (f"running {agent['version']} (Python {agent.get('python', '?')})" if "version" in agent else
                   f"<span style='color:{AMBER}'>not answering</span>")
            if agent.get("version") and agent["version"] != here and not agent["version"].startswith("0.0.0"):
                now += f" · <span style='color:{AMBER}'>restart it to run {here}</span>"
        else:
            app = tv.get("tvagent") or {}
            if "error" in tv:
                now = f"<span style='color:{AMBER}'>the TV isn't answering</span>"
            elif not app.get("installed"):
                now = f"<span style='color:{AMBER}'>not installed on the TV</span>"
            else:
                sha = app.get("sha256", "")
                release = gh.get("apks", {}).get(sha) if isinstance(gh, dict) else None
                now = (f"{app['version']} (build {app.get('code')}) on the TV<br><span style='color:gray'>APK SHA-256 "
                       f"<code>{sha[:16]}…</code>: " + (f"the release {release}" if release else "a local build")
                       + "</span>")
        if "error" in gh:
            latest = "<span style='color:gray'>can't check</span>"
        elif part in gh:
            g = gh[part]
            newer = _version_key(g["version"]) > _version_key(here)
            latest = (f"{g['version']} <span style='color:gray'>({g['date']})</span>"
                      + (f" · <span style='color:{AMBER}'>newer</span>" if newer else
                         f" · <span style='color:{GREEN}'>✓</span>" if g["version"] == here else ""))
        else:
            latest = "<span style='color:gray'>none yet</span>"
        rows.append(f"<tr><td><b>{name}</b></td><td>{here}<br><span style='color:gray'>"
                    f"{html.escape(info['source'].get(part, ''))}</span></td><td>{now}</td><td>{latest}</td></tr>")
    comps = (info.get("updates") or {}).get("components") or {}
    phones = [d for d in (info.get("clients") or {}).get("devices") or [] if d.get("app_version")]
    for key, name, url in APPS:
        if key == "tvplayer":
            app = tv.get("tvplayer") or {}
            now = (f"<span style='color:{AMBER}'>the TV isn't answering</span>" if "error" in tv else
                   f"{app['version']} on the TV" if app.get("installed") else "not on the TV")
        else:
            now = (", ".join(f"{html.escape(d['app_version'])} on {html.escape(d['name'])}" for d in phones)
                   or "<span style='color:gray'>no phone has said its version</span>")
        latest = (comps.get(key) or {}).get("latest") or "<span style='color:gray'>not checked</span>"
        rows.append(f"<tr><td><b>{name}</b></td><td><a href='{url}'>{url.removeprefix('https://github.com/')}</a>"
                    f"</td><td>{now}</td><td>{latest}</td></tr>")
    table = ("<table cellspacing=0 cellpadding=5><tr style='color:gray'><td></td><td>This copy</td>"
             "<td>Running / on the TV</td><td>Latest on GitHub</td></tr>" + "".join(rows) + "</table>")
    git_line = (f"git <code>{html.escape(info.get('describe') or '?')}</code> on {info.get('branch') or '?'}"
                if info.get("describe") else "not a git checkout")
    gh_note = f"<br><span style='color:gray'>GitHub: {html.escape(gh['error'])}</span>" if "error" in gh else ""
    models = ""
    if "version" in agent:
        stt = agent_stt.MODELS.get(agent["stt_model"])
        where = dict(WHERE)
        models = ("<h4>In use</h4>"
                  f"Speech to text: {where.get(agent['stt_where'], agent['stt_where'])} · "
                  f"{stt.label if stt else agent['stt_model']} · hosted "
                  f"{agent_stt.HOSTED.get(agent['stt_hosted_model'], agent['stt_hosted_model'])}<br>"
                  f"Decision model: {where.get(agent['d1_where'], agent['d1_where'])} · "
                  f"{html.escape(agent['d1_model'].rsplit('/', 1)[-1])} · hosted {agent['hosted_d1_model']}<br>"
                  f"LLM: {agent['llm_model']} · films and series play in "
                  f"{PLAYER_NAMES.get(agent['player'], agent['player'])}")
        players = [f"{n} {tv[k]['version']}" for k, n in (("tvplayer", "TV Agent Player"), ("kodi", "Kodi"),
                                                             ("just", "Just Player"))
                   if tv.get(k, {}).get("installed")]
        if players:
            models += f"<br>On the TV: {', '.join(players)}"
    pins = "".join(f"<tr><td>{html.escape(name)}</td><td><code>{sha}</code></td></tr>" for name, sha in info["pinned"])
    env = (f"Panel: Python {platform.python_version()}, PySide6 {PYSIDE_VERSION}, Qt {qVersion()} · "
           f"{html.escape(platform.platform(terse=True))}<br>Settings: <code>{agent_config.CONFIG_DIR}</code> · "
           f"data: <code>{agent_config.DATA_DIR}</code><br>Code: <code>{REPO}</code>")
    return (head + "<h4>Versions</h4>" + table + f"<p>{git_line}{gh_note}</p>" + models
            + "<h4>Pinned downloads (SHA-256 checked)</h4><table cellspacing=0 cellpadding=2>" + pins + "</table>"
            + "<h4>Where things are</h4>" + env)


class About(QDialog):
    def __init__(self, panel):
        super().__init__(panel)
        self.setWindowTitle("About TV Agent")
        self.resize(820, 640)
        lay = QVBoxLayout(self)
        self.text = QLabel(render(None))
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setWordWrap(True)
        self.text.setOpenExternalLinks(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.text.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.text)
        lay.addWidget(scroll, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = QPushButton("Copy details")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._plain()))
        bb.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        panel.bg.run(collect, lambda info, err: self.text.setText(render(info) if info else
                                                                  render(None) + f"<p>{html.escape(str(err))}</p>"))

    def _plain(self) -> str:
        doc = QTextDocument()
        doc.setHtml(self.text.text())
        return doc.toPlainText()
