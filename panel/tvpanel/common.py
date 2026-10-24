"""Shared bits of the TV Agent panel: talking to systemd and the agent, small widgets, colours, preferences."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PANEL = Path(__file__).resolve().parents[1]          # panel/ (or /usr/lib/tv-agent-panel when packaged)
REPO = PANEL.parent
try:                                                   # the installed tv-agent package, else this repo's server/
    import tv_agent  # noqa: F401
except ImportError:
    sys.path.insert(0, str(REPO / "server" / "src"))
from tv_agent import activity as agent_activity  # noqa: E402,F401  (stdlib only)
from tv_agent import config as agent_config  # noqa: E402  (stdlib only; the daemon's own checks)
from tv_agent import stt as agent_stt  # noqa: E402,F401  (stdlib only until a model is loaded)
from tv_agent import voicetests as agent_voicetests  # noqa: E402,F401  (stdlib only)
from tv_agent import quality as agent_quality  # noqa: E402,F401  (stdlib only)

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSettings, QSize, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtWidgets import (QAbstractScrollArea, QAbstractSpinBox, QComboBox, QFrame, QHBoxLayout, QLabel,  # noqa: E402
                               QMessageBox, QSizePolicy, QVBoxLayout, QWidget)

APP_ID = "tv-agent-panel"
PLAYER_NAMES = {"tvplayer": "TV Agent Player", "kodi": "Kodi", "just": "Just Player"}   # config's `player`
ICON = PANEL / "tv-agent.svg"
TV_AGENT = Path(shutil.which("tv-agent") or REPO / "server" / ".venv" / "bin" / "tv-agent")
GREEN, AMBER, RED, GREY, BLUE, PURPLE = "#2fb170", "#e0a030", "#e05555", "#8a8f98", "#4c8dff", "#9b6cf5"
SERVICES = [
    ("tv-agent", "Voice agent", "Speech to text, decisions and TV control: the remote's mic button, the phones and "
                                "this panel talk to it."),
    ("llama-d1", "Decision model", "Liquid AI d1-3B on this laptop's GPU: the local decision model (Decisions tab). "
                                   "Not needed when that step is off or only a hosted model is asked."),
]
STYLE = """
QFrame#card { border: 1px solid palette(midlight); border-radius: 12px; background: palette(base); }
QLabel#headline { font-size: 17pt; font-weight: 600; }
QLabel#cardTitle { font-size: 11.5pt; }
QLabel#value { font-weight: 600; }
QLabel#dim { color: #8a8f98; }
QTableWidget { background: transparent; }
"""


# ---------------------------------------------------------------- panel preferences (not the agent's)

class Prefs:
    """The panel's own behaviour, in ~/.config/tv-agent/panel.conf."""
    DEFAULTS = {"close_to_tray": True, "start_in_tray": False, "tray_details": True, "notify": True,
                "autostart": False}

    def __init__(self):
        self.s = QSettings(str(agent_config.CONFIG_DIR / "panel.conf"), QSettings.Format.IniFormat)

    def get(self, key: str) -> bool:
        v = self.s.value(key, self.DEFAULTS[key])
        return v if isinstance(v, bool) else str(v).lower() == "true"

    def set(self, key: str, value: bool):
        self.s.setValue(key, value)
        self.s.sync()


AUTOSTART = Path.home() / ".config/autostart/tv-agent-panel.desktop"


def desktop_entry(extra_args: str = "") -> str:
    """The .desktop entry for this copy of the panel (panel/tv-agent-panel.desktop.in, filled in)."""
    launcher = shutil.which("tv-agent-panel")         # a packaged install puts this on PATH
    exe = launcher or f"/usr/bin/python3 {PANEL / 'tv_agent_panel.py'}"
    text = (PANEL / "tv-agent-panel.desktop.in").read_text()
    return text.replace("@EXEC@", f"{exe} {extra_args}".strip()).replace("@ICON@", str(ICON))


def set_autostart(on: bool):
    """Start the panel (in the tray) when you log in."""
    if not on:
        AUTOSTART.unlink(missing_ok=True)
        return
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(desktop_entry("--tray"))


# ---------------------------------------------------------------- data (called from worker threads)

def systemctl(*args: str, timeout: float = 30) -> tuple[bool, str]:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def unit_info(name: str) -> dict:
    _, out = systemctl("show", f"{name}.service", "-p",
                       "ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestamp,ControlGroup", timeout=5)
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    pids = []
    if info.get("ControlGroup"):
        try:
            pids = [int(p) for p in Path(f"/sys/fs/cgroup{info['ControlGroup']}/cgroup.procs").read_text().split()]
        except OSError:
            pass
    info["pids"] = pids
    return info


def gpu_of(pid: int) -> tuple[int, int]:
    """(bytes of GPU memory resident, GPU busy ns so far) from the amdgpu fdinfo of one process."""
    seen, mem, busy = set(), 0, 0
    try:
        fds = os.listdir(f"/proc/{pid}/fdinfo")
    except OSError:
        return 0, 0
    for fd in fds:
        try:
            text = Path(f"/proc/{pid}/fdinfo/{fd}").read_text()
        except OSError:
            continue
        m = re.search(r"drm-client-id:\s*(\d+)", text)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        mem += sum(int(v) * 1024 for v in re.findall(r"drm-memory-(?:vram|gtt):\s*(\d+) KiB", text))
        busy += sum(int(v) for v in re.findall(r"drm-engine-\w+:\s*(\d+) ns", text))
    return mem, busy


def gpu_totals() -> tuple[int, int]:
    for card_dir in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        dev = card_dir / "device"
        try:
            used = int((dev / "mem_info_vram_used").read_text()) + int((dev / "mem_info_gtt_used").read_text())
            total = int((dev / "mem_info_vram_total").read_text()) + int((dev / "mem_info_gtt_total").read_text())
            return used, total
        except OSError:
            continue
    return 0, 0


def load_cfg() -> agent_config.Config:
    return agent_config.load()


def local_d1_needed() -> bool:
    """Whether the decision steps use this laptop's d1 (llama-d1): its step is on and it isn't hosted only."""
    try:
        cfg = load_cfg()
    except SystemExit:
        return True
    return "d1" not in cfg.decide_off and cfg.d1_where != "hosted"


class ApiError(RuntimeError):
    pass


def api(path: str, body=None, timeout: float = 6.0, raw: bytes | None = None, ok_codes=(200,)):
    """The agent's local API (127.0.0.1 + token). JSON in and out; raw= sends bytes (audio)."""
    cfg = load_cfg()
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(
        f"http://127.0.0.1:{cfg.local_port}{path}", data=data, method="POST" if data is not None else "GET",
        headers={"Authorization": f"Bearer {cfg.secret('agent_token') or ''}",
                 "Content-Type": "application/octet-stream" if raw is not None else "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            detail = body.get("error") or body.get("detail") or e.reason
        except ValueError:
            detail = e.reason
        raise ApiError(str(detail)) from None


def openrouter_check(model: str, kind: str = "llm") -> tuple[bool, str]:
    """Whether OpenRouter has this model in a form the agent can use (public catalog, no key): a chat model with a
    provider that enforces JSON answers (kind "llm"), one of its decision models ("decision") or one of its
    transcription models ("transcription")."""
    base = "https://openrouter.ai/api/v1"
    try:
        if kind in ("decision", "transcription"):
            what = "decisions" if kind == "decision" else "transcription"
            with urllib.request.urlopen(f"{base}/models?output_modalities={what}", timeout=8) as r:
                found = {m["id"]: m for m in json.loads(r.read()).get("data", [])}
            if model not in found:
                return False, f"not one of OpenRouter's {'decision' if kind == 'decision' else 'transcription'} models"
            price = float((found[model].get("pricing") or {}).get("prompt") or 0)
            if kind == "decision":
                return True, f"an OpenRouter decision model, ${price * 1e6:.3f} per million tokens in"
            per_hour = price * 3600 if price < 0.01 else price         # per second of audio, or (MAI) per hour
            return True, f"an OpenRouter transcription model, about ${per_hour:.2f} per hour of audio"
        with urllib.request.urlopen(f"{base}/models/{model}/endpoints", timeout=8) as r:
            endpoints = (json.loads(r.read()).get("data") or {}).get("endpoints") or []
    except urllib.error.HTTPError as e:
        return False, "not on OpenRouter" if e.code == 404 else f"OpenRouter said {e.code}"
    except (OSError, ValueError) as e:
        return False, f"couldn't ask OpenRouter ({type(e).__name__})"
    good = [e for e in endpoints if "structured_outputs" in (e.get("supported_parameters") or [])]
    if not good:
        return False, (f"on OpenRouter, but none of its {len(endpoints)} providers enforces JSON answers: it won't "
                       "work here" if endpoints else "on OpenRouter, but no provider serves it now")
    price = min(float((e.get("pricing") or {}).get("prompt") or 0) for e in good) * 1e6
    return True, f"on OpenRouter: {len(good)} provider{'s' * (len(good) > 1)} with JSON answers, from ${price:.2f} per million tokens in"


# Where a model runs: the same four choices for speech to text and the decision model
WHERE = [("local", "On this laptop"), ("hosted", "Hosted"), ("local-first", "This laptop, hosted if it fails"),
         ("hosted-first", "Hosted, this laptop if it fails")]


class Say:
    """A label's setText() that shows the message in the window's status bar (the tabs' messages, in one place)."""

    def __init__(self, panel):
        self.panel = panel

    def setText(self, text: str):
        self.panel.say(text)


class KeyLine(QLabel):
    """Whether an API key is set, with a link to where Settings changes it."""

    def __init__(self, panel):
        super().__init__("")
        self.panel = panel
        self.setTextFormat(Qt.TextFormat.RichText)
        self.linkActivated.connect(lambda _link: panel.show_settings_keys())

    def show_key(self, ok: bool, name: str, why: str = ""):
        if ok:
            self.setText(f"<span style='color:{GREEN}; font-size:13pt'>✔</span>&nbsp; <b>{name} key set</b>"
                         f"{why} · <a href='keys'>change it in Settings</a>")
        else:
            self.setText(f"<span style='color:{AMBER}; font-size:13pt'>⚠</span>&nbsp; <b>No {name} key</b>{why} · "
                         f"<a href='keys'>set it in Settings</a>")


def offer_to_delete_recordings(parent):
    """Recordings kept off: the ones already kept can go too."""
    n = len(agent_stt.recordings())
    if n and QMessageBox.question(parent, "TV Agent", f"Delete the {n} recordings already kept?") \
            == QMessageBox.StandardButton.Yes:
        agent_stt.delete_recordings()


def android_version() -> str | None:
    """The TV app's version in this copy of the code (android/app/build.gradle)."""
    try:
        return re.search(r"versionName\s+'([^']+)'", (REPO / "android/app/build.gradle").read_text()).group(1)
    except (OSError, AttributeError):
        return None


def run_eval() -> str:
    """The saved rules on the labelled sentences, run here (`tv-agent eval`): for when the voice agent is stopped."""
    r = subprocess.run([str(TV_AGENT), "eval"], capture_output=True, text=True, timeout=300)
    return "\n".join(l for l in (r.stdout + r.stderr).splitlines() if " INFO " not in l)


def run_doctor() -> str:
    r = subprocess.run([str(TV_AGENT), "doctor"], capture_output=True, text=True, timeout=60)
    return "\n".join(l for l in (r.stdout + r.stderr).splitlines() if " INFO " not in l)


HEARD = re.compile(r"tv_agent: heard (['\"])(.*)\1 -> (\w+) via (\w+) \(([\d.]+)\)")
RESULT = re.compile(r"tv_agent: (\w+) -> (.*?)(?: \((\d+) ms\))?$")


def journal_history(n: int = 2000) -> list[dict]:
    """Commands from before the activity log existed, out of the service journal (one-time import)."""
    r = subprocess.run(["journalctl", "--user", "-u", "tv-agent", "-n", str(n), "-o", "json", "--no-pager"],
                       capture_output=True, text=True, timeout=15)
    rows = []
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if not isinstance(msg, str):
            continue
        if m := HEARD.search(msg):
            rows.append({"t": int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6, "source": "unknown", "heard": m.group(2),
                         "type": m.group(3), "via": m.group(4), "rule": "", "confidence": float(m.group(5)),
                         "ok": None, "say": "", "ms": None})
        elif (m := RESULT.search(msg)) and rows and rows[-1]["type"] == m.group(1) and not rows[-1]["say"]:
            rows[-1]["say"], rows[-1]["ms"] = m.group(2), int(m.group(3)) if m.group(3) else None
            rows[-1]["ok"] = not m.group(2).startswith(("Didn't", "Couldn't", "No ", "That didn't"))
    return rows


# ---------------------------------------------------------------- threads

class WheelGuard(QObject):
    """The mouse wheel scrolls the page instead of changing a number box or a list it passes over; only one clicked
    into first takes it. Scrolling through Settings had changed the TV app's port (8765 → 8761) and Save wrote it, so
    the TV couldn't reach the agent (found 2026-10-23). Installed on the application."""

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.Polish and isinstance(obj, (QAbstractSpinBox, QComboBox)):
            obj.setFocusPolicy(Qt.FocusPolicy.StrongFocus)          # not focused by the wheel
        elif kind == QEvent.Type.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox)) and not obj.hasFocus():
            area = obj.parent()
            while area is not None and not isinstance(area, QAbstractScrollArea):
                area = area.parent()
            if area is not None:
                QCoreApplication.sendEvent(area.viewport(), event)
            return True
        return False


class Async:
    """Blocking calls on a thread pool; their callbacks run back on the GUI thread as cb(result, error)."""

    def __init__(self, parent):
        self.pool = ThreadPoolExecutor(4)
        self.pending = []
        t = QTimer(parent)
        t.timeout.connect(self._poll)
        t.start(60)

    def run(self, fn, callback=None, *args):
        self.pending.append((self.pool.submit(fn, *args), callback))

    def _poll(self):
        keep, done = [], []
        for item in self.pending:
            (done if item[0].done() else keep).append(item)
        self.pending = keep
        for fut, cb in done:
            # each as its own event, not inside this timer's handler: a callback that opens a dialog (exec) would
            # otherwise hold back every other result until it closes (Qt doesn't re-enter a running timer)
            if cb:
                QTimer.singleShot(0, lambda fut=fut, cb=cb: self._deliver(fut, cb))

    @staticmethod
    def _deliver(fut, cb):
        err = fut.exception()
        try:
            cb(None if err else fut.result(), err)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------- formatting and widgets

def human(n: float) -> str:
    if n >= 2**30:
        return f"{n / 2**30:.1f} GB"
    if n >= 2**20:
        return f"{n / 2**20:.0f} MB"
    return f"{n / 2**10:.0f} KB" if n >= 2**10 else f"{n:.0f} B"


def clock(s: int) -> str:
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def uptime(stamp: str) -> str:
    """'Thu 2026-10-22 10:22:00 CEST' -> 'Up 12 min (since 10:22)'."""
    try:
        t = time.mktime(time.strptime(" ".join(stamp.split()[1:3]), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, IndexError):
        return "Running"
    m = int(time.time() - t) // 60
    up = f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min" if m < 1440 else f"{m // 1440} days"
    return f"Up {up} (since {time.strftime('%H:%M', time.localtime(t))})"


def episode_of(video_id: str) -> str:
    parts = video_id.split(":")
    return f" · S{parts[1]}E{parts[2]}" if len(parts) == 3 else ""


def pill(text: str, color: str) -> str:
    """Inline status word for rich-text lines."""
    return f"<span style='color:{color}; font-weight:600'>●&nbsp;{text}</span>"


def set_badge(label: QLabel, text: str, color: str):
    label.setText(text)
    label.setStyleSheet(f"background:{color}; color:white; border-radius:10px; padding:3px 12px; font-weight:600;")


def card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


def dim(text: str = "", wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("dim")
    lab.setWordWrap(wrap)                   # a one-line status in a form: no wrap, or the row reserves 3 lines
    return lab


class Sparkline(QWidget):
    def __init__(self, color: str, floor: float = 1.0, height: int = 34):
        super().__init__()
        self.values: deque[float] = deque(maxlen=60)
        self.color, self.floor = QColor(color), floor
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def push(self, v: float):
        self.values.append(max(0.0, v))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        base = QColor(self.palette().mid().color())
        base.setAlpha(60)
        p.fillRect(0, h - 1, w, 1, base)
        if len(self.values) < 2:
            return
        top = max(self.floor, max(self.values)) * 1.15
        step = w / (self.values.maxlen - 1)
        x0 = w - step * (len(self.values) - 1)
        pts = [(x0 + i * step, h - 2 - (v / top) * (h - 4)) for i, v in enumerate(self.values)]
        line = QPainterPath()
        line.moveTo(*pts[0])
        for x, y in pts[1:]:
            line.lineTo(x, y)
        fill = QPainterPath(line)
        fill.lineTo(pts[-1][0], h)
        fill.lineTo(pts[0][0], h)
        fill.closeSubpath()
        grad = QLinearGradient(0, 0, 0, h)
        c1, c2 = QColor(self.color), QColor(self.color)
        c1.setAlpha(110)
        c2.setAlpha(10)
        grad.setColorAt(0, c1)
        grad.setColorAt(1, c2)
        p.fillPath(fill, grad)
        p.setPen(QPen(self.color, 1.6))
        p.drawPath(line)


class LevelBar(QWidget):
    """A thin microphone level bar (0..1)."""

    def __init__(self):
        super().__init__()
        self.level = 0.0
        self.setFixedHeight(6)
        self.setMinimumWidth(120)

    def set(self, v: float):
        self.level = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QColor(self.palette().mid().color())
        track.setAlpha(70)
        p.setPen(QPen(track, 0))
        p.setBrush(track)
        p.drawRoundedRect(0, 0, self.width(), self.height(), 3, 3)
        p.setBrush(QColor(BLUE))
        p.drawRoundedRect(0, 0, int(self.width() * self.level), self.height(), 3, 3)


def metric(title: str, color: str, floor: float) -> tuple[QWidget, QLabel, Sparkline]:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    head = QHBoxLayout()
    name = dim(title)
    value = QLabel("–")
    value.setObjectName("value")
    head.addWidget(name)
    head.addStretch()
    head.addWidget(value)
    spark = Sparkline(color, floor)
    lay.addLayout(head)
    lay.addWidget(spark)
    return box, value, spark


ICON_SIZE = QSize(52, 52)
