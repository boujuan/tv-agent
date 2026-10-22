"""Settings (~/.config/tv-agent/config.toml) and secrets (secrets.toml next to it, mode 0600).

The secrets file holds the agent token the TV app sends, the Stremio auth key (full account access) and the
Kodi web password. The daemon refuses to start if the folder or that file can be read by other accounts.
"""
import json
import os
import secrets as random_tokens
import socket
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("TV_AGENT_CONFIG", Path.home() / ".config/tv-agent"))
DATA_DIR = Path.home() / ".local/share/tv-agent"
CONFIG_FILE = CONFIG_DIR / "config.toml"
SECRETS_FILE = CONFIG_DIR / "secrets.toml"

SECRET_NAMES = ("agent_token", "stremio_auth_key", "kodi_password")


@dataclass
class Config:
    tv_hosts: list[str] = field(default_factory=list)   # the TV's addresses (e.g. Ethernet, then Wi-Fi); empty: mDNS
    adb_port: int = 5555
    adb_key: Path = Path.home() / ".android/adbkey"
    lan_host: str = ""                      # this computer on the TV's network; empty: worked out (never 0.0.0.0)
    lan_port: int = 8765
    local_port: int = 8766
    stream_port: int = 8767                 # the TV app's live audio (LAN, like lan_port)
    d1_url: str = "http://127.0.0.1:8091"
    d1_model: Path = DATA_DIR / "models/d1-3B/d1-3B-Q4_K_M.gguf"
    llama_server: str = ""                  # llama.cpp's llama-server; empty: on PATH or under ~/.local/opt
    stt_model: Path = DATA_DIR / "models/parakeet-v2/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
    kodi_port: int = 8080
    kodi_user: str = "kodi"
    max_height: int = 2160                  # preferred maximum resolution (lines)
    max_mbps: float = 60.0                  # stay under what the TV's network link carries
    probe_streams: bool = True              # turn off while the TV uses Mullvad (debrid one-IP rules)
    player: str = "kodi"                    # "kodi" (full remote control) or "just" (Just Player)
    # Tracks Kodi switches to after a start (ISO 639-2 codes, as Kodi reports them). Titles made in a country
    # that speaks native_language play in it with subtitles off; everything else gets audio_language (when the
    # file has it, else the original) with the first available subtitle_languages track on.
    audio_language: str = "eng"
    subtitle_languages: list[str] = field(default_factory=lambda: ["eng", "spa"])
    native_language: str = "spa"
    twitch_favorites: list[str] = field(default_factory=list)
    binge: bool = True                      # at the end of an episode the next one starts (also by voice)
    log_transcripts: bool = True
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def secret(self, name: str) -> str | None:
        return self.secrets.get(name) or None


def _private(path: Path, mode: int) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & ~mode == 0


def check_permissions():
    """Raise if the config folder or the secrets file is readable by anyone else."""
    if CONFIG_DIR.exists() and not _private(CONFIG_DIR, 0o700):
        raise SystemExit(f"{CONFIG_DIR} must be private: chmod 700 {CONFIG_DIR}")
    if SECRETS_FILE.exists() and not _private(SECRETS_FILE, 0o600):
        raise SystemExit(f"{SECRETS_FILE} must be private: chmod 600 {SECRETS_FILE}")


def load() -> Config:
    check_permissions()
    cfg = Config()
    if CONFIG_FILE.exists():
        data = tomllib.loads(CONFIG_FILE.read_text())
        for key, value in data.items():
            if key == "secrets" or not hasattr(cfg, key):
                raise SystemExit(f"{CONFIG_FILE}: unknown setting {key!r}")
            default = getattr(cfg, key)
            setattr(cfg, key, Path(value).expanduser() if isinstance(default, Path) else value)
    if cfg.player not in ("kodi", "just"):
        raise SystemExit(f"{CONFIG_FILE}: player must be \"kodi\" or \"just\"")
    if SECRETS_FILE.exists():
        cfg.secrets = {k: v for k, v in tomllib.loads(SECRETS_FILE.read_text()).items() if k in SECRET_NAMES}
    return cfg


def lan_address(cfg: Config) -> str:
    """This computer's address on the TV's network: lan_host if set, else the address the route to the TV uses.
    The TV listener binds only there, never to every interface."""
    if cfg.lan_host:
        return cfg.lan_host
    for host in cfg.tv_hosts:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((host, 9))           # no packet is sent; this only picks the route
                return s.getsockname()[0]
        except OSError:
            continue
    raise SystemExit(f"{CONFIG_FILE}: set tv_hosts (the TV's address) or lan_host (this computer's address on "
                     "the TV's network)")


def save_secret(name: str, value: str | None):
    """Set (or with None, remove) one secret, keeping the file 0600 from its first byte."""
    assert name in SECRET_NAMES, name
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    current = tomllib.loads(SECRETS_FILE.read_text()) if SECRETS_FILE.exists() else {}
    if value is None:
        current.pop(name, None)
    else:
        current[name] = value
    tmp = SECRETS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        # JSON string escaping is valid TOML basic-string escaping for these values.
        f.writelines(f"{k} = {json.dumps(v)}\n" for k, v in sorted(current.items()))
    os.replace(tmp, SECRETS_FILE)


def set_value(cfg: Config, key: str, value):
    """Change one setting in config.toml (its line, or a new one) and in cfg; the rest of the file is kept."""
    assert key in Config.__dataclass_fields__ and key != "secrets", key
    line = f"{key} = {json.dumps(value)}\n"
    text = CONFIG_FILE.read_text() if CONFIG_FILE.exists() else ""
    lines = [line if l.split("=", 1)[0].strip() == key else l for l in text.splitlines(keepends=True)]
    if line not in lines:
        lines.append(line if not lines or lines[-1].endswith("\n") else "\n" + line)
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text("".join(lines))
    os.replace(tmp, CONFIG_FILE)
    setattr(cfg, key, value)


def new_token() -> str:
    return random_tokens.token_urlsafe(32)
