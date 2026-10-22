"""Write Kodi's playback position back to your Stremio library, so Continue Watching in Stremio (on the TV, the
phone, anywhere) shows where you stopped. Runs only while something the agent started is playing in Kodi.

Each write is read-modify-write of one library item with a new _mtime, the way stremio-core syncs. At 90% the
item counts as watched (offset back to 0, timesWatched + 1). The per-episode `watched` bitfield is left alone.
"""
import asyncio
import logging
import time

from ..state import NowPlaying
from .account import Account, now_iso
from .library import Library

log = logging.getLogger(__name__)
WATCHED_AT = 0.9


def updated(item: dict | None, np: NowPlaying, offset_ms: int, duration_ms: int, watched_ms: int, meta: dict) -> dict:
    """The library item after watching np up to offset_ms (a new temp item if it wasn't in the library)."""
    now = now_iso()
    if item is None:
        item = {"_id": np.imdb_id, "name": np.name, "type": np.kind, "poster": meta.get("poster"),
                "posterShape": "poster", "removed": True, "temp": True, "_ctime": now,
                "behaviorHints": {}, "state": {}}
    state = dict(item.get("state") or {})
    done = duration_ms > 0 and offset_ms >= WATCHED_AT * duration_ms
    if state.get("video_id") != np.video_id:
        state["timeWatched"] = 0
    state.update({
        "video_id": np.video_id,
        "lastWatched": now,
        "duration": duration_ms,
        "timeOffset": 0 if done else offset_ms,
        "timeWatched": state.get("timeWatched", 0) + watched_ms,
        "overallTimeWatched": state.get("overallTimeWatched", 0) + watched_ms,
    })
    if done:
        state["timesWatched"] = state.get("timesWatched", 0) + 1
        state["flaggedWatched"] = 1
    return {**item, "state": state, "_mtime": now}


async def track(poll, account: Account, library: Library, np: NowPlaying, meta: dict, every_s: float = 10,
                write_every_s: float = 120) -> tuple[str, int | None, int]:
    """Follow the player until it stops; write on pause, every 2 minutes, and at the end.
    poll() returns (position ms, duration ms, speed) while something plays, None once it has stopped.
    Returns how it ended ("stopped", or "lost" when the player stopped answering), the last position and the
    duration; the last 45 s are watched every 2 s, so a stop at the very end can be told from an earlier one."""
    last_write = time.monotonic()
    last_pos, unwritten, paused, misses = None, 0, False, 0
    duration = 0
    how = "stopped"

    async def write():
        nonlocal unwritten, last_write
        if last_pos is None:
            return
        item = await library.get(np.imdb_id)
        await account.put([updated(item, np, last_pos, duration, unwritten, meta)])
        library.invalidate()
        unwritten, last_write = 0, time.monotonic()
        log.info("progress saved: %s at %d s of %d s", np.video_id, last_pos // 1000, duration // 1000)

    try:
        while True:
            near_end = last_pos is not None and duration and duration - last_pos < 45_000
            await asyncio.sleep(2 if near_end else every_s)
            try:
                now = await poll()
                if now is None:
                    break
                pos, duration, speed = now
                misses = 0
            except Exception as e:
                misses += 1
                if misses >= 3:
                    log.warning("progress: the player stopped answering (%s)", type(e).__name__)
                    how = "lost"
                    break
                continue
            if last_pos is not None and speed:
                unwritten += max(0, pos - last_pos)
            last_pos = pos
            if (speed == 0) != paused or time.monotonic() - last_write > write_every_s:
                paused = speed == 0
                await write()
    finally:
        try:
            await write()
        except Exception as e:
            log.warning("progress: final write failed (%s)", type(e).__name__)
    return how, last_pos, duration
