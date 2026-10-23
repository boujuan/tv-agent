"""Settings (~/.config/tv-agent/config.toml) and secrets (secrets.toml next to it, mode 0600).

The secrets file holds the agent token the TV app sends, the Stremio auth key (full account access), the Kodi
web password and, when set, the Liquid and OpenRouter API keys of the cloud fallbacks. The daemon refuses to start if the folder or that file can be read by other accounts.
"""
import json
import os
import re
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
CLIENTS_FILE = CONFIG_DIR / "clients.json"       # paired phones: names and their tokens' SHA-256 (clients.py)
TLS_CERT = CONFIG_DIR / "phone-tls.crt"          # the phones' listeners' certificate, pinned by the phone app
TLS_KEY = CONFIG_DIR / "phone-tls.key"

SECRET_NAMES = ("agent_token", "stremio_auth_key", "kodi_password", "liquid_api_key", "openrouter_api_key")
OPTIONAL_SECRETS = ("liquid_api_key", "openrouter_api_key")     # only for the cloud steps
STEPS = ("phrases", "patterns", "llm", "d1")                     # the decision steps (decide/router.py)
D1_WHERE = ("local-first", "hosted-first", "local", "hosted")
# What the hosted speech model should hear by name: the apps, the way they're said
STT_VOCABULARY = ("Stremio", "TizenTube", "YouTube", "YouTube Music", "Kodi", "Twitch", "Movistar Plus", "Prime Video",
                  "RTVE Play", "TDTChannels")
UPDATES = ("auto", "notify", "off")                               # updates.py
LEGACY = ("hosted_d1", "llm_fallback", "llm_first")              # before decide_steps, decide_off and d1_where


@dataclass
class Config:
    tv_hosts: list[str] = field(default_factory=list)   # the TV's addresses (e.g. Ethernet, then Wi-Fi); empty: mDNS
    adb_port: int = 5555
    adb_key: Path = Path.home() / ".android/adbkey"
    lan_host: str = ""                      # this computer on the TV's network; empty: worked out (never 0.0.0.0)
    lan_port: int = 8765
    local_port: int = 8766
    stream_port: int = 8767                 # the TV app's live audio (LAN, like lan_port)
    phones: bool = True                     # paired phones' listeners (clients.py): HTTPS API and live audio
    phone_port: int = 8768
    phone_stream_port: int = 8769
    phone_hosts: list[str] = field(default_factory=list)   # more addresses for them besides lan_host (e.g. Tailscale)
    d1_url: str = "http://127.0.0.1:8091"
    d1_model: Path = DATA_DIR / "models/d1-3B/d1-3B-Q4_K_M.gguf"
    llama_server: str = ""                  # llama.cpp's llama-server; empty: on PATH or under ~/.local/opt
    stt_model: str = "parakeet-v2"          # speech model: "parakeet-v2" (English), "parakeet-v3" (25 languages), a path
    stt_where: str = "local"                # "local", "hosted" (OpenRouter), or "local-first" / "hosted-first" (the other if it fails)
    stt_hosted_model: str = "mistralai/voxtral-mini-transcribe-3-0"   # an OpenRouter transcription model
    # The hosted model's language hint: "native" (native_language: a Spanish hint keeps English commands intact, an
    # English one garbles Spanish), "" (it guesses, and gets short commands wrong), or an ISO 639-1 code
    stt_language: str = "native"
    # Names the hosted model should hear (AssemblyAI's keyterms: "Stremio", not "este remedio"), and whether your
    # library's titles and Twitch favourites are added to them
    stt_vocabulary: list[str] = field(default_factory=lambda: list(STT_VOCABULARY))
    stt_vocabulary_library: bool = True
    keep_recordings: bool = False           # each command's audio kept (the last 300), playable in the Activity tab
    kodi_port: int = 8080
    kodi_user: str = "kodi"
    max_height: int = 2160                  # preferred maximum resolution (lines)
    max_mbps: float = 60.0                  # stay under what the TV's network link carries; 0: no limit
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
    auto_skip: bool = False                 # intros and recaps skipped by themselves when the timestamps are sure
    # How a command is decided (the panel's Decisions tab changes these live): the steps in order, the ones
    # switched off, and which d1 answers (local, Liquid's hosted one with `tv-agent setup liquid`, or either first)
    decide_steps: list[str] = field(default_factory=lambda: list(STEPS))
    decide_off: list[str] = field(default_factory=list)
    d1_where: str = "local-first"
    hosted_d1_model: str = "d1"             # "d1" / "d1-ultra" (Liquid's API) or an OpenRouter decision model id
    llm_model: str = "deepseek/deepseek-v4.1-flash-1017"   # any OpenRouter model (`tv-agent setup openrouter`)
    llm_reasoning: str = "none"             # reasoning effort ("none": off where the model allows); "" sends none
    llm_private: bool = False               # only providers that keep no data
    log_transcripts: bool = True
    # New releases on GitHub (updates.py): "notify" checks every few hours (the panel and the phones show them),
    # "auto" also installs the TV app's by itself, "off" never asks GitHub
    updates: str = "notify"
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def secret(self, name: str) -> str | None:
        return self.secrets.get(name) or None


def _private(path: Path, mode: int) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & ~mode == 0


def check_permissions():
    """Raise if the config folder, the secrets file, the paired phones or their listeners' key is readable by
    anyone else."""
    if CONFIG_DIR.exists() and not _private(CONFIG_DIR, 0o700):
        raise SystemExit(f"{CONFIG_DIR} must be private: chmod 700 {CONFIG_DIR}")
    for path in (SECRETS_FILE, CLIENTS_FILE, TLS_KEY):
        if path.exists() and not _private(path, 0o600):
            raise SystemExit(f"{path} must be private: chmod 600 {path}")


def load() -> Config:
    check_permissions()
    cfg = Config()
    data = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    legacy = {k: data.pop(k) for k in LEGACY if k in data}
    for key, value in data.items():
        if key == "secrets" or not hasattr(cfg, key):
            raise SystemExit(f"{CONFIG_FILE}: unknown setting {key!r}")
        default = getattr(cfg, key)
        setattr(cfg, key, Path(value).expanduser() if isinstance(default, Path) else value)
    # the settings these replaced
    if legacy.get("llm_fallback") is False and "decide_off" not in data:
        cfg.decide_off = [*cfg.decide_off, "llm"]
    if legacy.get("llm_first") is False and "decide_steps" not in data:
        cfg.decide_steps = ["phrases", "patterns", "d1", "llm"]
    if legacy.get("hosted_d1") is False and "d1_where" not in data:
        cfg.d1_where = "local"
    if cfg.player not in ("kodi", "just"):
        raise SystemExit(f"{CONFIG_FILE}: player must be \"kodi\" or \"just\"")
    if not set(cfg.decide_steps) | set(cfg.decide_off) <= set(STEPS):
        raise SystemExit(f"{CONFIG_FILE}: decide_steps and decide_off take {', '.join(STEPS)}")
    cfg.decide_steps = [s for s in cfg.decide_steps if s in STEPS] + [s for s in STEPS if s not in cfg.decide_steps]
    if cfg.stt_where not in ("local", "hosted", "local-first", "hosted-first"):
        raise SystemExit(f"{CONFIG_FILE}: stt_where must be local, hosted, local-first or hosted-first")
    if cfg.updates not in UPDATES:
        raise SystemExit(f"{CONFIG_FILE}: updates must be {', '.join(UPDATES)}")
    if cfg.stt_language not in ("native", "") and not re.fullmatch(r"[a-z]{2}", cfg.stt_language):
        raise SystemExit(f"{CONFIG_FILE}: stt_language must be \"native\", \"\" or a two-letter code such as \"es\"")
    if not isinstance(cfg.stt_vocabulary, list) or not all(isinstance(x, str) for x in cfg.stt_vocabulary):
        raise SystemExit(f"{CONFIG_FILE}: stt_vocabulary must be a list of words and names")
    if cfg.d1_where not in D1_WHERE:
        raise SystemExit(f"{CONFIG_FILE}: d1_where must be one of {', '.join(D1_WHERE)}")
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


def written(keys: tuple[str, ...]) -> bool:
    """Whether config.toml sets any of these."""
    if not CONFIG_FILE.exists():
        return False
    return any(l.split("=", 1)[0].strip() in keys for l in CONFIG_FILE.read_text().splitlines())


def unset(keys: tuple[str, ...]):
    """Remove settings from config.toml (the replaced ones, once their new form is written)."""
    if not CONFIG_FILE.exists():
        return
    lines = CONFIG_FILE.read_text().splitlines(keepends=True)
    kept = [l for l in lines if l.split("=", 1)[0].strip() not in keys]
    if kept != lines:
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text("".join(kept))
        os.replace(tmp, CONFIG_FILE)


def new_token() -> str:
    return random_tokens.token_urlsafe(32)
