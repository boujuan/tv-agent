#!/usr/bin/env python3
"""TV Agent panel: status, services, activity, decision rules and settings for the tv-agent voice control on this
laptop, with a system-tray icon. Native Qt from the distro (PySide6, psutil); no network listener of its own.

    /usr/bin/python3 ~/Coding/TV_HACK/tv-agent/panel/tv_agent_panel.py [--tray]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tvpanel.app import main  # noqa: E402

if __name__ == "__main__":
    main()
