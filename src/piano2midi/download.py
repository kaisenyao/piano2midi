"""Download one video's original audio; publish only a verified cache file."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .audio import ffmpeg_exe


def task_id(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()[:20]


def valid_audio(path: Path) -> bool:
    """Decode the whole audio without retaining PCM in memory; require samples."""
    if not path.is_file() or not path.stat().st_size:
        return False
    proc = subprocess.run(
        [ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
         "-i", str(path), "-map", "0:a:0", "-progress", "pipe:1", "-f", "null", "-"],
        capture_output=True,
    )
    return proc.returncode == 0 and any(
        line.startswith("out_time_us=") and line.partition("=")[2].isdigit()
        and int(line.partition("=")[2]) > 0
        for line in proc.stdout.decode(errors="replace").splitlines()
    )


def download_audio(url: str, cache_dir: Path) -> tuple[Path, str]:
    """Reuse a valid cache without network access, otherwise download atomically."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("expected an http(s) video URL")
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = task_id(url)
    metadata = cache_dir / f"{key}.json"
    title = ""
    if metadata.is_file():
        try:
            title = str(json.loads(metadata.read_text(encoding="utf-8")).get("title", ""))
        except (ValueError, OSError, AttributeError):
            pass
    for cached in sorted(cache_dir.glob(f"{key}.*")):
        if cached.suffix not in {".json", ".part", ".ytdl"} and valid_audio(cached):
            return cached, title or key

    from yt_dlp import YoutubeDL

    with tempfile.TemporaryDirectory(dir=cache_dir, prefix=f".{key}-") as tmp:
        options = {
            "format": "bestaudio/best",
            "outtmpl": str(Path(tmp) / "audio.%(ext)s"),
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "lazy_playlist": True,
            "playlistend": 1,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            "quiet": True,
            "noprogress": True,
            "fixup": "never",
            "ffmpeg_location": ffmpeg_exe(),
            "js_runtimes": {"deno": {}, "node": {}},
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info or info.get("_type", "video") != "video" or "entries" in info:
                raise ValueError("provide a single video URL, not a playlist or channel")
            if info.get("is_live") or info.get("live_status") == "is_upcoming":
                raise ValueError("live/upcoming streams are not supported")
            info = ydl.process_ie_result(info, download=True)
            downloaded = Path(ydl.prepare_filename(info))
        if not valid_audio(downloaded):
            raise RuntimeError("downloaded file has no valid decodable audio")
        title = str(info.get("title") or info.get("id") or key)
        # Metadata first, audio last. Only complete audio becomes visible as cache.
        meta_tmp = Path(tmp) / "metadata.json"
        meta_tmp.write_text(json.dumps({"title": title}, ensure_ascii=False), encoding="utf-8")
        os.replace(meta_tmp, metadata)
        target = cache_dir / f"{key}{downloaded.suffix}"
        os.replace(downloaded, target)
        return target, title
