"""What the panel's Overview shows about the TV: its model and Android version, the TV app's version, the network
(interfaces, Wi-Fi signal and link speed), CPU and memory use, the busiest processes, the project's own apps and the
players (CPU and memory of each that runs), and the app in front. Read-only commands over the agent's ADB
connection; plus a transfer speed test and step-by-step ADB checks."""
import os
import re
import socket
import subprocess
import time

from ..config import Config
from .adb import Adb

APP = "com.boujuan.tvagent"
# The apps whose processes the Overview lists, with their names there (PACKAGES below has their packages)
WATCHED = {"tvplayer": "TV Agent Player", "kodi": "Kodi", "just": "Just Player", "tvagent": "TV app"}
# Each one's CPU over the same half second as the whole TV's: /proc/<pid>/stat before and after
_PIDS = "P=$(for n in {}; do pidof $n; done | tr '\\n' ' '); "
_TICKS = "for p in $P; do cat /proc/$p/stat 2>/dev/null; done; "
SCRIPT = ("getprop ro.product.model; getprop ro.build.version.release; echo @@; "
          f"dumpsys package {APP} | grep -E 'versionName|versionCode|lastUpdateTime' | head -3; echo @@; "
          "ip -o -4 addr show; echo @@; cmd wifi status 2>/dev/null | grep -m1 WifiInfo; echo @@; "
          "{pids}head -1 /proc/stat; " + _TICKS + "sleep 0.5; head -1 /proc/stat; " + _TICKS + "echo @@; "
          "grep -E 'MemTotal|MemAvailable' /proc/meminfo; echo @@; "
          "dumpsys cpuinfo | grep -E '^ +[0-9.]+% [0-9]+/' | head -6; echo @@; uptime; echo @@; "
          "dumpsys activity activities | grep -m1 ResumedActivity; dumpsys power 2>/dev/null | grep -m1 mWakefulness=; "
          "echo @@; for n in {packages}; do for p in $(pidof $n); do "
          "echo APP $p $n $(grep -m1 VmRSS /proc/$p/status | tr -s ' ' | cut -d' ' -f2); done; done")
KEYS = {"home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "wake": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP",
        "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE", "volume_up": "KEYCODE_VOLUME_UP",
        "volume_down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE", "settings": "KEYCODE_SETTINGS"}


def _cpu(lines: list[str]) -> float | None:
    """Busy % between two /proc/stat "cpu" lines."""
    vals = [[int(x) for x in line.split()[1:]] for line in lines if line.startswith("cpu ")]
    if len(vals) < 2:
        return None
    total = sum(vals[1]) - sum(vals[0])
    idle = (vals[1][3] + vals[1][4]) - (vals[0][3] + vals[0][4])
    return round(100 * (1 - idle / total), 1) if total > 0 else None


def apps(stat_lines: list[str], app_lines: list[str], packages: dict[str, str]) -> list[dict]:
    """The watched apps that run: their CPU share of the whole TV over the sample (like the laptop's rows) and memory,
    from two rounds of /proc/<pid>/stat around two /proc/stat "cpu" lines, and VmRSS. A process is known by its pid
    ("APP <pid> <package> <kB>" lines): /proc's name keeps only 15 characters, the same for the TV app and the player."""
    cpu = [[int(x) for x in l.split()[1:]] for l in stat_lines if l.startswith("cpu ")]
    total = sum(cpu[1]) - sum(cpu[0]) if len(cpu) == 2 else 0
    ticks: dict[int, list[int]] = {}
    for line in stat_lines:
        m = re.match(r"(\d+) \(.*\) \S+ (.*)", line)
        if m:
            f = m.group(2).split()                  # after the state: ppid…; utime and stime are fields 14 and 15
            ticks.setdefault(int(m.group(1)), []).append(int(f[10]) + int(f[11]))
    keys = {pkg: key for key, pkg in packages.items()}
    out = []
    for line in app_lines:
        m = re.match(r"APP (\d+) (\S+) ?(\d*)", line)
        if not m or m.group(2) not in keys:
            continue
        pid, key, t = int(m.group(1)), keys[m.group(2)], ticks.get(int(m.group(1)), [])
        busy = (t[-1] - t[0]) if len(t) == 2 else 0
        out.append({"key": key, "name": WATCHED[key], "package": m.group(2), "pid": pid,
                    "cpu": round(100 * busy / total, 1) if total > 0 else 0.0, "rss_kb": int(m.group(3) or 0)})
    return sorted(out, key=lambda a: list(WATCHED).index(a["key"]))


def status(adb: Adb) -> dict:
    watched = {k: PACKAGES[k] for k in WATCHED}
    script = SCRIPT.replace("{pids}", _PIDS.format(" ".join(watched.values()))).replace(
        "{packages}", " ".join(watched.values()))
    parts = [p.strip().splitlines() for p in adb.run(["sh", "-c", script], timeout=15).split("@@")]
    parts += [[]] * (9 - len(parts))
    out: dict = {"model": (parts[0] + [""])[0], "android": (parts[0] + ["", ""])[1], "address": adb.host}
    app = " ".join(parts[1])
    out["app"] = {"installed": "versionName" in app,
                  "version": (re.search(r"versionName=(\S+)", app) or [None, None])[1],
                  "code": int(m.group(1)) if (m := re.search(r"versionCode=(\d+)", app)) else None,
                  "updated": (re.search(r"lastUpdateTime=(.+?)(?:\s{2,}|$)", app) or [None, None])[1]}
    out["interfaces"] = [{"name": m.group(1), "ip": m.group(2)} for line in parts[2]
                         if (m := re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)", line)) and m.group(1) != "lo"]
    wifi = " ".join(parts[3])
    if wifi:
        out["wifi"] = {k: (m.group(1) if (m := re.search(rx, wifi)) else None) for k, rx in
                       (("ssid", r'SSID: "([^"]*)"'), ("rssi", r"RSSI: (-?\d+)"), ("link_mbps", r"Link speed: (\d+)Mbps"),
                        ("mhz", r"Frequency: (\d+)MHz"), ("standard", r"Wi-Fi standard: (\d+)"))}
    on = next((i["name"] for i in out["interfaces"] if i["ip"] == adb.host), None)
    out["link"] = "wifi" if (on or "").startswith("wlan") else "ethernet" if on else None
    out["cpu"] = _cpu(parts[4])
    mem = {k: int(v) for k, v in re.findall(r"(MemTotal|MemAvailable):\s+(\d+)", " ".join(parts[5]))}
    if mem.get("MemTotal"):
        out["memory"] = {"total_mb": mem["MemTotal"] // 1024,
                         "used_mb": (mem["MemTotal"] - mem.get("MemAvailable", 0)) // 1024}
    out["busiest"] = [{"cpu": float(m.group(1)), "name": m.group(2)} for line in parts[6]
                      if (m := re.match(r"\s*([\d.]+)% \d+/(\S+?):", line))]
    up = re.search(r"up\s+(.+?),\s+\d+ users?", " ".join(parts[7]))
    out["uptime"] = up.group(1) if up else None
    front = " ".join(parts[8])
    out["foreground"] = m.group(1) if (m := re.search(r"ActivityRecord\{\S+ \S+ ([\w.]+)/", front)) else None
    out["screen"] = m.group(1).lower() if (m := re.search(r"mWakefulness=(\w+)", front)) else None   # awake, asleep…
    out["apps"] = apps(parts[4], parts[9] if len(parts) > 9 else [], watched)
    return out


PACKAGES = {"tvagent": APP, "tvplayer": "com.boujuan.tvagent.player", "kodi": "org.xbmc.kodi",
            "just": "com.brouken.player"}


def packages(adb: Adb) -> dict:
    """The TV app and the players: installed, version, enabled; the TV app's APK SHA-256 too (to match a GitHub
    release's)."""
    script = "; echo @@; ".join(f"dumpsys package {pkg} | grep -m3 -E 'versionName|versionCode|User 0:'"
                                for pkg in PACKAGES.values())
    script += f"; echo @@; sha256sum $(pm path {APP} | head -1 | cut -d: -f2) 2>/dev/null"
    parts = adb.run(["sh", "-c", script], timeout=20).split("@@")
    parts += [""] * (len(PACKAGES) + 1 - len(parts))
    out = {}
    for (key, pkg), text in zip(PACKAGES.items(), parts):
        name = re.search(r"versionName=(\S+)", text)
        state = re.search(r"User 0:.*?\benabled=(\d)", text)
        out[key] = {"package": pkg, "installed": bool(name), "version": name.group(1) if name else None,
                    "code": int(m.group(1)) if (m := re.search(r"versionCode=(\d+)", text)) else None,
                    "enabled": state is None or state.group(1) in ("0", "1")}     # 2, 3, 4: disabled
    if out["tvagent"]["installed"] and (m := re.match(r"\s*([0-9a-f]{64})\s", parts[len(PACKAGES)])):
        out["tvagent"]["sha256"] = m.group(1)
    return out


def ping(host: str, count: int = 3) -> float | None:
    """The laptop's round trip to the TV, in ms (system ping; None if it doesn't answer)."""
    try:
        r = subprocess.run(["/usr/bin/ping", "-c", str(count), "-q", "-W", "1", host], capture_output=True, text=True,
                           timeout=count + 3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"= [\d.]+/([\d.]+)/", r.stdout)
    return float(m.group(1)) if m else None


def laptop_link(host: str) -> dict:
    """The laptop's interface towards the TV and its link speed (Linux sysfs)."""
    try:
        dev = re.search(r"\bdev (\S+)", subprocess.run(["ip", "route", "get", host], capture_output=True,
                                                         text=True, timeout=3).stdout).group(1)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        return {}
    info = {"interface": dev}
    bond = f"/proc/net/bonding/{dev}"
    if os.path.exists(bond):
        active = re.search(r"Currently Active Slave: (\S+)", open(bond).read())
        if active:
            info["via"] = dev = active.group(1)
    try:
        speed = int(open(f"/sys/class/net/{dev}/speed").read())
        info["link_mbps"] = speed if speed > 0 else None
    except (OSError, ValueError):
        pass
    info["wifi"] = os.path.exists(f"/sys/class/net/{dev}/wireless")
    return info


def speedtest(adb: Adb, megabytes: int = 16) -> float:
    """Mbit/s reading data from the TV over ADB: a lower bound for the link (ADB and Python add their own cost)."""
    t = time.perf_counter()
    got = adb.run_bytes(["head", "-c", str(megabytes << 20), "/dev/zero"], timeout=60)
    return round(len(got) * 8 / (time.perf_counter() - t) / 1e6, 1)


def diagnose(cfg: Config, adb: Adb) -> list[dict]:
    """Each step of reaching the TV over ADB, with what went wrong and what to do."""
    steps = []

    def step(name, ok, detail, hint=""):
        steps.append({"step": name, "ok": ok, "detail": detail, "hint": "" if ok is not False else hint})
        return ok

    if not step("TV addresses set", bool(cfg.tv_hosts), ", ".join(cfg.tv_hosts) or "none",
                "Settings › TV addresses (the TV shows them in Network settings)"):
        return steps
    found = []
    for host in cfg.tv_hosts:
        rtt = ping(host, 1)
        try:
            socket.create_connection((host, cfg.adb_port), timeout=1.5).close()
            port = True
        except OSError:
            port = False
        found.append((host, port, (f"answers ping in {rtt:.0f} ms" if rtt is not None else "no ping answer")
                      + (f", ADB port {cfg.adb_port} open" if port else f", ADB port {cfg.adb_port} closed")))
    any_open = any(port for _, port, _ in found)
    for host, port, detail in found:
        # an address that doesn't answer is fine while another does (the TV on Wi-Fi, its Ethernet unplugged)
        step(host, True if port else (None if any_open else False), detail,
             "Is the TV on and on this network? On the TV: Developer options › USB debugging (and Wireless "
             "debugging if it has it); the port is 5555")
    if not any_open:
        return steps
    if not step("ADB key", cfg.adb_key.exists() and cfg.adb_key.with_suffix(".pub").exists(), str(cfg.adb_key),
                "Make one with `adb keygen ~/.android/adbkey` (or connect once with adb)"):
        return steps
    try:
        model = adb.run(["getprop", "ro.product.model"], timeout=6).strip()
        step("ADB connection", True, f"{model} at {adb.host}")
    except Exception as e:
        step("ADB connection", False, f"{type(e).__name__}: {e}",
             "Accept the 'Allow USB debugging' prompt on the TV (tick 'Always allow'); if it never shows, "
             "revoke USB debugging authorisations on the TV and try again")
        return steps
    app = adb.run(["sh", "-c", f"dumpsys package {APP} | grep -m1 versionName"], timeout=8)
    step("TV app", "versionName" in app, app.strip().replace("versionName=", "version ") or "not installed",
         "Overview › TV app › Install")
    mic = adb.run(["sh", "-c", f"dumpsys package {APP} | grep -m1 'android.permission.RECORD_AUDIO: granted=true'"],
                  timeout=8)
    step("Microphone permission", "granted=true" in mic, "granted" if mic.strip() else "not granted",
         "Install the app from the Overview (it grants it), or allow it in the TV's app settings")
    return steps
