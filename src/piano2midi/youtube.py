"""YouTube search through the official YouTube Data API v3 (needs YOUTUBE_API_KEY)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/youtube/v3"

# Hints for the chat model; a hit is a warning sign, not an automatic rejection.
TITLE_FLAGS = {
    "synthesia": "falling-notes (Synthesia) video",
    "midi": "mentions MIDI",
    "tutorial": "tutorial",
    "how to play": "tutorial",
    "lesson": "lesson",
    "easy": "easy arrangement",
    "slow": "slowed or practice version",
    "beginner": "beginner arrangement",
    "piano tiles": "game",
    "compilation": "compilation",
    "playlist": "compilation",
    "1 hour": "long compilation",
    "hours": "long compilation",
    "8 bit": "not acoustic piano",
    "8-bit": "not acoustic piano",
    "remix": "remix",
}


class YouTubeError(RuntimeError):
    pass


def api_key() -> str | None:
    return os.environ.get("YOUTUBE_API_KEY") or None


def _get(endpoint: str, params: dict, key: str) -> dict:
    url = f"{API}/{endpoint}?{urllib.parse.urlencode({**params, 'key': key})}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc)["error"]["message"]
        except Exception:
            message = str(exc)
        raise YouTubeError(f"YouTube API error {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise YouTubeError(f"cannot reach YouTube API: {exc.reason}") from None


def parse_duration(iso: str) -> int:
    """'PT1H2M3S' -> 3723 seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def flags_for(title: str, channel: str, description: str, seconds: int) -> list[str]:
    text = f"{title} {channel}".casefold()
    flags = sorted({label for word, label in TITLE_FLAGS.items() if word in text})
    if "synthesia" in description.casefold() and "falling-notes (Synthesia) video" not in flags:
        flags.append("description mentions Synthesia")
    if seconds and seconds < 60:
        flags.append("very short")
    elif seconds > 20 * 60:
        flags.append("over 20 minutes (compilation or multi-movement)")
    return flags


def search(query: str, max_results: int = 8, key: str | None = None) -> list[dict]:
    key = key or api_key()
    if not key:
        raise YouTubeError("YOUTUBE_API_KEY is not set")
    max_results = max(1, min(int(max_results), 15))
    found = _get("search", {"part": "snippet", "type": "video", "q": query, "maxResults": max_results}, key)
    ids = [it["id"]["videoId"] for it in found.get("items", []) if it.get("id", {}).get("videoId")]
    if not ids:
        return []
    details = _get("videos", {"part": "snippet,contentDetails,statistics", "id": ",".join(ids)}, key)
    by_id = {v["id"]: v for v in details.get("items", [])}
    results = []
    for vid in ids:  # keep YouTube's relevance order
        v = by_id.get(vid)
        if not v:
            continue
        sn, cd, st = v.get("snippet", {}), v.get("contentDetails", {}), v.get("statistics", {})
        seconds = parse_duration(cd.get("duration", ""))
        description = (sn.get("description") or "").strip()
        results.append({
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "duration_s": seconds,
            "views": int(st["viewCount"]) if st.get("viewCount", "").isdigit() else None,
            "published": (sn.get("publishedAt") or "")[:10],
            "description": description[:300],
            "flags": flags_for(sn.get("title", ""), sn.get("channelTitle", ""), description, seconds),
        })
    return results
