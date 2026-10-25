"""Backups of this computer's tv-agent setup: settings, API keys and tokens, the paired phones and TV apps, taught
commands, the voice tests and the rest of the agent's own state, in dated folders on this computer. Never uploaded.
Stdlib only: the panel's Settings › Backup uses it too.

    tv-agent backup [--force] [--to DIR]            one now, unless nothing changed since the last one
    tv-agent backup list | verify [<backup>]
    tv-agent backup restore <backup> [--only PARTS] [--dry-run] [--yes]
    tv-agent setup backups [--to DIR] [--enable]    where they go (backup.json) and the daily timer

One backup is a folder named by its time (700, its files 600) with:
    config/         everything in ~/.config/tv-agent but LEFT_OUT's
    data/           ~/.local/share/tv-agent but what's downloaded or made again (LEFT_OUT)
    adb/            the ADB key this computer uses for the TV (adb_key): without it the TV asks to allow it again
    gradle/         the TV_AGENT_* lines of ~/.gradle/gradle.properties: the Android signing key's password
    manifest.json   the versions of every part, each file's size and SHA-256, and what's left out and why

A file the newest backup has unchanged is a hard link to that one's: every backup is whole on its own, and what
rarely changes (the voice tests) takes its space once. Kept: the newest of each of the last 7 days that have one and
of each of the last 4 weeks (backup.json's keep_daily, keep_weekly), and the 3 newest made before a restore.

A restore checks the backup against its checksums, backs up what's here first (safety-…), stops the voice agent
(only while nothing plays), writes the backup's files where they came from, checks them, and starts it again.
Files made since the backup are left as they are.
"""
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from . import config

FORMAT = 1
DEFAULT_DIR = Path.home() / "tv-agent-backups"
KEEP_DAILY, KEEP_WEEKLY, KEEP_SAFETY = 7, 4, 3
DAILY_AT = "05:15"          # the timer's (tv-agent setup backups): after the nightly check, so its run is in it
NAME = re.compile(r"(safety-)?(\d{4}-\d\d-\d\d-\d{6})")
SAFETY = "before restore"
GRADLE = "gradle/tv-agent.properties"          # in a backup: the TV_AGENT_* lines of ~/.gradle/gradle.properties
LEFT_OUT = {
    "config/now-playing.json": "what's playing now: it holds the stream's link, and the agent only reads it for 6 hours "
                               "after a restart",
    "data/models": "the speech and decision models: downloaded again (tv-agent setup stt, scripts/fetch-models.sh)",
    "data/shaders": "made again by the preview pictures the first time they're needed",
    "data/apk-justplayer": "Just Player's APK: downloaded again when it's installed",
}
TEMPORARY = re.compile(r".*\.tmp|\..*\.restoring|(tv-agent|kodi)-.*\.apk")   # half-written, downloads in progress
WHAT = {   # what a part is, for the dry run and the panel (the longest match of a path's start)
    "config/config.toml": "settings", "config/secrets.toml": "API keys and tokens",
    "config/clients.json": "paired phones", "config/paired.json": "what the TV apps were told at pairing",
    "config/learned.json": "taught commands", "config/rules.json": "your rules",
    "config/rules.": "saved rules", "config/phone-tls.": "the certificate the phones and TV Agent Player pin",
    "config/panel.conf": "the panel's preferences", "config/backup.json": "where backups go",
    "data/voice-tests": "voice tests", "data/quality": "nightly check baselines and runs",
    "data/signing": "the Android release signing key", "data/activity.jsonl": "the history",
    "data/traces": "the history's traces", "data/recordings": "kept recordings",
    "data/episode-checks.json": "episodes checked by their subtitles", "data/intros": "intro and credits times",
    "data/finding": "plots and subtitles for the episode and scene searches", "data/tv-apps": "the apps on the TV",
    "data/tvplayer-settings.json": "TV Agent Player's settings", "adb/": "this computer's ADB key for the TV",
    GRADLE: "the signing key's password (~/.gradle/gradle.properties)",
}
SECRET = ("config/secrets.toml", "config/clients.json", "config/phone-tls.", "data/signing/", "adb/", GRADLE)
PARTS = {"config": "Settings, keys and pairings (~/.config/tv-agent)",
         "data": "Voice tests, history and the agent's data (~/.local/share/tv-agent)",
         "adb": "This computer's ADB key for the TV (~/.android)",
         "gradle": "The Android signing key's password (~/.gradle/gradle.properties)"}
SYNC_MARKERS = {".stfolder": "Syncthing", ".dropbox": "Dropbox", ".dropbox.cache": "Dropbox", ".sync": "Resilio Sync",
                ".megaignore": "MEGA", ".nextcloudsync.log": "Nextcloud", ".owncloudsync.log": "ownCloud",
                ".tresorit": "Tresorit", ".pcloud": "pCloud", ".insync": "Insync", ".git": "a git repository"}
SYNC_ROOTS = ("Dropbox", "OneDrive", "Nextcloud", "ownCloud", "MEGA", "pCloudDrive", "Google Drive", "GoogleDrive",
              "Insync", "Seafile", "Sync", "Tresorit", "iCloudDrive", "Box", "Yandex.Disk")
NETWORK_FS = ("nfs", "nfs4", "cifs", "smb3", "fuse.rclone", "fuse.sshfs", "davfs", "fuse.davfs2",
              "fuse.gcsfuse", "fuse.s3fs", "fuse.onedriver", "fuse.google-drive-ocamlfuse", "9p")


class BackupError(RuntimeError):
    pass


# ---- where backups go, and how many are kept ----

def settings_file() -> Path:
    return config.CONFIG_DIR / "backup.json"


def settings() -> dict:
    """backup.json: {"dir", "keep_daily", "keep_weekly"}; defaults for what it doesn't say."""
    try:
        data = json.loads(settings_file().read_text())
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    keep = lambda k, d: data[k] if isinstance(data.get(k), int) and data[k] >= 1 else d    # noqa: E731
    folder = data.get("dir") if isinstance(data.get("dir"), str) and data.get("dir") else str(DEFAULT_DIR)
    return {"dir": Path(folder).expanduser(), "keep_daily": keep("keep_daily", KEEP_DAILY),
            "keep_weekly": keep("keep_weekly", KEEP_WEEKLY)}


def save_settings(**changes):
    """Changes some of backup.json's settings (dir as an absolute path); the rest stay."""
    now = settings() | changes
    data = {"dir": str(Path(now["dir"]).expanduser().absolute()), "keep_daily": now["keep_daily"],
            "keep_weekly": now["keep_weekly"]}
    path = settings_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)


# ---- what's backed up ----

def _toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _adb_key(config_dir: Path) -> Path:
    """The agent's ADB key, as config.toml sets it (read leniently: a backup is most wanted when something's off)."""
    value = _toml(config_dir / "config.toml").get("adb_key")
    return Path(value).expanduser() if isinstance(value, str) and value else config.Config().adb_key


def roots(home: Path | None = None) -> dict[str, Path]:
    """Where each part of a backup lives: this computer's, or under another home (a test, a check of a backup)."""
    if home is None:
        return {"config": config.CONFIG_DIR, "data": config.DATA_DIR, "adb": _adb_key(config.CONFIG_DIR).parent,
                "gradle": Path.home() / ".gradle"}
    return {"config": home / ".config/tv-agent", "data": home / ".local/share/tv-agent", "adb": home / ".android",
            "gradle": home / ".gradle"}


def where(rel: str, at: dict[str, Path]) -> Path:
    """The file a path in a backup stands for (gradle/'s is gradle.properties, of which it holds some lines)."""
    if rel == GRADLE:
        return at["gradle"] / "gradle.properties"
    part, _, rest = rel.partition("/")
    return at[part] / rest


def inside(rel: str) -> bool:
    """Whether a path in a backup stays in its part: a manifest can't send a restore anywhere else."""
    part, _, rest = rel.partition("/")
    return part in PARTS and bool(rest) and not {"", ".", ".."} & set(rest.split("/")) and \
        (part != "gradle" or rel == GRADLE)


def what(rel: str) -> str:
    keys = [k for k in WHAT if rel == k or rel.startswith(k if k.endswith((".", "/")) else k + "/")]
    return WHAT[max(keys, key=len)] if keys else ""


def _gradle_lines(text: str) -> str:
    return "".join(l for l in text.splitlines(keepends=True) if l.lstrip().startswith("TV_AGENT_"))


def read(rel: str, path: Path) -> bytes | None:
    """What a backup keeps of a file here, or None when there's nothing."""
    try:
        if rel == GRADLE:
            lines = _gradle_lines(path.read_text())
            return lines.encode() if lines else None
        return path.read_bytes()
    except (OSError, UnicodeDecodeError):
        return None


def _digest(rel: str, path: Path) -> str | None:
    if rel == GRADLE:
        data = read(rel, path)
        return hashlib.sha256(data).hexdigest() if data is not None else None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def collect(home: Path | None = None) -> tuple[dict[str, Path], list[dict]]:
    """What a backup now would hold (its path in the backup → the file here), and what's left out and why."""
    at = roots(home)
    files, left_out = {}, []
    for part in ("config", "data"):
        base = at[part]
        for top, dirs, names in os.walk(base):
            here = Path(top)
            rel_dir = f"{part}/{here.relative_to(base)}".removesuffix("/.")
            for d in list(dirs):
                if (why := LEFT_OUT.get(f"{rel_dir}/{d}")) is not None:
                    dirs.remove(d)
                    left_out.append({"path": f"{rel_dir}/{d}", "why": why, "size": _size(here / d)})
            for n in names:
                rel, path = f"{rel_dir}/{n}", here / n
                if (why := LEFT_OUT.get(rel)) is not None:
                    left_out.append({"path": rel, "why": why, "size": _size(path)})
                elif not TEMPORARY.fullmatch(n) and path.is_file() and not path.is_symlink():
                    files[rel] = path
    key = _adb_key(at["config"]) if home is None else at["adb"] / _adb_key(at["config"]).name
    for path in (key, key.with_name(key.name + ".pub")):
        if path.is_file():
            files[f"adb/{path.name}"] = path
    if read(GRADLE, at["gradle"] / "gradle.properties") is not None:
        files[GRADLE] = at["gradle"] / "gradle.properties"
    return dict(sorted(files.items())), left_out


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for top, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += (Path(top) / n).lstat().st_size
            except OSError:
                pass
    return total


# ---- versions, and what else covers the folder ----

def _get(path: str, timeout: float = 3) -> dict | None:
    """The running agent's local API (its token from secrets.toml), or None when it doesn't answer."""
    port = _toml(config.CONFIG_DIR / "config.toml").get("local_port", config.Config().local_port)
    token = _toml(config.CONFIG_DIR / "secrets.toml").get("agent_token") or ""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def versions() -> dict:
    """Each part's version as this computer knows it: the agent's code (and its git commit), the running agent's, the
    panel's, the TV app's and TV Agent Player's (the agent's last look at the TV), the phones' (as they last said)."""
    repo = Path(__file__).resolve().parents[3]
    commit = dirty = None          # dirty: the checkout has changes not committed
    if (repo / ".git").exists():
        try:
            git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,  # noqa: E731
                                            timeout=5).stdout.strip()
            commit = git("rev-parse", "--short=12", "HEAD") or None
            dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
        except (OSError, subprocess.TimeoutExpired):
            pass
    panel = None
    for init in (repo / "panel/tvpanel/__init__.py", Path("/usr/lib/tv-agent-panel/tvpanel/__init__.py")):
        try:
            panel = re.search(r'__version__ = "([^"]+)"', init.read_text())[1]
            break
        except (OSError, TypeError):
            continue
    up = (_get("/v1/updates") or {}).get("components") or {}
    phones = []
    try:
        for d in json.loads((config.CONFIG_DIR / "clients.json").read_text()).get("devices", []):
            phones.append({"name": d.get("name"), "app": d.get("app_version") or None})
    except (OSError, ValueError, AttributeError):
        pass
    return {"agent": __version__, "commit": commit, "uncommitted": dirty,
            "running": (up.get("server") or {}).get("current"),
            "panel": panel, "tv_app": (up.get("tv") or {}).get("current"),
            "tvplayer": (up.get("tvplayer") or {}).get("current"), "phones": phones,
            "asked_agent": bool(up)}


def _git_ignores(repo: Path, folder: Path) -> bool:
    """Whether the repository's .gitignore keeps the folder out of it (then it's never committed)."""
    try:
        return subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", str(folder)],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def covered(folder: Path) -> list[str]:
    """What else could copy this folder off the computer: a sync client's folder or marker above it, a git
    repository, a network or cloud mount. Empty when nothing's found."""
    folder = folder.expanduser().absolute()
    found = []
    for d in (folder, *folder.parents):
        for marker, who in SYNC_MARKERS.items():
            if (d / marker).exists() and not (marker == ".git" and _git_ignores(d, folder)):
                found.append(f"{who} ({d})")
        if d.parent == Path.home() and d.name in SYNC_ROOTS:
            found.append(f"{d.name}'s synced folder ({d})")
    syncthing = [Path.home() / ".local/state/syncthing/config.xml", Path.home() / ".config/syncthing/config.xml"]
    for cfg in syncthing:
        try:
            for path in re.findall(r'<folder [^>]*path="([^"]+)"', cfg.read_text()):
                p = Path(path).expanduser()
                if folder == p or p in folder.parents:
                    found.append(f"Syncthing ({p})")
        except OSError:
            pass
    try:
        for line in Path("/proc/self/mounts").read_text().splitlines():
            _src, target, fstype, *_ = line.split()
            target = target.replace("\\040", " ")
            if fstype in NETWORK_FS and (folder == Path(target) or Path(target) in folder.parents):
                found.append(f"a {fstype} mount ({target})")
    except (OSError, ValueError):
        pass
    return list(dict.fromkeys(found))


# ---- the backups in a folder ----

def _private(path: Path, top: Path | None = None):
    """The folder 700, and each one made for it below top."""
    made = [p for p in (path, *path.parents) if not p.exists() and (top is None or top in p.parents)]
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for p in [path, *made]:
        os.chmod(p, 0o700)


def _manifest(path: Path) -> dict | None:
    try:
        m = json.loads((path / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) and m.get("tv_agent_backup") == FORMAT else None


def backups(folder: Path | None = None) -> list[dict]:
    """The backups in the folder, newest first: name, path, time, why it was made, its size, what it added on disk."""
    folder = Path(folder or settings()["dir"])
    out = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for path in entries:
        m = NAME.fullmatch(path.name)
        if not m or not (man := _manifest(path)):
            continue
        out.append({"name": path.name, "path": path, "t": man.get("t", 0), "made": man.get("made"),
                    "reason": man.get("reason"), "size": man.get("size", 0), "new": man.get("new", 0),
                    "files": len(man.get("files", [])), "versions": man.get("versions", {}),
                    "covered": man.get("covered", []), "stamp": m[2]})
    return sorted(out, key=lambda b: b["stamp"], reverse=True)


def find(name: str, folder: Path | None = None) -> Path:
    """A backup by its name ("latest" is the newest), or its folder's path."""
    folder = Path(folder or settings()["dir"])
    if name == "latest":
        regular = [b for b in backups(folder) if b["reason"] != SAFETY]
        if not regular:
            raise BackupError(f"no backups in {folder}")
        return regular[0]["path"]
    path = Path(name).expanduser()
    for p in (path, folder / name):
        if _manifest(p):
            return p
    raise BackupError(f"no backup {name!r} in {folder}")


def usage(folder: Path | None = None) -> int:
    """What the backups take on disk: each file once however many backups link it."""
    folder = Path(folder or settings()["dir"])
    seen, total = set(), 0
    for top, _dirs, names in os.walk(folder):
        for n in names:
            try:
                st = (Path(top) / n).lstat()
            except OSError:
                continue
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_size
    return total


def keep(found: list[dict], keep_daily: int, keep_weekly: int, keep_safety: int = KEEP_SAFETY) -> set[str]:
    """The backups to keep (newest first in): the newest of each of the last keep_daily days that have one and of
    each of the last keep_weekly weeks, the newest one whatever its day, and the keep_safety newest safety ones."""
    regular = [b for b in found if b["reason"] != SAFETY]
    kept = {b["name"] for b in [b for b in found if b["reason"] == SAFETY][:keep_safety]}
    kept |= {regular[0]["name"]} if regular else set()
    for fmt, n in (("%Y-%m-%d", keep_daily), ("%G-%V", keep_weekly)):
        buckets = []
        for b in regular:
            bucket = time.strftime(fmt, time.localtime(b["t"]))
            if bucket not in buckets:
                buckets.append(bucket)
                if len(buckets) > n:
                    break
                kept.add(b["name"])
    return kept


def prune(folder: Path, keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY) -> list[str]:
    """Removes the backups keep() doesn't keep, and what a run that stopped halfway left; their names."""
    found = backups(folder)
    kept = keep(found, keep_daily, keep_weekly)
    gone = []
    for b in found:
        if b["name"] not in kept:
            shutil.rmtree(b["path"])
            gone.append(b["name"])
    for path in folder.glob(".incomplete-*"):
        shutil.rmtree(path, ignore_errors=True)
    return gone


class _Lock:
    """One backup or restore at a time (the timer and the panel's Back up now)."""

    def __init__(self, folder: Path):
        self.path = folder / ".lock"

    def __enter__(self):
        self.f = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600), "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.f.close()
            raise BackupError("another backup or restore is running") from None
        return self

    def __exit__(self, *exc):
        self.f.close()


# ---- making one ----

def _copy(rel: str, src: Path, dest: Path) -> tuple[str, int]:
    """Copies a file into a backup (600 from its first byte); its SHA-256 and size as copied."""
    h, size = hashlib.sha256(), 0
    if rel == GRADLE:
        data = read(rel, src)
        if data is None:
            raise FileNotFoundError(src)
        chunks = iter([data])
    else:
        f = open(src, "rb")                     # first: one gone since it was listed leaves nothing behind
        chunks = iter(lambda: f.read(1 << 20), b"")
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                out.write(chunk)
                h.update(chunk)
                size += len(chunk)
    finally:
        if rel != GRADLE:
            f.close()
    return h.hexdigest(), size


def _folder(folder: Path | None) -> Path:
    folder = Path(folder or settings()["dir"]).expanduser().absolute()
    _private(folder)
    return folder


def backup(reason: str = "manual", force: bool = False, folder: Path | None = None, home: Path | None = None,
           now: float | None = None) -> dict:
    """A backup now into the folder (backup.json's), unless nothing changed since the newest one (force: anyway).
    Returns its summary ({"skipped": True, "last": …} when skipped)."""
    folder = _folder(folder)
    with _Lock(folder):
        return _backup(folder, reason, force, home, now)


def _backup(folder: Path, reason: str, force: bool, home: Path | None, now: float | None) -> dict:
    files, left_out = collect(home)
    hashes = {rel: _digest(rel, path) for rel, path in files.items()}
    hashes = {rel: h for rel, h in hashes.items() if h is not None}       # gone since it was listed
    found = backups(folder)
    regular = [b for b in found if b["reason"] != SAFETY]
    if not force and regular and {f["path"]: f["sha256"] for f in _manifest(regular[0]["path"])["files"]} == hashes:
        return {"skipped": True, "last": _public(regular[0]), "folder": str(folder)}
    last = found[0] if found else None          # what's linked to: the newest, a safety one too
    last_files = {f["path"]: f for f in _manifest(last["path"])["files"]} if last else {}
    now = time.time() if now is None else now
    name = ("safety-" if reason == SAFETY else "") + time.strftime("%Y-%m-%d-%H%M%S", time.localtime(now))
    if (folder / name).exists():
        raise BackupError(f"{folder / name} exists already")
    tmp = folder / f".incomplete-{name}"
    shutil.rmtree(tmp, ignore_errors=True)
    _private(tmp)
    entries, new = [], 0
    for rel, h in hashes.items():
        src, dest = files[rel], tmp / rel
        _private(dest.parent, tmp)
        prev = last_files.get(rel)
        if prev and prev["sha256"] == h and _link(last["path"] / rel, dest, prev["size"]):
            size = prev["size"]
        else:
            try:
                h, size = _copy(rel, src, dest)
            except FileNotFoundError:
                continue
            new += size
        try:
            st = src.stat()
            mode, mtime = f"{st.st_mode & 0o777:o}", round(st.st_mtime, 3)
        except OSError:
            mode, mtime = "600", None
        entries.append({"path": rel, "size": size, "sha256": h, "mode": mode, "mtime": mtime})
    manifest = {
        "tv_agent_backup": FORMAT, "name": name, "t": round(now, 3),
        "made": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "reason": reason,
        "host": socket.gethostname(), "versions": versions() if home is None else {"agent": __version__},
        "from": {part: str(path) for part, path in roots(home).items()}, "size": sum(e["size"] for e in entries),
        "new": new, "files": entries, "left_out": left_out, "covered": covered(folder),
        "restore": f"tv-agent backup restore {name} (--dry-run first; --only {','.join(PARTS)} for some)",
    }
    fd = os.open(tmp / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    os.rename(tmp, folder / name)
    s = settings()
    gone = prune(folder, s["keep_daily"], s["keep_weekly"]) if reason != SAFETY else []
    made = next(b for b in backups(folder) if b["name"] == name)
    return {"skipped": False, **_public(made), "removed": gone, "left_out": left_out, "folder": str(folder),
            "usage": usage(folder)}


def _link(old: Path, dest: Path, size: int) -> bool:
    """dest as a hard link to the newest backup's copy, when that's still its size; False: copy it instead (another
    file system, the old copy gone)."""
    try:
        if old.stat().st_size != size:
            return False
        os.link(old, dest)
        return True
    except OSError:
        return False


def _public(b: dict) -> dict:
    return {k: (str(v) if isinstance(v, Path) else v) for k, v in b.items()}


def verify(path: Path) -> list[str]:
    """A backup's files against its manifest: the ones missing or not as they were written (empty: all fine)."""
    man = _manifest(path)
    if not man:
        return ["no manifest.json"]
    bad = []
    for f in man.get("files", []):
        if not inside(f["path"]):
            bad.append(f"{f['path']}: not a path a backup holds")
            continue
        got = _digest("", path / f["path"])      # as stored: a plain file, gradle/'s too
        if got is None:
            bad.append(f"{f['path']}: missing")
        elif got != f["sha256"]:
            bad.append(f"{f['path']}: changed since the backup")
    return bad


# ---- restoring one ----

def _chosen(rel: str, only: list[str] | None) -> bool:
    return not only or any(rel == p.rstrip("/") or rel.startswith(p.rstrip("/") + "/") for p in only)


def plan(name: str, only: list[str] | None = None, home: Path | None = None, folder: Path | None = None) -> dict:
    """What restoring a backup (some parts of it: "config", "data/voice-tests", "config/learned.json"…) would
    change here: each of its files new here or different, how many are as they are, and the files here it doesn't
    have (left alone)."""
    path = find(name, folder)
    man = _manifest(path)
    if outside := [f["path"] for f in man["files"] if not inside(f["path"])]:
        raise BackupError(f"{path.name} names files outside what a backup holds ({', '.join(outside[:3])})")
    at = roots(home)
    changes, same = [], 0
    for f in man["files"]:
        if not _chosen(f["path"], only):
            continue
        now = _digest(f["path"], where(f["path"], at))
        if now == f["sha256"]:
            same += 1
            continue
        changes.append({"path": f["path"], "change": "new" if now is None else "changed", "size": f["size"],
                        "what": what(f["path"])})
    files_now, _ = collect(home)
    in_backup = {f["path"] for f in man["files"]}
    extra = [rel for rel in files_now if rel not in in_backup and _chosen(rel, only)]
    return {"backup": path.name, "path": str(path), "made": man.get("made"), "t": man.get("t"),
            "versions": man.get("versions", {}), "changes": changes, "same": same, "kept": extra,
            "only": only or list(PARTS)}


def _write(rel: str, src: Path, dest: Path, mode: int):
    """One file of a backup back where it came from (gradle/'s lines into gradle.properties, the rest of that file
    kept), written whole then moved into place."""
    data = src.read_bytes()
    if rel == GRADLE:
        try:
            current = dest.read_text()
        except OSError:
            current = ""
        others = "".join(l for l in current.splitlines(keepends=True) if not l.lstrip().startswith("TV_AGENT_"))
        if others and not others.endswith("\n"):
            others += "\n"
        data, mode = others.encode() + data, 0o600
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.restoring")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def restore_files(name: str, only: list[str] | None = None, home: Path | None = None,
                  folder: Path | None = None) -> dict:
    """Writes the backup's files that differ from what's here (plan()), after checking the backup against its
    checksums, then checks each one written. The voice agent's own stop and start are restore()'s."""
    p = plan(name, only, home, folder)
    path = Path(p["path"])
    if bad := verify(path):
        raise BackupError(f"{path.name} isn't as it was made ({len(bad)} files: {', '.join(bad[:3])}): not restored")
    man = {f["path"]: f for f in _manifest(path)["files"]}
    at = roots(home)
    _private(at["config"])                      # the agent refuses to start with it readable by others
    written = []
    for c in p["changes"]:
        f = man[c["path"]]
        mode = int(f.get("mode") or "600", 8) & 0o777       # as it was, but a secret never readable by others
        if c["path"].startswith(SECRET) and not c["path"].endswith(".pub"):
            mode &= 0o600
        dest = where(c["path"], at)
        _write(c["path"], path / c["path"], dest, mode)
        if _digest(c["path"], dest) != f["sha256"]:
            raise BackupError(f"{c['path']} isn't as in the backup after writing it")
        written.append(c["path"])
    return {**p, "restored": written}


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def agent_active() -> bool:
    try:
        return _systemctl("is-active", "--quiet", "tv-agent.service").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def playing() -> str | None:
    """What's open in a player on the TV, as the running agent says (None: nothing). BackupError when it doesn't
    answer: then nobody can tell."""
    state = _get("/v1/state", timeout=8)
    if state is None:
        raise BackupError("the voice agent isn't answering, so whether something plays can't be told: try again in a "
                          "moment, or stop it yourself first (systemctl --user stop tv-agent)")
    player = state.get("player")
    if not player:
        return None
    name = (state.get("now_playing") or {}).get("name") or "something"
    return f"{name} is {'playing' if player.get('playing') else 'paused'} on the TV"


def restore(name: str, only: list[str] | None = None, folder: Path | None = None) -> dict:
    """The whole restore on this computer: a safety backup of what's here, the voice agent stopped (refused while
    something plays or is paused in a player), the files written and checked, the agent started again."""
    folder = _folder(folder)
    with _Lock(folder):
        p = plan(name, only, folder=folder)
        if not p["changes"]:
            return {**p, "restored": [], "safety": None, "agent": "untouched"}
        running = agent_active()
        if running and (now_playing := playing()):
            raise BackupError(f"{now_playing}: restore once it's stopped (the voice agent restarts for it)")
        safety = _backup(folder, SAFETY, True, None, None)
        try:
            if running:
                r = _systemctl("stop", "tv-agent.service")
                if r.returncode != 0:
                    raise BackupError(f"the voice agent didn't stop: {(r.stderr or r.stdout).strip()[:200]}")
            done = restore_files(name, only, folder=folder)
        finally:
            if running:
                _systemctl("start", "tv-agent.service")
    return {**done, "safety": safety["name"], "agent": "restarted" if running else "not running"}
