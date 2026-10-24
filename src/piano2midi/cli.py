"""Command line: piano2midi INPUT... [-o OUTPUT_DIR]"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

from .audio import find_audio_files
from .pipeline import Options, convert_file


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="piano2midi",
        description="Transcribe solo piano recordings (mp3/m4a/wav/flac) into performance MIDI with sustain pedal.",
    )
    p.add_argument("inputs", nargs="+", help="audio files and/or folders of audio files")
    p.add_argument("-o", "--output", default="output", help="output folder (default: ./output)")
    p.add_argument("--overwrite", action="store_true",
                   help="convert again and replace MIDI files that already exist (default: skip them)")
    p.add_argument("--no-legato", action="store_true", help="do not close short melody gaps")
    p.add_argument("--keep-soft-pedal", action="store_true", help="keep predicted una corda (CC67) events")
    p.add_argument("--pedal-ramps", action="store_true", help="replace instant pedal jumps with ~50 ms ramps")
    p.add_argument("--device", default="auto", help="auto (default), cpu, cuda or mps")
    p.add_argument("--threads", type=int, default=None, help="CPU threads for the model")
    args = p.parse_args(argv)

    try:
        files = find_audio_files(args.inputs)
    except FileNotFoundError as exc:
        print(f"Not found: {exc}", file=sys.stderr)
        return 2
    if not files:
        print("No audio files found (mp3, m4a, wav, flac, aac, ogg, aiff).", file=sys.stderr)
        return 2

    options = Options(legato=not args.no_legato, keep_soft_pedal=args.keep_soft_pedal,
                      pedal_ramps=args.pedal_ramps)
    done, failed, skipped = [], [], []
    todo = []
    for f in files:
        if not args.overwrite and (Path(args.output) / f"{f.stem}.mid").exists():
            skipped.append(f)
        else:
            todo.append(f)
    if skipped:
        print(f"Skipping {len(skipped)} already converted file(s) (use --overwrite to redo).")
    if not todo:
        print("Nothing new to convert.")
        return 0

    print("Loading Transkun model…", flush=True)
    from .engine import TranskunEngine

    engine = TranskunEngine(device=args.device, threads=args.threads)
    print(f"Model ready on {engine.device}. {len(todo)} file(s) to convert.\n", flush=True)

    files = todo
    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name} … ", end="", flush=True)
        t0 = time.time()
        try:
            info = convert_file(engine, f, args.output, options, overwrite=args.overwrite)
            o = info["output"]
            print(f"ok in {time.time() - t0:.0f}s — {o['notes']} notes, velocity {o['velocity_min']}–"
                  f"{o['velocity_max']}, {o['sustain_pedal_presses']} pedal presses → {Path(info['files']['midi']).name}",
                  flush=True)
            done.append(f)
        except Exception as exc:  # keep going with the rest of the batch
            print(f"FAILED: {exc}", flush=True)
            log = Path(args.output) / "extras" / f"{f.stem}.error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            failed.append(f)

    print(f"\nDone: {len(done)} converted, {len(failed)} failed, {len(skipped)} skipped. "
          f"MIDI files are in {Path(args.output).resolve()}")
    for f in failed:
        print(f"  failed: {f.name} (see extras/{f.stem}.error.log)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
