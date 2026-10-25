"""Terminal chat: ask for a piece, get a performance MIDI.

    ./run.sh chat

The assistant looks in the local library first, then searches YouTube (YOUTUBE_API_KEY),
hands the chosen video to the existing CSV download-and-transcribe flow, files the result
in output/MIDI Files and the library, and plays it on Pianoteq over MIDI.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import re
import shutil
import sys
import tempfile
import urllib.parse
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

from . import youtube
from .library import CACHE_DIR, DB_PATH, MIDI_DIR, Library, enrich, file_result, format_row
from .pipeline import Options
from .play import clock

MAX_TOOL_ROUNDS = 8
TASK_DIR = CACHE_DIR / "chat"

INSTRUCTIONS = """You are the piano2midi assistant. The user asks for music; you find a good piano performance and
play it on their piano. Talk like a friendly person at the piano, not like a program.

Language: answer in the language the user writes in. When a message mixes Chinese and English, Chinese is usually
right. Write Chinese in simplified characters unless the user clearly writes traditional ones. Do not force it;
follow the user's habit.

Style: one or two short, natural sentences, plain text without Markdown. Do not mention tools, YouTube, downloads,
transcription, MIDI, note counts, ids or file paths unless the user asks how it works or where something is; then
answer truthfully. Never pretend, for example do not say you are "learning" a piece.

When the user wants to hear something:
1. Call search_library. If a recording clearly matches (same piece; same performer if one was named) and its file
   exists, call play with its id and say in a few words what you are playing.
2. If it is not there, write one short sentence saying you don't have it yet and will look online, which takes a
   minute or two (for example "I don't have that one yet. Let me find a good piano performance online, it takes a
   minute or two."), in the same response as your search_youtube call. Search with the composer, piece, catalog number, performer if named, and "piano".
   Choose the best candidate yourself:
   - prefer recordings where the piano carries the music: solo piano first, piano with voice second, never a band
     mix or a remix; a real human performance by a named pianist, a good recording, and a duration that fits;
   - avoid falling-notes/Synthesia videos, MIDI renderings, tutorials, easy or slowed arrangements, compilations,
     and different pieces (see each result's flags);
   - for a song that is not a piano piece, a good piano cover is right;
   - if the user named a performer, use only that performer; if there is none, say so and ask.
   If nothing fits, try one refined search.
3. Call transcribe_url with the chosen URL and a clean title such as "Chopin - Nocturne Op.9 No.2 (Rubinstein)",
   plus composer, performer and a few English tags. Then call play with the new id and say whose performance it
   is in a few words.
4. If there is no good piano version, say so kindly and do not play something poor.
If you are not sure which piece the user means (a misspelled or ambiguous name), ask a short question such as
"Do you mean John Legend's All of Me?". If the request is merely vague ("a Chopin nocturne"), pick a well-known
piece and say which. If the user pastes a video link, say it takes a minute or two, call transcribe_url with that
link (title empty unless the user named the piece) and play it. For a local audio file path use transcribe_local
in the same way.

Playback:
The user may say these in any language:
- "stop playing", "stop", "pause", "that's enough" and the like: call pause. It keeps the position.
- "continue", "keep playing", "go on", or "play" / "start playing" without naming a piece: call play without an
  id. After a pause this continues from the same spot; it does not start over.
- "start over", "from the beginning": call play without an id and with from_start true.
- Asking for another piece replaces whatever is playing or paused. When a piece ends, playback simply stops.
- If play reports an error, explain it simply (for example the iPad is not connected).
Use update_recording to add composer, performer, aliases (names in other languages) or tags when useful.
Never invent results: report only what the tools returned."""

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
    {"name": "transcribe_url",
     "description": "Download the audio of one video (YouTube, bilibili or another site yt-dlp supports) through "
                    "the piano2midi CSV flow, transcribe it to MIDI and add it to the library. Takes a few minutes.",
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string"},
         "title": {"type": "string", "description": "clean display title used as the file name; "
                                                    "empty to use the video's own title"},
         "composer": {"type": "string"}, "performer": {"type": "string"},
         "tags": {"type": "string", "description": "comma-separated English tags"}},
         "required": ["url"]}},
    {"name": "transcribe_local",
     "description": "Transcribe a local audio file (mp3, m4a, wav, flac) to MIDI and add it to the library.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "title": {"type": "string"},
         "composer": {"type": "string"}, "performer": {"type": "string"}, "tags": {"type": "string"}},
         "required": ["path", "title"]}},
    {"name": "play",
     "description": "Play on the user's piano (Pianoteq through MIDI); returns at once and plays in the background. "
                    "With an id: start that library recording from the beginning, replacing anything else. "
                    "Without an id: continue the current piece from where it was paused, or from the beginning "
                    "when from_start is true or it had stopped.",
     "parameters": {"type": "object", "properties": {
         "id": {"type": "integer"}, "from_start": {"type": "boolean"}}}},
    {"name": "pause",
     "description": "Pause playback and keep the position, so play without an id continues from there.",
     "parameters": {"type": "object", "properties": {}}},
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


TRACKING_PARAMS = {"si", "feature", "pp", "spm_id_from", "vd_source", "share_source", "share_medium",
                   "share_plat", "share_from", "from_spmid", "unique_k", "bbid"}


def clean_url(url: str) -> str:
    """Canonical form of a pasted link, so the same video is recognized when it comes back."""
    parts = urllib.parse.urlsplit(url.strip())
    host = re.sub(r"^(www|m|music)\.", "", parts.netloc.lower())
    path = parts.path.split("/")
    query = urllib.parse.parse_qs(parts.query)
    vid = ""
    if host == "youtu.be":
        vid = path[1] if len(path) > 1 else ""
    elif host == "youtube.com":
        vid = query.get("v", [""])[0] if parts.path == "/watch" else (
            path[2] if len(path) > 2 and path[1] in {"shorts", "live", "embed"} else "")
    if re.fullmatch(r"[\w-]{11}", vid):
        return f"https://www.youtube.com/watch?v={vid}"
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS and not k.startswith("utm_")]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), ""))


class Assistant:
    def __init__(self, lib: Library):
        self.lib = lib
        self.engine = None
        self.player = None
        self.request = ""
        self.speak = None  # optional callback that also says interim messages aloud (voice mode)
        self.touched_playback = False  # set whenever play, pause or stop runs

    # ---- tools -------------------------------------------------------------------------------
    def search_library(self, query: str) -> dict:
        print(f"  · library: {query}", flush=True)
        return {"results": [_brief(r) for r in self.lib.search(query)]}

    def search_youtube(self, query: str, max_results: int = 8) -> dict:
        print(f"  · searching online: {query}", flush=True)
        results = youtube.search(query, max_results)
        for r in results:
            known = self.lib.by_source(r["url"])
            r["in_library"] = known["id"] if known and known["file_exists"] else None
        return {"results": results}

    def transcribe_url(self, url: str, title: str = "", composer: str = "", performer: str = "",
                       tags: str = "") -> dict:
        from .batch import COLUMNS, read_tasks, run_csv, safe_name, save_tasks

        if not re.match(r"https?://", url.strip()):
            return {"status": "failed", "error": "url must start with http:// or https://"}
        url, title = clean_url(url), title.strip()
        known = self.lib.by_source(url)
        if known and known["file_exists"]:
            return {"status": "already in library", **_brief(known)}
        # Each request runs the existing CSV flow in its own work folder. The folder is deleted once the
        # MIDI is in the library; after a failure it stays, so a retry reuses the downloaded audio.
        work = TASK_DIR / hashlib.sha256(url.encode()).hexdigest()[:16]
        work.mkdir(parents=True, exist_ok=True)
        csv_path = work / "task.csv"
        fields, rows = read_tasks(csv_path) if csv_path.exists() else (list(COLUMNS), [])
        if not rows:
            rows = [{c: "" for c in fields} | {"url": url, "name": safe_name(title) if title else ""}]
        save_tasks(csv_path, fields, rows)
        print(f"  · getting {title or url} ready (1–3 minutes)…", flush=True)
        with contextlib.redirect_stdout(io.StringIO()):  # the batch runner's progress is noise here
            run_csv(csv_path, work, Options())
        _, rows = read_tasks(csv_path)
        row = rows[0]
        if row["status"] != "done":
            return {"status": "failed", "error": row["error"] or "unknown error"}
        produced = (work / row["midi_path"]).resolve()
        rec = file_result(self.lib, produced, title or row["name"] or produced.stem, composer=composer,
                          performer=performer, tags=tags, source_type="url", source_url=url)
        self.lib.log_request(self.request, rec["id"])
        shutil.rmtree(work, ignore_errors=True)
        for folder in (TASK_DIR, CACHE_DIR):  # remove them only when nothing else is left inside
            with contextlib.suppress(OSError):
                folder.rmdir()
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
        print(f"  · getting {src.name} ready…", flush=True)
        if self.engine is None:
            from .engine import TranskunEngine

            self.engine = TranskunEngine()
        with tempfile.TemporaryDirectory() as tmp:  # only the final MIDI is kept
            info = convert_file(self.engine, src, Path(tmp), Options(), output_name=safe_name(title))
            rec = file_result(self.lib, Path(info["files"]["midi"]), title, composer=composer,
                              performer=performer, tags=tags, source_type="local", source_url=str(src.resolve()))
        self.lib.log_request(self.request, rec["id"])
        return {"status": "done", **_brief(rec)}

    def _get_player(self):
        from .play import Player

        if self.player is None:
            self.player = Player()  # LookupError when the MIDI output is missing
        return self.player

    def play(self, id: int | None = None, from_start: bool = False) -> dict:
        self.touched_playback = True
        try:
            player = self._get_player()
        except LookupError as exc:
            return {"error": str(exc)}
        if id is not None:
            rec = self.lib.get(int(id))
            if not rec or not rec["file_exists"]:
                return {"error": f"no playable recording with id {id}"}
            player.start([(Path(rec["midi_path"]), rec["title"])])
        elif player.current is None:
            return {"error": "nothing has been played yet"}
        elif from_start or player.state != "paused":
            player.start([player.current])
        else:
            player.resume()
        print(f"  ▶ {player.current[1]}  {clock(player.position)}", flush=True)
        return {"status": "playing", "title": player.current[1], "from": clock(player.position)}

    def pause(self) -> dict:
        self.touched_playback = True
        player = self.player
        if not player or player.current is None:
            return {"status": "nothing is playing"}
        if player.state != "playing":
            return {"status": player.state, "title": player.current[1]}
        player.pause()
        print(f"  ⏸ {player.current[1]}  {clock(player.position)}", flush=True)
        return {"status": "paused", "title": player.current[1], "at": clock(player.position)}

    def stop(self) -> dict:
        self.touched_playback = True
        if self.player and self.player.current is not None:
            self.player.stop()
            print(f"  ⏹ {self.player.current[1]}", flush=True)
        return {"status": "stopped"}

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
            if reply.text.strip():  # say "one moment" before a long step, not after it
                print(f"\nassistant> {reply.text.strip()}\n", flush=True)
                if self.speak:
                    self.speak(reply.text)
            results = [(c, self.run_tool(c.name, c.arguments)) for c in reply.tool_calls]
            reply = chat.send_tool_results(results)
        return reply.text.strip()


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


HELP = """Playback: /play ▶ continue or start · /pause ⏸ · /stop ⏹ back to the beginning
Library: /library [words] · /import <folder> · /enrich      Chat: /new start over · /quit"""


def main(argv: list[str] | None = None) -> int:
    from .llm import complete_json, make_chat

    lib = Library()
    index_midi_folder(lib)
    lib.write_playlist()  # also drops files that were deleted by hand
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
        except EOFError:
            text = "/quit"
        except KeyboardInterrupt:  # Control+C must not end the session by accident
            print("\n(type /quit to exit)")
            continue
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            assistant.stop()
            return 0
        if text in {"/play", "/pause", "/stop"}:
            result = getattr(assistant, text[1:])()
            if "error" in result:
                print(result["error"])
            elif text == "/pause" and result["status"] != "paused":
                print("Nothing is playing.")
            continue
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
        if answer:
            print(f"\nassistant> {answer}\n")


if __name__ == "__main__":
    sys.exit(main())
