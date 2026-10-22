"""One persistent ADB connection to the TV (pure Python, adb_shell), reusing the key the TV already trusts.

run(argv) is the only way the agent talks to the TV's shell, and the only place that builds a command string:
every argument is shell-quoted, so a title or query can never become shell syntax.
"""
import logging
import shlex
import socket
import subprocess
import threading
import time

from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.sign_pythonrsa import PythonRSASigner

from ..config import Config

log = logging.getLogger(__name__)


class TvUnreachable(RuntimeError):
    pass


def mdns_adb_host(timeout: float = 6.0) -> str | None:
    """A device announcing ADB on the local network (avahi), for when DHCP gave the TV another address."""
    try:
        out = subprocess.run(["avahi-browse", "-rpt", "_adb._tcp"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.splitlines():
        f = line.split(";")
        if len(f) > 7 and f[0] == "=" and f[2] == "IPv4":
            return f[7]
    return None


def quote(argv: list[str]) -> str:
    if not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a non-empty list of strings")
    return " ".join(shlex.quote(a) for a in argv)


class Adb:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._dev: AdbDeviceTcp | None = None
        self._lock = threading.Lock()
        self.host: str | None = None
        self._used = 0.0

    def _signer(self) -> PythonRSASigner:
        key = self.cfg.adb_key
        return PythonRSASigner(key.with_suffix(".pub").read_text(), key.read_text())

    def _find_host(self) -> str:
        """The configured addresses in order (e.g. Ethernet, then Wi-Fi), then the network's mDNS (_adb._tcp)."""
        for host in self.cfg.tv_hosts:
            try:
                socket.create_connection((host, self.cfg.adb_port), timeout=0.8).close()
                return host
            except OSError:
                continue
        if host := mdns_adb_host():
            return host
        raise TvUnreachable("TV not reachable over ADB (is it on, with USB debugging enabled?)")

    def _connect(self):
        self.close()
        host = self._find_host()
        dev = AdbDeviceTcp(host, self.cfg.adb_port, default_transport_timeout_s=5.0)
        dev.connect(rsa_keys=[self._signer()], transport_timeout_s=5.0, auth_timeout_s=5.0)
        self._dev, self.host = dev, host
        log.info("ADB connected to %s", host)

    def close(self):
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
        self._dev = None

    def run(self, argv: list[str], timeout: float = 10.0) -> str:
        """Run one command on the TV and return its output. Reconnects once if the connection went stale."""
        cmd = quote(argv)
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._dev is None or not self._dev.available:
                        self._connect()
                    out = self._dev.shell(cmd, timeout_s=timeout, read_timeout_s=timeout)
                    self._used = time.monotonic()
                    return out if isinstance(out, str) else out.decode(errors="replace")
                except TvUnreachable:
                    raise
                except Exception as e:  # adb_shell raises several transport/OS error types
                    log.warning("ADB command failed (attempt %d): %s", attempt, type(e).__name__)
                    self.close()
                    if attempt == 2:
                        raise TvUnreachable(f"ADB failed: {type(e).__name__}") from e
        raise AssertionError("unreachable")

    def push(self, local, remote: str, timeout: float = 120.0):
        """Copy a (small) file to the TV."""
        with self._lock:
            if self._dev is None or not self._dev.available:
                self._connect()
            self._dev.push(str(local), remote, read_timeout_s=timeout)

    def keepalive(self):
        """Cheap round trip so the next real command doesn't pay for a reconnect."""
        if time.monotonic() - self._used > 50:
            try:
                self.run(["true"], timeout=3)
            except TvUnreachable:
                pass
