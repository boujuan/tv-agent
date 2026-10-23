"""Installing tv-agent on a computer and its app on the TV: user services, the TV app, and pairing.

    tv-agent setup services [--enable]   write ~/.config/systemd/user/{llama-d1,tv-agent}.service for this install
    tv-agent tv install <apk>            install the TV app (grant the microphone, allow it to start) or TV Agent
                                         Player (allow it to start, compile it ahead of time), and pair it
    tv-agent tv pair                     tell the TV app, and TV Agent Player if it's installed, where this agent is
                                         and the token each must send
"""
import glob
import os
import zipfile
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import clients
from .config import Config, lan_address, new_token, read_secret, save_secret
from .tv.adb import Adb
from .tv.tvplayer import PACKAGE as PLAYER

APP = "com.boujuan.tvagent"
UNIT_DIR = Path.home() / ".config/systemd/user"


def llama_server(cfg: Config) -> Path:
    """llama.cpp's server: the configured one, one on PATH, or the newest under ~/.local/opt/llama.cpp."""
    if cfg.llama_server:
        return Path(cfg.llama_server).expanduser()
    if found := shutil.which("llama-server"):
        return Path(found)
    builds = sorted(glob.glob(str(Path.home() / ".local/opt/llama.cpp/*/*/llama-server")))
    if builds:
        return Path(builds[-1])
    raise SystemExit("llama-server not found: install llama.cpp (Vulkan build) or set llama_server in config.toml")


def tv_agent_command() -> Path:
    """This install's own tv-agent command (a virtualenv's, or /usr/bin/tv-agent from a package)."""
    here = Path(sys.argv[0]).resolve()
    if here.name == "tv-agent" and os.access(here, os.X_OK):
        return here
    if found := shutil.which("tv-agent"):
        return Path(found)
    raise SystemExit("can't find the tv-agent command")


def _arg(path) -> str:
    """One ExecStart argument for systemd: quoted, with its specifier character escaped."""
    text = str(path)
    if "\n" in text:
        raise SystemExit(f"{text!r}: a path with a line break can't go in a systemd unit")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def write_services(cfg: Config, enable: bool) -> list[Path]:
    server = llama_server(cfg)
    if not cfg.d1_model.exists():
        raise SystemExit(f"{cfg.d1_model} is missing: run scripts/fetch-models.sh (or set d1_model)")
    port = urlparse(cfg.d1_url).port or 8091
    libdir = f"Environment=LD_LIBRARY_PATH={server.parent}\n" if (server.parent / "libggml.so").exists() or \
        glob.glob(str(server.parent / "libllama*.so")) else ""
    units = {
        "llama-d1.service": f"""[Unit]
Description=d1-3B decision model for tv-agent (llama.cpp, localhost only)

[Service]
{libdir}ExecStart={_arg(server)} -m {_arg(cfg.d1_model)} --host 127.0.0.1 --port {port} -ngl 99 -c 4096 --parallel 1 -fa on --alias d1-3B --no-webui
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
""",
        "tv-agent.service": f"""[Unit]
Description=tv-agent: voice control for the TV (listens on this computer's address on the TV's network)
# Not Wants=llama-d1: the local d1 is optional (the Decisions tab can leave it off), so it starts on its own
After=llama-d1.service network-online.target

[Service]
ExecStart={tv_agent_command()} serve
# Away from the TV's network the address doesn't exist and the bind fails: retry slowly instead of spinning.
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
""",
    }
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in units.items():
        path = UNIT_DIR / name
        if path.is_symlink():
            path.unlink()                  # older installs linked the repo's files
        path.write_text(text)
        written.append(path)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if enable:                             # the local d1 only when the decision steps use it
        local_d1 = "d1" not in cfg.decide_off and cfg.d1_where != "hosted"
        subprocess.run(["systemctl", "--user", "enable", "--now",
                        *(["llama-d1.service"] if local_d1 else []), "tv-agent.service"], check=True)
    return written


def pair(cfg: Config, adb: Adb) -> str:
    """Send the agent's address, token and audio-stream port to the TV app (a broadcast only ADB may send)."""
    token = cfg.secret("agent_token")
    if not token:
        raise SystemExit("no agent token yet: run `tv-agent setup token`")
    url = f"http://{lan_address(cfg)}:{cfg.lan_port}"
    out = adb.run(["am", "broadcast", "-n", f"{APP}/.ConfigReceiver", "--es", "url", url, "--es", "token", token,
                   "--ei", "stream_port", str(cfg.stream_port)])
    if "result=-1" not in out.replace(" ", ""):
        raise SystemExit(f"the TV app didn't take the settings ({out.strip()[-80:]}); is it installed?")
    return url


def is_player_apk(apk: Path) -> bool:
    """Whether an APK is TV Agent Player rather than the TV app: its package name is in its manifest (binary XML keeps
    its strings in UTF-16)."""
    with zipfile.ZipFile(apk) as z:
        return PLAYER.encode("utf-16-le") in z.read("AndroidManifest.xml")


def installed(adb: Adb, package: str) -> bool:
    return "package:" in adb.run(["pm", "path", package])


def pair_player(cfg: Config, adb: Adb) -> str:
    """TV Agent Player's pairing (its PairReceiver, which only ADB may send to): this agent's address and player_port,
    the player's own token (made the first time), and the certificate it must find there (the phones' listeners'
    one, clients.py). The running agent reads the token at each connection: no restart."""
    token = read_secret("player_token")
    if not token:
        token = new_token()
        save_secret("player_token", token)
    cfg.secrets["player_token"] = token
    host = lan_address(cfg)
    out = adb.run(["am", "broadcast", "-n", f"{PLAYER}/.PairReceiver", "--es", "host", host,
                   "--ei", "port", str(cfg.player_port), "--es", "token", token, "--es", "cert", clients.certificate()])
    if "result=-1" not in out.replace(" ", ""):
        raise SystemExit(f"TV Agent Player didn't take the settings ({out.strip()[-80:]}); is it installed?")
    return f"{host}:{cfg.player_port}"


def install_app(cfg: Config, adb: Adb, apk: Path) -> str:
    """Installs the TV app or TV Agent Player (whichever the APK is) and pairs it; returns where it talks to."""
    player = is_player_apk(apk)
    remote = "/data/local/tmp/tv-agent.apk"
    adb.push(apk, remote)
    out = adb.run(["pm", "install", "-r", remote], timeout=120)
    adb.run(["rm", "-f", remote])
    if "Success" not in out:
        raise SystemExit(f"pm install failed: {out.strip()}")
    if player:
        # TCL's TclAppBoot blocks sideloaded apps from starting without this.
        adb.run(["appops", "set", PLAYER, "AUTO_START", "allow"])
        adb.run(["appops", "write-settings"])
        # Compiled ahead of time now: this TV installs a sideloaded APK as `verify` (interpreted), ignoring the profile
        # inside it, and a file's header is read ~0.2 s faster compiled (tv-agent-player's DESIGN.md). ~6 s, once.
        adb.run(["cmd", "package", "compile", "-m", "speed", "-f", PLAYER], timeout=180, quiet_s=180)
        return pair_player(cfg, adb)
    adb.run(["pm", "grant", APP, "android.permission.RECORD_AUDIO"])
    # TCL's TclAppBoot blocks sideloaded apps from starting without this (its on-screen messages need it).
    adb.run(["appops", "set", APP, "AUTO_START", "allow"])
    adb.run(["appops", "write-settings"])
    return pair(cfg, adb)
