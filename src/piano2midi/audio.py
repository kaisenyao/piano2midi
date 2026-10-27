"""Decode any common audio file to float32 PCM with ffmpeg (no re-encoding to disk)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".aif", ".aiff"}
MAX_AUDIO_S = 60 * 60  # a single piano piece; anything longer is a compilation, and would fill the memory


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # bundled binary, so users do not need Homebrew
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on the machine
        raise RuntimeError("ffmpeg not found. Install it (brew install ffmpeg) or `pip install imageio-ffmpeg`.") from exc


def load_audio(path: str | Path, sample_rate: int, channels: int = 2) -> np.ndarray:
    """Return audio as float32 array shaped (samples, channels) at `sample_rate`."""
    cmd = [  # decoding stops just past the limit, so a long file is refused without being read whole
        ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-vn", "-ac", str(channels), "-ar", str(sample_rate), "-t", str(MAX_AUDIO_S + 1),
        "-f", "f32le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"could not decode audio: {proc.stderr.decode(errors='replace').strip()}")
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("decoded audio is empty")
    if audio.size > MAX_AUDIO_S * sample_rate * channels:
        raise RuntimeError(f"the recording is longer than {MAX_AUDIO_S // 60} minutes")
    return audio.reshape(-1, channels).copy()


def find_audio_files(paths: list[str | Path]) -> list[Path]:
    """Expand files and folders into a sorted list of audio files."""
    found: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            found += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                            and not f.name.startswith("."))
        elif p.is_file():
            found.append(p)
        else:
            raise FileNotFoundError(p)
    return found
