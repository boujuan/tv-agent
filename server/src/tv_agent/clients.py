"""Paired phones (the TV Agent phone app): pairing, their tokens, and the listeners they talk to.

Pairing, from the panel: POST /v1/clients/pairing on the local API opens a one-time code (8 characters of Crockford
base32, 40 bits; 5 minutes, one use, 5 wrong tries close it) and gives the link the panel shows as a QR code:
    tvagent://pair?u=<https address>[&u=<another>]&c=<code>&s=<certificate SHA-256>&n=<this computer>
The phone sends the code to POST /v1/pair on the phone listener, over HTTPS pinned to that certificate, and gets its
own token. Typed in instead of scanned, the phone shows the certificate's fingerprint to compare with the panel's.

Tokens: 32 random bytes per phone; only their SHA-256 is kept (clients.json, mode 600, with the phone's name, when it
was paired and last seen) and compared in constant time. Revoking one (the panel, or the phone forgetting this agent)
takes effect at once.

The phone listener (phone_port, 8768) speaks HTTPS with this computer's own certificate (phone-tls.crt/.key, made on
the first start: EC P-256, self-signed), on the LAN address and phone_hosts only. For a valid token it passes the
local API's endpoints in PHONE_ROUTES through, with bodies up to MAX_JSON (MAX_BODY for audio): never the stream link
(it carries the debrid key), the rules editor or the list of phones, and no answer carries an API key, the Stremio
auth key or the Kodi password. Live audio: phone_stream_port (8769), TLS, the TV app's stream protocol (server.py)
with the phone's token in its first line.
"""
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import ssl
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request

from . import __version__
from . import config as config_mod
from .config import Config, lan_address

log = logging.getLogger("tv_agent")
DEVICE = "tv_agent.device"      # in a request's scope: the paired phone the phone listener let in (its id)
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"     # Crockford base32: no I, L, O or U
CODE_LEN = 8
PAIRING_S = 300
PAIRING_TRIES = 5
PAIR_PER_MINUTE = 10            # tries /v1/pair answers in a minute, whatever the code
MAX_DEVICES = 20
MAX_BODY = 400_000              # audio: 12 s of 16 kHz PCM16, as for the TV
MAX_JSON = 64_000
SEEN_EVERY = 60                 # last seen is written to clients.json at most once a minute

# The local API's endpoints a paired phone may use: the panel's (server.py), what remote.py adds, and its own entry.
PHONE_ROUTES = {
    ("GET", "/v1/state"), ("GET", "/v1/stats"), ("GET", "/v1/about"),
    ("POST", "/v1/text"), ("POST", "/v1/audio"), ("POST", "/v1/decide"), ("POST", "/v1/action"),
    ("GET", "/v1/rules"), ("GET", "/v1/learned"), ("POST", "/v1/learn"), ("POST", "/v1/learned/remove"),
    ("GET", "/v1/pipeline"), ("POST", "/v1/pipeline"), ("POST", "/v1/transcribe"),
    ("GET", "/v1/tv/status"), ("GET", "/v1/tv/packages"), ("POST", "/v1/tv/speedtest"), ("POST", "/v1/tv/diagnose"),
    ("POST", "/v1/tv/reconnect"), ("POST", "/v1/tv/key"), ("GET", "/v1/tv/screenshot"), ("POST", "/v1/tv/play-link"),
    ("GET", "/v1/now-playing"), ("POST", "/v1/now-playing/track"), ("GET", "/v1/tv/volume"),
    ("POST", "/v1/remote/key"), ("POST", "/v1/remote/text"), ("GET", "/v1/apps"), ("GET", "/v1/lists"),
    ("POST", "/v1/lists/pick"), ("GET", "/v1/activity"), ("POST", "/v1/activity/mark"), ("GET", "/v1/recording"),
    ("GET", "/v1/settings"), ("POST", "/v1/settings"),
    ("GET", "/v1/clients/me"), ("POST", "/v1/clients/me/forget"),
}


@dataclass
class Device:
    id: str
    name: str
    token_sha256: str
    paired: float
    last_seen: float = 0.0
    last_ip: str = ""

    def public(self) -> dict:
        """What the panel and the phone see of it (never the token's hash)."""
        return {"id": self.id, "name": self.name, "paired": self.paired, "last_seen": self.last_seen or None,
                "last_ip": self.last_ip}


@dataclass
class Pairing:
    code: str
    expires: float                  # time.time()
    tries: int = 0


class PairingError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_code(code: str) -> str:
    """As typed: any case, with or without the dash; O for 0 and I or L for 1 are forgiven."""
    code = re.sub(r"[^0-9A-Z]", "", code.upper())
    return code.translate(str.maketrans("OIL", "011"))


def shown_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def clean_name(name: str) -> str:
    name = " ".join("".join(c for c in name if c.isprintable()).split())[:40]
    return name or "Phone"


class Clients:
    def __init__(self, path: Path | None = None):
        self.path = path or config_mod.CLIENTS_FILE
        self.devices: list[Device] = self._load()
        self.pairing: Pairing | None = None
        self.paired: Device | None = None       # the phone the last pairing let in (the panel's dialog closes on it)
        self.fingerprint = ""                   # the listeners' certificate's SHA-256, once they're up (serve())
        self._tries: deque[float] = deque()
        self._written = 0.0

    def _load(self) -> list[Device]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        out = []
        for d in data.get("devices", []) if isinstance(data, dict) else []:
            try:
                out.append(Device(**{k: d[k] for k in Device.__dataclass_fields__ if k in d}))
            except TypeError:
                continue
        return out

    def _save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"devices": [asdict(d) for d in self.devices]}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)
        self._written = time.time()

    # ---- pairing ----

    def start_pairing(self) -> Pairing:
        """A new code; one still open is closed."""
        self.pairing = Pairing("".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN)), time.time() + PAIRING_S)
        self.paired = None
        return self.pairing

    def open_pairing(self) -> Pairing | None:
        if self.pairing and time.time() > self.pairing.expires:
            self.pairing = None
        return self.pairing

    def pair(self, code: str, name: str, ip: str = "") -> tuple[Device, str]:
        """The phone's code checked: a new device and its token (shown this once), else PairingError."""
        now = time.time()
        while self._tries and now - self._tries[0] > 60:
            self._tries.popleft()
        if len(self._tries) >= PAIR_PER_MINUTE:
            raise PairingError(429, "Too many tries: wait a minute")
        self._tries.append(now)
        p = self.open_pairing()
        if p is None:
            raise PairingError(410, "No pairing open: start one in the panel (Settings › Phones › Pair a phone)")
        if not hmac.compare_digest(normalize_code(code).encode(), p.code.encode()):
            p.tries += 1
            if p.tries >= PAIRING_TRIES:
                self.pairing = None
                raise PairingError(403, "Wrong code, and that pairing is closed now: start another in the panel")
            raise PairingError(403, f"Wrong code ({PAIRING_TRIES - p.tries} tries left)")
        if len(self.devices) >= MAX_DEVICES:
            raise PairingError(409, f"{MAX_DEVICES} phones are paired already: revoke one in the panel first")
        token = "tvp_" + secrets.token_urlsafe(32)
        device = Device(secrets.token_hex(4), clean_name(name), digest(token), now, now, ip)
        self.devices.append(device)
        self._save()
        self.pairing, self.paired = None, device
        return device, token

    # ---- paired phones ----

    def verify(self, token: str, ip: str = "") -> Device | None:
        """The phone this token belongs to, or None. Every hash is compared (in constant time), so how long it takes
        doesn't tell which one matched."""
        if not token:
            return None
        h = digest(token).encode()
        found = None
        for d in self.devices:
            if hmac.compare_digest(h, d.token_sha256.encode()):
                found = d
        if found:
            now, moved = time.time(), bool(ip) and ip != found.last_ip
            found.last_seen, found.last_ip = now, ip or found.last_ip
            if moved or now - self._written > SEEN_EVERY:
                try:
                    self._save()
                except OSError as e:
                    log.warning("clients.json: %s", type(e).__name__)
        return found

    def get(self, device_id: str) -> Device | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def revoke(self, device_id: str) -> bool:
        kept = [d for d in self.devices if d.id != device_id]
        if len(kept) == len(self.devices):
            return False
        self.devices = kept
        self._save()
        return True

    def rename(self, device_id: str, name: str) -> bool:
        d = self.get(device_id)
        if d is None:
            return False
        d.name = clean_name(name)
        self._save()
        return True


# ---- the listeners' certificate ----

def certificate() -> str:
    """The phone listeners' certificate's SHA-256 (hex), made first if there's none (EC P-256, self-signed, 20 years).
    Phones pin it at pairing, so it never changes by itself: delete both files for a new one, then pair again."""
    cert, key = config_mod.TLS_CERT, config_mod.TLS_KEY
    if not (cert.exists() and key.exists()):
        _make_certificate(cert, key)
    return fingerprint(cert.read_text())


def fingerprint(pem: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def _make_certificate(cert: Path, key: Path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"tv-agent on {socket.gethostname()}"[:64])])
    now = datetime.datetime.now(datetime.UTC)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=365 * 20))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .sign(k, hashes.SHA256()))
    key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path, data in ((key, k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption())),
                       (cert, c.public_bytes(serialization.Encoding.PEM))):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # private from the first byte
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    log.info("made the phones' certificate (%s)", cert)


def tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(config_mod.TLS_CERT, config_mod.TLS_KEY)
    return ctx


# ---- addresses ----

def addresses(cfg: Config) -> list[str]:
    """Where the phones' listeners are: the LAN address, then phone_hosts."""
    return list(dict.fromkeys([lan_address(cfg), *cfg.phone_hosts]))


def host_port(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def pairing_link(cfg: Config, p: Pairing, fp: str) -> str:
    query = [("u", f"https://{host_port(h, cfg.phone_port)}") for h in addresses(cfg)]
    query += [("c", p.code), ("s", fp), ("n", socket.gethostname())]
    return "tvagent://pair?" + urlencode(query)


def listen_socket(host: str, port: int) -> socket.socket:
    """A listening socket bound to one address (uvicorn and asyncio are given it, so a failed bind is ours to handle)."""
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(100)
        s.setblocking(False)
    except OSError:
        s.close()
        raise
    return s


# ---- the phone listener ----

async def _reply(send, status: int, body: dict):
    data = json.dumps(body, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


async def _read(receive, limit: int) -> bytes:
    body = b""
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            return body
        body += msg.get("body", b"")
        if len(body) > limit or not msg.get("more_body"):
            return body[:limit + 1]


def gateway(svc, inner):
    """The phone listener's ASGI app: POST /v1/pair, and for a paired phone's token the PHONE_ROUTES of the local API
    (`inner`), whose checks let a request through when the scope names the phone (Service.check)."""
    clients: Clients = svc.clients

    async def app(scope, receive, send):
        if scope["type"] != "http":                 # lifespan is off for this listener; no websockets
            return
        method, path = scope["method"], scope["path"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        ip = (scope.get("client") or ("",))[0]
        if "transfer-encoding" in headers:          # bodies have a length, so a limit holds before reading them
            return await _reply(send, 411, {"detail": "Content-Length required"})
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            return await _reply(send, 400, {"detail": "Bad Content-Length"})
        if (method, path) == ("POST", "/v1/pair"):
            if length > 4096:
                return await _reply(send, 413, {"detail": "Too large"})
            return await _reply(send, *pair_reply(svc, await _read(receive, 4096), ip))
        if (method, path) not in PHONE_ROUTES:
            return await _reply(send, 404, {"detail": "Not Found"})
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        device = clients.verify(token, ip)
        if device is None:
            return await _reply(send, 401, {"detail": "Not paired (or revoked): pair this phone again"})
        if length > (MAX_BODY if path == "/v1/audio" else MAX_JSON):
            return await _reply(send, 413, {"detail": "Too large"})
        await inner({**scope, DEVICE: device.id}, receive, send)

    return app


def pair_reply(svc, body: bytes, ip: str) -> tuple[int, dict]:
    try:
        data = json.loads(body or b"{}")
        code, name = str(data.get("code", "")), str(data.get("name", ""))
    except (ValueError, AttributeError):
        return 422, {"detail": "JSON with the code and the phone's name"}
    try:
        device, token = svc.clients.pair(code, name, ip)
    except PairingError as e:
        log.info("phone pairing refused (from %s): %s", ip, e.message)
        return e.status, {"detail": e.message}
    log.info("phone paired: %s (from %s)", device.name, ip)
    return 200, {"token": token, "device": device.public(), "agent": about(svc), "stream_port": svc.cfg.phone_stream_port}


def about(svc) -> dict:
    return {"name": socket.gethostname(), "version": __version__}


# ---- the local API's endpoints for them ----

def routes(svc) -> APIRouter:
    r = APIRouter()
    clients: Clients = svc.clients

    def local_only(request: Request):
        """The panel's (localhost and the agent token): never a phone, even if PHONE_ROUTES had it by mistake."""
        svc.check(request, lan=False)
        if request.scope.get(DEVICE):
            raise HTTPException(403)

    def pairing_state() -> dict:
        p = clients.open_pairing()
        return {"open": p is not None, "expires_in": round(p.expires - time.time()) if p else 0,
                "paired": clients.paired.public() if clients.paired else None}

    @r.get("/v1/clients")
    async def list_clients(request: Request):
        local_only(request)
        return {"devices": [d.public() for d in clients.devices], "pairing": pairing_state(), "enabled": svc.cfg.phones,
                "listening": bool(clients.fingerprint), "fingerprint": clients.fingerprint,
                "port": svc.cfg.phone_port, "stream_port": svc.cfg.phone_stream_port}

    @r.post("/v1/clients/pairing")
    async def start_pairing(request: Request):
        """A one-time code and the QR code's link (the panel shows both)."""
        local_only(request)
        if not clients.fingerprint:
            raise HTTPException(409, "The phones' listener isn't running: phones = true in config.toml, then restart "
                                     "the voice agent")
        p = clients.start_pairing()
        hosts = addresses(svc.cfg)
        return {"code": shown_code(p.code), "expires_in": PAIRING_S, "link": pairing_link(svc.cfg, p, clients.fingerprint),
                "addresses": [f"https://{host_port(h, svc.cfg.phone_port)}" for h in hosts],
                "fingerprint": clients.fingerprint}

    @r.get("/v1/clients/pairing")
    async def pairing_status(request: Request):
        local_only(request)
        return pairing_state()

    @r.post("/v1/clients/pairing/cancel")
    async def cancel_pairing(request: Request):
        local_only(request)
        clients.pairing = None
        return pairing_state()

    @r.post("/v1/clients/revoke")
    async def revoke(request: Request):
        local_only(request)
        device = clients.get(str((await request.json()).get("id", "")))
        if device is None or not clients.revoke(device.id):
            raise HTTPException(404, "no such phone")
        log.info("phone revoked: %s", device.name)
        return {"ok": True}

    @r.post("/v1/clients/rename")
    async def rename(request: Request):
        local_only(request)
        body = await request.json()
        if not clients.rename(str(body.get("id", "")), str(body.get("name", ""))):
            raise HTTPException(404, "no such phone")
        return {"ok": True}

    def me(request: Request) -> Device:
        svc.check(request, lan=False)
        device = clients.get(request.scope.get(DEVICE) or "")
        if device is None:
            raise HTTPException(404, "only for a paired phone")
        return device

    @r.get("/v1/clients/me")
    async def whoami(request: Request):
        """The phone's own entry, and the agent it's paired with."""
        return {"device": me(request).public(), "agent": about(svc), "stream_port": svc.cfg.phone_stream_port}

    @r.post("/v1/clients/me/forget")
    async def forget(request: Request):
        """The phone forgetting this agent: its token stops working."""
        device = me(request)
        clients.revoke(device.id)
        log.info("phone unpaired itself: %s", device.name)
        return {"ok": True}

    return r
