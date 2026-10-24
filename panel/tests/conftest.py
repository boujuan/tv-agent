"""The panel's tests run with the distribution's Python and PySide6 (`/usr/bin/python3 -m pytest panel/tests`),
offscreen, on a home of their own: the agent's settings, history and recordings they could touch are a temporary
folder's, its local API is a port nothing listens on, and the programs the panel runs (systemctl, journalctl, git,
tv-agent…) are stand-ins that do nothing, so no test can restart the real agent or read the real journal."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="tv-agent-panel-test-"))
os.environ["HOME"] = str(HOME)                           # tv_agent's DATA_DIR and CONFIG_DIR follow it
os.environ.pop("TV_AGENT_CONFIG", None)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
config = HOME / ".config/tv-agent"
config.mkdir(parents=True, mode=0o700)
(config / "config.toml").write_text('tv_hosts = ["192.0.2.1"]\nlocal_port = 9\n')     # discard: refused at once
data = HOME / ".local/share/tv-agent"
data.mkdir(parents=True)
(data / "activity.jsonl").write_text("")                 # no one-time import from the journal
bin_dir = HOME / "bin"
bin_dir.mkdir()
for program in ("systemctl", "journalctl", "git", "gh", "tv-agent", "pw-record", "pw-play", "qrencode", "xdg-open",
                "tailscale"):
    (bin_dir / program).write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / program).chmod(0o755)
os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # panel/: import tvpanel


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(HOME, ignore_errors=True)
