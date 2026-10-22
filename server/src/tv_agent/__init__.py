"""Voice decision agent for an Android TV."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tv-agent")
except PackageNotFoundError:          # running from a source tree that isn't installed
    __version__ = "0.0.0+source"
