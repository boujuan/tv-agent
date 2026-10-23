"""Shared for every test: this computer's real Tailscale address (its CLI) never leaks into a test's addresses.
A test that wants one sets it with monkeypatch afterwards."""
import pytest

from tv_agent import clients, config
from tv_agent.tv import tvplayer


@pytest.fixture(autouse=True)
def no_tailscale(monkeypatch):
    for module in (config, clients, tvplayer):
        monkeypatch.setattr(module, "tailscale_address", lambda: None)
