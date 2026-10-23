"""Kodi on the TV, over its JSON-RPC web API (Settings > Services > Control > Allow remote control via HTTP).

Stream URLs go to Kodi as JSON (Player.Open), never through a shell. Transport commands are explicit
(PlayPause with play=true/false, never a toggle) so a repeated or misheard command can't flip the state back.
"""
import asyncio
import logging

import httpx

from .adb import Adb
from .apps import APPS, open_app

log = logging.getLogger(__name__)


class KodiError(RuntimeError):
    pass


def _time(ms: int) -> dict:
    s, ms = divmod(max(0, int(ms)), 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return {"hours": h, "minutes": m, "seconds": s, "milliseconds": ms}


def _ms(t: dict) -> int:
    return ((t.get("hours", 0) * 60 + t.get("minutes", 0)) * 60 + t.get("seconds", 0)) * 1000 + t.get("milliseconds", 0)


class Kodi:
    def __init__(self, http: httpx.AsyncClient, adb: Adb, port: int, user: str, password: str | None):
        self.http, self.adb, self.port = http, adb, port
        self.auth = httpx.BasicAuth(user, password) if password else None
        self._id = 0
        self._settings = {}          # what prefer() last set, so unchanged settings aren't sent again

    @property
    def url(self) -> str:
        if not self.adb.host:
            self.adb.run(["true"])          # connects and learns the TV's address
        return f"http://{self.adb.host}:{self.port}/jsonrpc"

    async def call(self, method: str, params: dict | None = None, timeout: float = 4.0):
        if not self.auth:
            raise KodiError("Kodi password not set (tv-agent setup kodi)")
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        url = await asyncio.to_thread(lambda: self.url)
        r = await self.http.post(url, json=body, auth=self.auth, timeout=timeout)
        if r.status_code == 401:
            raise KodiError("Kodi refused the user name or password")
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise KodiError(f"{method}: {data['error'].get('message')}")
        return data.get("result")

    async def up(self) -> bool:
        try:
            return await self.call("JSONRPC.Ping", timeout=1.0) == "pong"
        except (httpx.HTTPError, KodiError, OSError):
            return False

    async def start(self, wait_s: float = 20.0):
        """Bring Kodi to the front (cold-starting it if needed) and wait until its API answers."""
        # As the home screen launches it: Kodi's splash screen needs MAIN/LEANBACK_LAUNCHER, and with a bare
        # `am start -n` its process starts but no window (or API) ever appears.
        await asyncio.to_thread(open_app, self.adb, APPS["kodi"])
        deadline = asyncio.get_running_loop().time() + wait_s
        while asyncio.get_running_loop().time() < deadline:
            if await self.up():
                return
            await asyncio.sleep(0.25)
        raise KodiError("Kodi didn't start (is 'Allow remote control via HTTP' on?)")

    async def player(self) -> int | None:
        for p in await self.call("Player.GetActivePlayers") or []:
            if p.get("type") == "video":
                return p["playerid"]
        return None

    async def position(self, pid: int) -> tuple[int, int, float]:
        """(time ms, total ms, speed) of the active player."""
        r = await self.call("Player.GetProperties", {"playerid": pid, "properties": ["time", "totaltime", "speed"]})
        return _ms(r["time"]), _ms(r["totaltime"]), r.get("speed", 0)

    async def open(self, url: str, resume_ms: int = 0):
        options = {"resume": _time(resume_ms)} if resume_ms > 0 else {}
        await self.call("Player.Open", {"item": {"file": url}, "options": options}, timeout=15)

    async def play(self, pid: int, on: bool | str):
        """on: True plays, False pauses, "toggle" switches."""
        await self.call("Player.PlayPause", {"playerid": pid, "play": on})

    async def stop(self, pid: int):
        await self.call("Player.Stop", {"playerid": pid})

    async def seek_by(self, pid: int, seconds: int):
        await self.call("Player.Seek", {"playerid": pid, "value": {"seconds": seconds}})

    async def seek_percent(self, pid: int, percent: float):
        await self.call("Player.Seek", {"playerid": pid, "value": {"percentage": float(percent)}})

    async def seek_to(self, pid: int, ms: int):
        await self.call("Player.Seek", {"playerid": pid, "value": {"time": _time(ms)}})

    async def info(self, *labels: str) -> dict:
        """Kodi's info labels, e.g. "Player.ChapterName"."""
        return await self.call("XBMC.GetInfoLabels", {"labels": list(labels)}) or {}

    async def action(self, name: str):
        """One of Kodi's built-in actions, e.g. "chapterorbigstepforward"."""
        await self.call("Input.ExecuteAction", {"action": name})

    async def tracks(self, pid: int) -> dict:
        return await self.call("Player.GetProperties", {"playerid": pid, "properties": [
            "audiostreams", "currentaudiostream", "subtitles", "currentsubtitle", "subtitleenabled"]})

    async def set_audio(self, pid: int, index: int):
        await self.call("Player.SetAudioStream", {"playerid": pid, "stream": index})

    async def set_subtitle(self, pid: int, index: int | None):
        """Show subtitle track `index`, or hide subtitles with None."""
        if index is None:
            await self.call("Player.SetSubtitle", {"playerid": pid, "subtitle": "off"})
            return
        await self.call("Player.SetSubtitle", {"playerid": pid, "subtitle": index, "enable": True})
        await self.call("Player.SetSubtitle", {"playerid": pid, "subtitle": "on"})

    async def prefer(self, audio: str, subtitles: str):
        """Kodi's own language choice for the next file (names as in its settings: "English", "Spanish"…, or
        "mediadefault" / "none"), with the file's "default" flags ignored, so it starts on the right tracks."""
        wanted = {"locale.audiolanguage": audio, "locale.subtitlelanguage": subtitles,
                  "videoplayer.preferdefaultflag": False}
        for key, value in wanted.items():
            if self._settings.get(key) != value:
                await self.call("Settings.SetSettingValue", {"setting": key, "value": value})
                self._settings[key] = value

    async def add_subtitle(self, pid: int, url: str):
        """Load a subtitle file by link (Kodi detects the format from its content); it becomes the shown one."""
        await self.call("Player.AddSubtitle", {"playerid": pid, "subtitle": url}, timeout=15)

    async def notify(self, title: str, message: str):
        await self.call("GUI.ShowNotification", {"title": title, "message": message, "displaytime": 4000})
