"""Turn a spoken YouTube request into one video id with yt-dlp's search (no API key), for TizenTube to open."""
import re

import yt_dlp

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def first_video(query: str) -> tuple[str, str] | None:
    """(video id, title) of the top search result, or None."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    for entry in (info or {}).get("entries") or []:
        vid = entry.get("id", "")
        if VIDEO_ID.match(vid):
            return vid, entry.get("title") or query
    return None
