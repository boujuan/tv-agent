"""MCP server (stdio) so Claude Code or another MCP client can drive the TV with the same closed set of actions
as the voice path. It is a thin client of the running daemon (127.0.0.1:8766), so there is one ADB connection
and one view of what's playing. There is deliberately no shell tool.

    claude mcp add --scope user tv-agent -- ~/Coding/TV_HACK/tv-agent/.venv/bin/tv-agent-mcp
"""
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer

from . import config

mcp = MCPServer("tv-agent")


def _client() -> httpx.Client:
    cfg = config.load()
    return httpx.Client(base_url=f"http://127.0.0.1:{cfg.local_port}", timeout=60,
                        headers={"Authorization": f"Bearer {cfg.secret('agent_token') or ''}"})


def _action(body: dict) -> str:
    with _client() as c:
        r = c.post("/v1/action", json=body)
        if r.status_code != 200:
            return f"error {r.status_code}: {r.text[:200]}"
        return r.json()["say"]


@mcp.tool()
def tv_say(text: str) -> str:
    """Do what a spoken command would do, e.g. "play the bear season 3 episode 2" or "volume 15"."""
    with _client() as c:
        r = c.post("/v1/text", json={"text": text, "source": "mcp"})
        return r.json()["say"] if r.status_code == 200 else f"error {r.status_code}"


@mcp.tool()
def tv_state() -> dict:
    """What the agent last started and Kodi's position, if Kodi is playing."""
    with _client() as c:
        return c.get("/v1/state").json()


@mcp.tool()
def tv_play(title: str, season: int | None = None, episode: int | None = None, restart: bool = False) -> str:
    """Play a movie or episode in Kodi from the user's Stremio addons (English audio + subs unless Spanish-made)."""
    return _action({"type": "play_title", "title": title, "season": season, "episode": episode, "restart": restart})


@mcp.tool()
def tv_continue(title: str | None = None) -> str:
    """Resume a title from Stremio's Continue Watching (the most recent one if no title)."""
    return _action({"type": "continue_watching", "title": title})


@mcp.tool()
def tv_next_episode() -> str:
    """Play the episode after the current or last-watched one."""
    return _action({"type": "next_episode"})


@mcp.tool()
def tv_open_in_stremio(title: str, season: int | None = None, episode: int | None = None) -> str:
    """Show a title's page (or an episode) in the Stremio app without playing it."""
    return _action({"type": "open_in_stremio", "title": title, "season": season, "episode": episode})


@mcp.tool()
def tv_youtube(query: str) -> str:
    """Play the top YouTube result for a search in TizenTube."""
    return _action({"type": "youtube", "query": query})


@mcp.tool()
def tv_twitch(channel: str) -> str:
    """Open a Twitch channel's live stream."""
    return _action({"type": "twitch", "channel": channel})


@mcp.tool()
def tv_open_app(app: str) -> str:
    """Open one of the TV's apps by its key in the rules' app list (e.g. tizentube, stremio, kodi, twitch,
    movistar, prime)."""
    return _action({"type": "open_app", "app": app})


@mcp.tool()
def tv_transport(op: Literal["pause", "resume", "stop", "forward", "back", "seek_to", "next", "previous"],
                 seconds: int | None = None) -> str:
    """Playback control. seconds: how far for forward/back, or the position for seek_to."""
    return _action({"type": "transport", "op": op, "seconds": seconds})


@mcp.tool()
def tv_volume(op: Literal["up", "down", "set", "mute", "unmute"], amount: int | None = None) -> str:
    """Volume (0-100 on this TV). amount: steps for up/down, the level for set."""
    return _action({"type": "volume", "op": op, "amount": amount})


@mcp.tool()
def tv_home() -> str:
    """Go to the home screen."""
    return _action({"type": "home"})


def main():
    mcp.run()
