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
from .library import CACHE_DIR, MIDI_DIR, Library, file_result
from .pipeline import Options, convert_file


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="piano2midi",
        description="Transcribe solo piano recordings (mp3/m4a/wav/flac) into performance MIDI with sustain pedal.",
    )
    p.add_argument("inputs", nargs="*", help="audio files and/or folders of audio files")
    p.add_argument("--csv", metavar="TASKS", help="video URL task CSV (updated in place)")
    p.add_argument("-o", "--output", default=None,
                   help="write MIDI and work files to this folder instead of the library "
                        "(default: output/MIDI Files, work files in output/.cache)")
    p.add_argument("--overwrite", action="store_true",
                   help="convert again and replace MIDI files that already exist (default: skip them)")
    p.add_argument("--legato", action="store_true",
                   help="close short gaps (<=100 ms) in the top voice; for cantabile/legato pieces, off by default")
    p.add_argument("--keep-soft-pedal", action="store_true", help="keep predicted una corda (CC67) events")
    p.add_argument("--pedal-ramps", action="store_true", help="replace instant pedal jumps with ~50 ms ramps")
    p.add_argument("--device", default="auto", help="auto (default), cpu, cuda or mps")
    p.add_argument("--threads", type=int, default=None, help="CPU threads for the model")
    args = p.parse_args(argv)

    if args.csv and args.inputs:
        p.error("use either --csv TASKS or local audio inputs, not both")
    if not args.csv and not args.inputs:
        p.error("provide audio inputs or --csv TASKS")
    use_library = args.output is None
    if use_library:
        args.output = str(CACHE_DIR)
    if args.csv:
        from .batch import read_tasks, run_csv

        csv_path = Path(args.csv).expanduser()
        try:
            before = {r["url"]: r["status"] for r in read_tasks(csv_path)[1]}
        except Exception:  # run_csv reports CSV problems itself
            before = {}
        code = run_csv(csv_path, args.output,
                       Options(args.legato, args.keep_soft_pedal, args.pedal_ramps),
                       overwrite=args.overwrite, device=args.device, threads=args.threads)
        if use_library and csv_path.is_file():
            lib, filed = Library(), 0
            for row in read_tasks(csv_path)[1]:
                produced = (csv_path.parent / row["midi_path"]).resolve()
                if row["status"] != "done" or not produced.is_file():
                    continue
                known = lib.by_source(row["url"])
                if args.overwrite or before.get(row["url"]) != "done" or not (known and known["file_exists"]):
                    file_result(lib, produced, row["name"] or produced.stem, source_type="url",
                                source_url=row["url"])
                    filed += 1
            print(f"Library: {filed} MIDI file(s) saved to {MIDI_DIR.resolve()}")
        return code

    try:
        files = find_audio_files(args.inputs)
    except FileNotFoundError as exc:
        print(f"Not found: {exc}", file=sys.stderr)
        return 2
    if not files:
        print("No audio files found (mp3, m4a, wav, flac, aac, ogg, aiff).", file=sys.stderr)
        return 2

    options = Options(legato=args.legato, keep_soft_pedal=args.keep_soft_pedal,
                      pedal_ramps=args.pedal_ramps)
    done, failed, skipped = [], [], []
    todo = []
    lib = Library() if use_library else None
    for f in files:
        if lib is not None:
            known = lib.by_source(str(f.resolve()))
            exists = bool(known and known["file_exists"])
        else:
            exists = (Path(args.output) / f"{f.stem}.mid").exists()
        if not args.overwrite and exists:
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
            info = convert_file(engine, f, args.output, options, overwrite=args.overwrite or use_library)
            midi = Path(info["files"]["midi"])
            if lib is not None:
                midi = Path(file_result(lib, midi, f.stem, source_type="local",
                                        source_url=str(f.resolve()))["midi_path"])
            o = info["output"]
            print(f"ok in {time.time() - t0:.0f}s — {o['notes']} notes, velocity {o['velocity_min']}–"
                  f"{o['velocity_max']}, {o['sustain_pedal_presses']} pedal presses → {midi.name}",
                  flush=True)
            done.append(f)
        except Exception as exc:  # keep going with the rest of the batch
            print(f"FAILED: {exc}", flush=True)
            log = Path(args.output) / "extras" / f"{f.stem}.error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            failed.append(f)

    print(f"\nDone: {len(done)} converted, {len(failed)} failed, {len(skipped)} skipped. "
          f"MIDI files are in {(MIDI_DIR if use_library else Path(args.output)).resolve()}")
    for f in failed:
        print(f"  failed: {f.name} (see {Path(args.output) / 'extras' / f'{f.stem}.error.log'})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
