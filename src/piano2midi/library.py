"""Local MIDI library: a small SQLite index of every MIDI file piano2midi knows about.

    python -m piano2midi.library import ~/Desktop/midi      # copy MIDI files in and index them
    python -m piano2midi.library list
    python -m piano2midi.library enrich                     # let the chat model add composers, aliases, tags
"""

from __future__ import annotations

import argparse
import filecmp
import re
import shutil
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pretty_midi

OUTPUT_DIR = Path("output")
MIDI_DIR = OUTPUT_DIR / "MIDI Files"          # the MIDI files you play
PLAYLIST = MIDI_DIR / "playlist.m3u"
DB_PATH = OUTPUT_DIR / "library.db"
CACHE_DIR = OUTPUT_DIR / ".cache"              # downloads, raw MIDI, statistics, task state

FIELDS = ("title", "composer", "performer", "aliases", "tags")
SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    composer TEXT NOT NULL DEFAULT '',
    performer TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'local',
    source_url TEXT NOT NULL DEFAULT '',
    midi_path TEXT NOT NULL UNIQUE,
    notes INTEGER,
    velocity_min INTEGER,
    velocity_max INTEGER,
    pedal_presses INTEGER,
    duration_s REAL,
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recordings_source_url ON recordings(source_url);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    asked_at TEXT NOT NULL,
    text TEXT NOT NULL,
    recording_id INTEGER REFERENCES recordings(id)
);
"""
STOPWORDS = {"the", "a", "an", "of", "in", "and", "by", "for", "piano", "solo", "version", "play", "me"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\W_]+", " ", text).strip()


def _terms(text: str) -> set[str]:
    norm = _norm(text)
    terms = {w for w in norm.split() if w not in STOPWORDS}
    for run in re.findall(r"[\u3400-\u9fff]+", norm):  # Chinese has no spaces: use characters and pairs
        terms.update(run)
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def midi_stats(path: Path) -> dict:
    midi = pretty_midi.PrettyMIDI(str(path))
    notes = [n for inst in midi.instruments for n in inst.notes]
    pedal = sorted((c for inst in midi.instruments for c in inst.control_changes if c.number == 64),
                   key=lambda c: c.time)
    presses, down = 0, False
    for c in pedal:
        if c.value >= 64 and not down:
            presses += 1
        down = c.value >= 64
    velocities = [n.velocity for n in notes]
    return {
        "notes": len(notes),
        "velocity_min": min(velocities) if velocities else None,
        "velocity_max": max(velocities) if velocities else None,
        "pedal_presses": presses,
        "duration_s": round(midi.get_end_time(), 1),
    }


def place_midi(src: Path, title: str, midi_dir: Path = MIDI_DIR) -> Path:
    """Copy a MIDI file into the MIDI folder as '<title>.mid' without overwriting a different file."""
    midi_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(" .") or "Untitled"
    dest = midi_dir / f"{safe}.mid"
    n = 2
    while dest.exists() and not filecmp.cmp(src, dest, shallow=False):
        dest = midi_dir / f"{safe} ({n}).mid"
        n += 1
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest


def file_result(lib: "Library", produced: Path, title: str, *, source_type: str, source_url: str,
                composer: str = "", performer: str = "", tags: str = "") -> dict:
    """Put a freshly transcribed MIDI into the MIDI folder and the library.

    A new transcription of a source that is already in the library replaces that file in place.
    """
    known = lib.by_source(source_url)
    if known and known["file_exists"]:
        if Path(produced).resolve() != Path(known["midi_path"]):
            shutil.copy2(produced, known["midi_path"])
        return lib.add(Path(known["midi_path"]), known["title"], composer=composer, performer=performer,
                       tags=tags, source_type=source_type, source_url=source_url)
    return lib.add(place_midi(Path(produced), title), title, composer=composer, performer=performer,
                   tags=tags, source_type=source_type, source_url=source_url)


class Library:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]

    def get(self, rec_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM recordings WHERE id = ?", (rec_id,)).fetchone()
        return self._public(row) if row else None

    def by_source(self, source_url: str) -> dict | None:
        row = self.db.execute("SELECT * FROM recordings WHERE source_url = ? ORDER BY id DESC",
                              (source_url,)).fetchone()
        return self._public(row) if row else None

    def all(self) -> list[dict]:
        return [self._public(r) for r in self.db.execute("SELECT * FROM recordings ORDER BY title")]

    def add(self, midi_path: Path, title: str, *, composer: str = "", performer: str = "",
            aliases: str = "", tags: str = "", source_type: str = "local", source_url: str = "") -> dict:
        midi_path = Path(midi_path).resolve()
        stats = midi_stats(midi_path)
        self.db.execute(
            """INSERT INTO recordings (title, composer, performer, aliases, tags, source_type, source_url,
                   midi_path, notes, velocity_min, velocity_max, pedal_presses, duration_s, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(midi_path) DO UPDATE SET title = excluded.title,
                   composer = COALESCE(NULLIF(excluded.composer, ''), composer),
                   performer = COALESCE(NULLIF(excluded.performer, ''), performer),
                   aliases = COALESCE(NULLIF(excluded.aliases, ''), aliases),
                   tags = COALESCE(NULLIF(excluded.tags, ''), tags),
                   source_type = excluded.source_type,
                   source_url = COALESCE(NULLIF(excluded.source_url, ''), source_url),
                   notes = excluded.notes, velocity_min = excluded.velocity_min,
                   velocity_max = excluded.velocity_max, pedal_presses = excluded.pedal_presses,
                   duration_s = excluded.duration_s""",
            (title, composer, performer, aliases, tags, source_type, source_url, str(midi_path),
             stats["notes"], stats["velocity_min"], stats["velocity_max"], stats["pedal_presses"],
             stats["duration_s"], _now()))
        self.db.commit()
        self.write_playlist()
        row = self.db.execute("SELECT * FROM recordings WHERE midi_path = ?", (str(midi_path),)).fetchone()
        return self._public(row)

    def update(self, rec_id: int, **fields) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in FIELDS and v is not None}
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE recordings SET {assignments} WHERE id = ?", (*fields.values(), rec_id))
            self.db.commit()
            self.write_playlist()
        return self.get(rec_id)

    def playlist(self) -> list[dict]:
        """Every recording whose file exists, in the order it was added."""
        rows = (self._public(r) for r in self.db.execute("SELECT * FROM recordings ORDER BY id"))
        return [r for r in rows if r["file_exists"]]

    def write_playlist(self, path: Path = PLAYLIST) -> None:
        """Rewrite the .m3u playlist from playlist()."""
        path.parent.mkdir(parents=True, exist_ok=True)
        folder = path.parent.resolve()
        lines = ["#EXTM3U"]
        for r in self.playlist():
            midi = Path(r["midi_path"])
            entry = midi.name if midi.parent == folder else str(midi)
            lines += [f'#EXTINF:{round(r["duration_s"] or 0)},{r["title"]}', entry]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def search(self, query: str, limit: int = 8) -> list[dict]:
        wanted = _terms(query)
        if not wanted:
            return []
        query_norm = _norm(query)
        scored = []
        for row in self.db.execute("SELECT * FROM recordings"):
            hay_text = " ".join(row[f] for f in FIELDS)
            have = _terms(hay_text)
            score = sum(2.0 if len(t) > 1 else 0.5 for t in wanted & have)
            if query_norm and query_norm in _norm(hay_text):
                score += 3
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda s: -s[0])
        return [self._public(r) | {"score": round(s, 1)} for s, r in scored[:limit]]

    def log_request(self, text: str, rec_id: int | None) -> None:
        self.db.execute("INSERT INTO requests (asked_at, text, recording_id) VALUES (?, ?, ?)",
                        (_now(), text, rec_id))
        self.db.commit()

    def import_folder(self, folder: Path, *, copy: bool = True) -> list[dict]:
        added = []
        for src in sorted(Path(folder).expanduser().rglob("*")):
            if src.suffix.lower() not in {".mid", ".midi"} or src.name.startswith("."):
                continue
            if src.name.endswith(".raw.mid") or "extras" in src.parts:  # piano2midi side files
                continue
            try:
                path = place_midi(src, src.stem) if copy else src
                added.append(self.add(path, src.stem, source_type="import", source_url=str(src.resolve())))
            except Exception as exc:  # skip unreadable files, keep importing
                print(f"  skipped {src.name}: {exc}", file=sys.stderr)
        return added

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["file_exists"] = Path(d["midi_path"]).is_file()
        d.pop("added_at", None)
        return d


def enrich(lib: Library, complete_json) -> int:
    """Ask the chat model to fill in composer, aliases (other-language titles) and tags."""
    todo = [r for r in lib.all() if not r["composer"] or not r["aliases"]]
    changed = 0
    for i in range(0, len(todo), 30):
        batch = todo[i:i + 30]
        listing = "\n".join(f'{r["id"]}: {r["title"]}' for r in batch)
        prompt = (
            "These are titles of piano recordings (MIDI files). For each, return a JSON object mapping the id "
            "to {\"composer\": str, \"aliases\": str, \"tags\": str}. aliases: other common names of the piece, "
            "including English and Chinese names, separated by '; '. tags: 2-4 short English words about genre "
            "and mood, separated by ', '. Use an empty string when unsure; do not guess composers you do not know."
            "\nReturn only the JSON object.\n\n" + listing)
        data = complete_json(prompt)
        for key, info in (data or {}).items():
            if not str(key).isdigit() or not isinstance(info, dict):
                continue
            rec = lib.get(int(key))
            if rec is None:
                continue
            lib.update(int(key), composer=rec["composer"] or info.get("composer", ""),
                       aliases=rec["aliases"] or info.get("aliases", ""),
                       tags=rec["tags"] or info.get("tags", ""))
            changed += 1
    return changed


def format_row(r: dict) -> str:
    who = " · ".join(x for x in (r["composer"], r["performer"]) if x)
    missing = "" if r["file_exists"] else "  [file missing]"
    return f'{r["id"]:>4}  {r["title"]}{"  (" + who + ")" if who else ""}  — {r["notes"]} notes{missing}'


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="piano2midi-library", description="Manage the local MIDI library.")
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="add MIDI files from a folder (copied into output/MIDI Files)")
    imp.add_argument("folder")
    imp.add_argument("--in-place", action="store_true", help="index the files where they are instead of copying")
    sub.add_parser("list", help="list the library")
    sub.add_parser("enrich", help="use the chat model to add composers, aliases and tags")
    args = p.parse_args(argv)

    lib = Library()
    if args.cmd == "import":
        added = lib.import_folder(Path(args.folder), copy=not args.in_place)
        print(f"Imported {len(added)} MIDI file(s). Library now has {lib.count()} recording(s).")
    elif args.cmd == "list":
        rows = lib.all()
        for r in rows:
            print(format_row(r))
        print(f"{len(rows)} recording(s) in {DB_PATH}")
    elif args.cmd == "enrich":
        from .llm import complete_json

        print(f"Updated {enrich(lib, complete_json)} recording(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
