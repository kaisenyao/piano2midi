"""Audio file -> raw MIDI -> cleaned MIDI + info.json."""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from pathlib import Path

import pretty_midi

from .audio import load_audio
from .cleanup import clean

EXTRAS_DIR = "extras"


@dataclass
class Options:
    legato: bool = True
    keep_soft_pedal: bool = False
    pedal_ramps: bool = False


def summarize(midi: pretty_midi.PrettyMIDI) -> dict:
    notes = [n for i in midi.instruments for n in i.notes]
    ccs = [c for i in midi.instruments for c in i.control_changes]
    sustain = sorted((c for c in ccs if c.number == 64), key=lambda c: c.time)
    presses, down = 0, False
    for c in sustain:
        if c.value >= 64 and not down:
            presses += 1
        down = c.value >= 64
    vel = [n.velocity for n in notes]
    return {
        "notes": len(notes),
        "velocity_min": min(vel) if vel else None,
        "velocity_max": max(vel) if vel else None,
        "distinct_velocities": len(set(vel)),
        "pitch_range": [min(n.pitch for n in notes), max(n.pitch for n in notes)] if notes else None,
        "sustain_pedal_presses": presses,
        "sustain_pedal_released_at_end": (not sustain) or sustain[-1].value < 64,
        "soft_pedal_events": sum(1 for c in ccs if c.number == 67),
        "end_time_s": round(midi.get_end_time(), 3),
    }


def transcribe_audio(engine, audio_path, options: Options):
    """Run the model and clean-up. Returns (raw_midi, clean_midi, info)."""
    t0 = time.time()
    audio = load_audio(audio_path, engine.sample_rate)
    t1 = time.time()
    raw = engine.transcribe(audio)
    t2 = time.time()
    cleaned, report = clean(raw, keep_soft_pedal=options.keep_soft_pedal,
                            legato=options.legato, pedal_ramps=options.pedal_ramps)
    info = {
        "source": Path(audio_path).name,
        "engine": engine.version,
        "device": engine.device,
        "audio_duration_s": round(len(audio) / engine.sample_rate, 3),
        "transcribe_seconds": round(t2 - t1, 1),
        "total_seconds": round(time.time() - t0, 1),
        "raw": summarize(raw),
        "output": summarize(cleaned),
        "cleanup": report.as_dict(),
        "options": vars(options),
    }
    return raw, cleaned, info


def unique_path(path: Path, overwrite: bool) -> Path:
    if overwrite or not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many existing copies of {path}")


def convert_file(engine, audio_path, out_dir, options: Options, overwrite: bool = False) -> dict:
    audio_path, out_dir = Path(audio_path), Path(out_dir)
    raw, cleaned, info = transcribe_audio(engine, audio_path, options)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / EXTRAS_DIR).mkdir(exist_ok=True)
    midi_path = unique_path(out_dir / f"{audio_path.stem}.mid", overwrite)
    stem = midi_path.stem
    raw_path = out_dir / EXTRAS_DIR / f"{stem}.raw.mid"
    info_path = out_dir / EXTRAS_DIR / f"{stem}.json"
    cleaned.write(str(midi_path))
    raw.write(str(raw_path))
    pretty_midi.PrettyMIDI(str(midi_path))  # re-read: fail loudly if the file is not valid
    info["files"] = {"midi": str(midi_path), "raw_midi": str(raw_path), "info": str(info_path)}
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def midi_bytes(midi: pretty_midi.PrettyMIDI) -> bytes:
    buf = io.BytesIO()
    midi.write(buf)
    return buf.getvalue()
