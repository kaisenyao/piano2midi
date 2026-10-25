"""Single-process CSV task runner. Paths in the CSV are relative to its folder."""
from __future__ import annotations

import csv
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

import pretty_midi

from .download import download_audio, task_id, valid_audio
from .pipeline import Options, convert_file

COLUMNS = ["url", "name", "status", "audio_path", "midi_path", "error"]


def read_tasks(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if "url" not in fields or len(set(fields)) != len(fields) or any(not f for f in fields):
            raise ValueError("CSV needs a url header and unique, nonempty column names")
        rows = []
        for row in reader:
            if None in row:
                raise ValueError(f"CSV line {reader.line_num}: more values than headers")
            if not any(v and v.strip() for v in row.values()):
                continue
            rows.append({k: (v or "") for k, v in row.items()})
    fields += [col for col in COLUMNS if col not in fields]
    for row in rows:
        for col in COLUMNS:
            row.setdefault(col, "")
        row["url"] = row["url"].strip()
    return fields, rows


def save_tasks(path: Path, fields: list[str], rows: list[dict]) -> None:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                         dir=path.parent, prefix=f".{path.name}-",
                                         delete=False) as stream:
            tmp = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def valid_midi(path: Path) -> bool:
    if not path.is_file() or not path.stat().st_size:
        return False
    try:
        pretty_midi.PrettyMIDI(str(path))
        return True
    except Exception:
        return False


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
    # Keep UTF-8 filenames below common filesystem component limits.
    name = name.encode("utf-8")[:160].decode("utf-8", errors="ignore").rstrip(" .")
    return name or "Untitled"


def run_csv(csv_path, out_dir, options: Options, *, overwrite=False,
            device="auto", threads=None) -> int:
    path = Path(csv_path).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    engine = None
    done = failed = skipped = 0
    stage = "CSV"
    try:
        fields, rows = read_tasks(path)
        # Check persistence before doing any downloads.
        save_tasks(path, fields, rows)
        owners: dict[Path, str] = {}
        assigned: dict[str, Path] = {}
        for row in rows:
            if row["midi_path"]:
                target = (path.parent / row["midi_path"]).resolve()
                if target in owners and owners[target] != row["url"]:
                    raise ValueError("different URLs claim the same midi_path; clear one midi_path")
                owners[target] = row["url"]
                assigned.setdefault(row["url"], target)
        completed: dict[str, dict] = {}
        for i, row in enumerate(rows, 1):
            url = row["url"]
            print(f"[{i}/{len(rows)}] {row['name'] or url or '(missing URL)'}", flush=True)
            if url in completed:
                for col in ("name", "status", "audio_path", "midi_path", "error"):
                    row[col] = completed[url][col]
                if row["status"] == "failed":
                    failed += 1
                else:
                    skipped += 1
                save_tasks(path, fields, rows)
                continue
            target = assigned.get(url)
            if target and not overwrite and valid_midi(target):
                row.update(status="done", midi_path=os.path.relpath(target, path.parent), error="")
                completed[url] = row.copy()
                skipped += 1
                save_tasks(path, fields, rows)
                print("  already completed; skipped", flush=True)
                continue
            row.update(status="processing", error="")
            save_tasks(path, fields, rows)
            try:
                stage = "download"
                audio = (path.parent / row["audio_path"]).resolve() if row["audio_path"] else None
                if audio is None or not valid_audio(audio):
                    audio, title = download_audio(url, out / "audio")
                else:
                    title = audio.stem
                row["audio_path"] = os.path.relpath(audio, path.parent)
                row["name"] = row["name"].strip() or title
            except Exception as exc:
                row.update(status="failed", error=f"{stage}: {exc}"[:1000])
                completed[url] = row.copy()
                failed += 1
                save_tasks(path, fields, rows)
                print(f"  FAILED: {row['error']}", flush=True)
                continue
            # Persist the audio checkpoint before attempting model loading.
            if target is None:
                stem = safe_name(row["name"])
                target = out / f"{stem}.mid"
                if target.exists() or target in owners:
                    target = out / f"{stem}-{task_id(url)}.mid"
                if target.exists() or target in owners:
                    raise ValueError(f"output path already occupied: {target}")
                assigned[url] = target
                owners[target] = url
            row["midi_path"] = os.path.relpath(target, path.parent)
            save_tasks(path, fields, rows)
            try:
                stage = "transcribe"
                if engine is None:
                    print("Loading Transkun model…", flush=True)
                    from .engine import TranskunEngine
                    engine = TranskunEngine(device=device, threads=threads)
                convert_file(engine, audio, target.parent, options, overwrite=True,
                             output_name=target.stem)
                if not valid_midi(target):
                    raise RuntimeError("output MIDI is missing or invalid")
                row.update(status="done", error="")
                completed[url] = row.copy()
                done += 1
                print(f"  done → {target.name}", flush=True)
            except Exception as exc:
                row.update(status="failed", error=f"{stage}: {exc}"[:1000])
                completed[url] = row.copy()
                failed += 1
                log = out / "extras" / f"{task_id(url)}.error.log"
                try:
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text(traceback.format_exc(), encoding="utf-8")
                except OSError:
                    pass
                print(f"  FAILED: {row['error']}", flush=True)
            save_tasks(path, fields, rows)
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command to resume.", file=sys.stderr)
        return 130
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Task list error (stopped): {exc}", file=sys.stderr)
        return 2
    print(f"Done: {done} converted, {failed} failed, {skipped} skipped. MIDI: {out}")
    return 1 if failed else 0
