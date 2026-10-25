"""Terminal chat: ask for a piece, get a performance MIDI.

    ./run.sh chat

The assistant looks in the local library first, then searches YouTube (YOUTUBE_API_KEY),
hands the chosen video to the existing CSV download-and-transcribe flow, and files the
result in output/MIDI Files and the library.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

from . import youtube
from .library import DB_PATH, MIDI_DIR, OUTPUT_DIR, Library, enrich, format_row, place_midi
from .pipeline import Options

MAX_TOOL_ROUNDS = 8
TASK_DIR = OUTPUT_DIR / "chat_tasks"

INSTRUCTIONS = f"""You are the piano2midi assistant. You help the user get performance MIDI files of piano music.
Reply in the language the user writes in. Keep replies short.

For a request to get or play a piece:
1. Call search_library first. If a recording clearly matches (same work; same performer if one was named) and
   its file exists, use it: give the title and file path. Do not transcribe it again.
2. Otherwise call search_youtube with a focused query: composer, work, catalog number, performer if named,
   and the word "piano". Choose the best candidate yourself:
   - prefer a real human performance by a named pianist, solo piano, a good recording, and a duration that
     fits the work;
   - avoid candidates whose flags or titles suggest falling-notes/Synthesia videos, MIDI renderings,
     tutorials, easy or slowed arrangements, compilations, or a different piece;
   - if the user named a performer, pick only that performer; if none match, say so and ask before using
     someone else.
   If nothing is suitable, try one refined search; if there is still nothing, explain and stop.
3. Call transcribe_youtube with the chosen URL and a clean title such as
   "Chopin - Nocturne Op.9 No.2 (Rubinstein)", plus composer, performer and a few English tags.
4. Report the chosen video (title, channel, link) and in one sentence why, one or two candidates you skipped,
   and the MIDI path with its note count and pedal presses.

Only ask a question when the request is genuinely ambiguous. If the user is vague (for example "a Chopin
nocturne"), pick a well-known piece and say which. If the user gives a local audio file path, use
transcribe_local. Use update_recording to add composer, performer, aliases (names in other languages) or tags.
Never invent file paths, links or results: report only what the tools returned.
Finished MIDI files are saved in "{MIDI_DIR}"."""

TOOLS = [
    {"name": "search_library",
     "description": "Search the local MIDI library by title, composer, performer, alias or tag (any language).",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "search_youtube",
     "description": "Search YouTube videos. Returns url, title, channel, duration_s, views, description, "
                    "warning flags, and in_library when that video was already transcribed.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"},
         "max_results": {"type": "integer", "description": "1-15, default 8"}}, "required": ["query"]}},
    {"name": "transcribe_youtube",
     "description": "Download the audio of one YouTube video through the existing piano2midi CSV flow, "
                    "transcribe it to MIDI and add it to the library. Takes a few minutes.",
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string"},
         "title": {"type": "string", "description": "clean display title used as the file name"},
         "composer": {"type": "string"}, "performer": {"type": "string"},
         "tags": {"type": "string", "description": "comma-separated English tags"}},
         "required": ["url", "title"]}},
    {"name": "transcribe_local",
     "description": "Transcribe a local audio file (mp3, m4a, wav, flac) to MIDI and add it to the library.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "title": {"type": "string"},
         "composer": {"type": "string"}, "performer": {"type": "string"}, "tags": {"type": "string"}},
         "required": ["path", "title"]}},
    {"name": "update_recording",
     "description": "Update library details of a recording by id.",
     "parameters": {"type": "object", "properties": {
         "id": {"type": "integer"}, "title": {"type": "string"}, "composer": {"type": "string"},
         "performer": {"type": "string"}, "aliases": {"type": "string"}, "tags": {"type": "string"}},
         "required": ["id"]}},
]


def _brief(rec: dict) -> dict:
    keys = ("id", "title", "composer", "performer", "aliases", "tags", "source_type", "source_url",
            "midi_path", "notes", "pedal_presses", "duration_s", "file_exists", "score")
    return {k: rec[k] for k in keys if k in rec}


class Assistant:
    def __init__(self, lib: Library):
        self.lib = lib
        self.engine = None
        self.request = ""

    # ---- tools -------------------------------------------------------------------------------
    def search_library(self, query: str) -> dict:
        print(f"  · searching the library: {query}")
        return {"results": [_brief(r) for r in self.lib.search(query)]}

    def search_youtube(self, query: str, max_results: int = 8) -> dict:
        print(f"  · searching YouTube: {query}")
        results = youtube.search(query, max_results)
        for r in results:
            known = self.lib.by_source(r["url"])
            r["in_library"] = known["id"] if known and known["file_exists"] else None
        return {"results": results}

    def transcribe_youtube(self, url: str, title: str, composer: str = "", performer: str = "",
                           tags: str = "") -> dict:
        from .batch import COLUMNS, read_tasks, run_csv, safe_name, save_tasks

        url = url.strip()
        if not re.match(r"https?://", url):
            return {"status": "failed", "error": "url must start with http:// or https://"}
        known = self.lib.by_source(url)
        if known and known["file_exists"]:
            return {"status": "already in library", **_brief(known)}
        TASK_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = TASK_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.csv"
        fields, rows = read_tasks(csv_path) if csv_path.exists() else (list(COLUMNS), [])
        if not rows:
            rows = [{c: "" for c in fields} | {"url": url, "name": safe_name(title)}]
        save_tasks(csv_path, fields, rows)
        print(f"  · downloading and transcribing \"{title}\" (usually 1–3 minutes)…", flush=True)
        run_csv(csv_path, OUTPUT_DIR, Options())
        _, rows = read_tasks(csv_path)
        row = rows[0]
        if row["status"] != "done":
            return {"status": "failed", "error": row["error"] or "unknown error"}
        produced = (csv_path.parent / row["midi_path"]).resolve()
        rec = self.lib.add(place_midi(produced, title), title, composer=composer, performer=performer,
                           tags=tags, source_type="youtube", source_url=url)
        self.lib.log_request(self.request, rec["id"])
        return {"status": "done", **_brief(rec)}

    def transcribe_local(self, path: str, title: str, composer: str = "", performer: str = "",
                         tags: str = "") -> dict:
        from .batch import safe_name
        from .pipeline import convert_file

        src = Path(path).expanduser()
        if not src.is_file():
            return {"status": "failed", "error": f"file not found: {src}"}
        known = self.lib.by_source(str(src.resolve()))
        if known and known["file_exists"]:
            return {"status": "already in library", **_brief(known)}
        print(f"  · transcribing {src.name}…", flush=True)
        if self.engine is None:
            from .engine import TranskunEngine

            self.engine = TranskunEngine()
        info = convert_file(self.engine, src, OUTPUT_DIR, Options(), output_name=safe_name(title))
        rec = self.lib.add(place_midi(Path(info["files"]["midi"]), title), title, composer=composer,
                           performer=performer, tags=tags, source_type="local", source_url=str(src.resolve()))
        self.lib.log_request(self.request, rec["id"])
        return {"status": "done", **_brief(rec)}

    def update_recording(self, id: int, **fields) -> dict:
        rec = self.lib.update(int(id), **fields)
        return _brief(rec) if rec else {"error": f"no recording with id {id}"}

    def run_tool(self, name: str, args: dict) -> str:
        fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
        if fn and name != "update_recording":  # ignore extra arguments the model may add
            allowed = inspect.signature(fn).parameters
            args = {k: v for k, v in args.items() if k in allowed}
        try:
            result = fn(**args) if fn else {"error": f"unknown tool {name}"}
        except youtube.YouTubeError as exc:
            result = {"error": str(exc)}
        except TypeError as exc:
            result = {"error": f"bad arguments: {exc}"}
        except Exception as exc:  # report to the model instead of crashing the chat
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return json.dumps(result, ensure_ascii=False)

    # ---- conversation ------------------------------------------------------------------------
    def ask(self, chat, text: str) -> str:
        self.request = text
        reply = chat.send_user(text)
        for _ in range(MAX_TOOL_ROUNDS):
            if not reply.tool_calls:
                break
            results = [(c, self.run_tool(c.name, c.arguments)) for c in reply.tool_calls]
            reply = chat.send_tool_results(results)
        return reply.text.strip() or "(no reply)"


def index_midi_folder(lib: Library) -> int:
    """Pick up MIDI files that were put into output/MIDI Files by hand."""
    known = {r["midi_path"] for r in lib.all()}
    added = 0
    for f in sorted(MIDI_DIR.glob("*.mid")) if MIDI_DIR.is_dir() else []:
        if str(f.resolve()) not in known:
            try:
                lib.add(f, f.stem, source_type="folder")
                added += 1
            except Exception:
                pass
    return added


HELP = """Commands: /library [words]  list or search the library · /import <folder>  add MIDI files
          /enrich  fill in composers and aliases · /new  start a new conversation · /quit"""


def main(argv: list[str] | None = None) -> int:
    from .llm import complete_json, make_chat

    lib = Library()
    index_midi_folder(lib)
    try:
        chat, label = make_chat(INSTRUCTIONS, TOOLS)
    except Exception as exc:
        print(f"Cannot start the chat model: {exc}", file=sys.stderr)
        return 2
    assistant = Assistant(lib)
    yt = "on" if youtube.api_key() else "off (set YOUTUBE_API_KEY)"
    print(f"piano2midi chat · model {label} · YouTube search {yt}")
    print(f"Library: {lib.count()} recording(s) in {DB_PATH} · MIDI folder: {MIDI_DIR}")
    print(HELP + "\n")
    try:
        import readline  # noqa: F401  (line editing and history)
    except ImportError:
        pass
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return 0
        if text == "/help":
            print(HELP)
            continue
        if text == "/new":
            chat.reset()
            print("Started a new conversation.")
            continue
        if text.startswith("/library"):
            words = text[len("/library"):].strip()
            rows = lib.search(words, limit=50) if words else lib.all()
            print("\n".join(format_row(r) for r in rows) or "Nothing found.")
            continue
        if text.startswith("/import "):
            added = lib.import_folder(Path(text[len("/import "):].strip().strip("'\"")))
            print(f"Imported {len(added)} MIDI file(s). Library now has {lib.count()} recording(s).")
            continue
        if text == "/enrich":
            print(f"Updated {enrich(lib, complete_json)} recording(s).")
            continue
        try:
            answer = assistant.ask(chat, text)
        except KeyboardInterrupt:
            chat.reset()  # a half-finished tool exchange cannot be continued
            print("\n(interrupted; started a new conversation)")
            continue
        except Exception as exc:
            chat.reset()
            print(f"Model error: {exc}\n(started a new conversation)")
            continue
        print(f"\nassistant> {answer}\n")


if __name__ == "__main__":
    sys.exit(main())
