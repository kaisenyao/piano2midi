"""Terminal chat: ask for a piece, get a performance MIDI.

    ./run.sh chat

The assistant looks in the local library first, then searches YouTube (YOUTUBE_API_KEY),
hands the chosen video to the existing CSV download-and-transcribe flow, files the result
in output/MIDI Files and the library, and plays it on Pianoteq over MIDI.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import inspect
import io
import json
import re
import random
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
from .playback_commands import match_command, strip_fillers

MAX_TOOL_ROUNDS = 8
TASK_DIR = CACHE_DIR / "chat"

INSTRUCTIONS = """You are the piano2midi assistant. The user asks for music; you find a good piano performance and
play it on their piano. Talk like a friend who loves the piano: warm, cheerful and a little playful, never like a
program.

Language: answer in the language the user writes in. When a message mixes Chinese and English, Chinese is usually
right. Write Chinese in simplified characters unless the user clearly writes traditional ones. Do not force it;
follow the user's habit.

Style: one or two short, natural sentences, plain text without Markdown. Vary your wording; never repeat a sentence
you used earlier in the conversation, and say the same thing a different way each time (a small interjection or a
light, cute touch is welcome). Do not mention tools, YouTube, downloads, transcription, MIDI, note counts, ids or
file paths unless the user asks how it works or where something is; then answer truthfully. Describe the music
as something you play on the piano for the listener: in Chinese, prefer "\u5f39", "\u5f39\u594f" or "\u6f14\u594f" rather than
"\u64ad\u653e"; in English, use natural phrases such as "I'll play it for you". Keep the recording's performer/version
accurate, and never claim authorship or pretend to be learning the piece. Briefly acknowledge successful
pause, continue and restart actions; speak only the connection reminder when an action fails.

When the user wants to hear something:
1. Call search_library. If a recording clearly matches (same piece; same performer if one was named) and its file
   exists, call play_recording with its id and say in a few words what you are playing.
2. If it is not there, write one short, friendly sentence that you will go and find it and that it takes a minute or
   two, worded differently each time (for example "Ooh, nice pick! Give me a minute to find a lovely recording.",
   "On it, back in a minute or two!", "Let me hunt that one down, it won't take long."), in the same response as
   your search_youtube call. Search with the composer, piece, catalog number, performer if named, and "piano".
   Choose the best candidate yourself:
   - prefer recordings where the piano carries the music: solo piano first, piano with voice second, never a band
     mix or a remix; a real human performance by a named pianist, a good recording, and a duration that fits;
   - avoid falling-notes/Synthesia videos, MIDI renderings, tutorials, easy or slowed arrangements, compilations,
     and different pieces (see each result's flags);
   - for a song that is not a piano piece, a good piano cover is right;
   - if the user named a performer, use only that performer; if there is none, say so and ask.
   If nothing fits, try one refined search.
3. Call transcribe_url with the chosen URL and a clean title such as "Chopin - Nocturne Op.9 No.2 (Rubinstein)",
   plus composer, performer and a few English tags. Then call play_recording with the new id and say whose performance it
   is in a few words.
4. If there is no good piano version, say so kindly and do not play something poor.
If you are not sure which piece the user means (a misspelled or ambiguous name), ask a short question such as
"Do you mean John Legend's All of Me?". If the request is merely vague ("a Chopin nocturne"), pick a well-known
piece and say which. If the user pastes a video link, say it takes a minute or two, call transcribe_url with that
link (title empty unless the user named the piece) and play it. For a local audio file path use transcribe_local
in the same way.

Playback:
- Use play_recording(id) ONLY to select a recording returned by search_library or a successful transcription
  in this request. Never invent, infer or reuse an unverified recording ID.
- To operate the current or pending piece, use control_playback(action). It takes NO recording ID:
  pause: stop playing but keep the position; resume: continue from the saved position;
  restart: play the same piece from zero; retry: repeat the pending playback, preserving its intended position;
  cancel: explicitly abandon the selected request and reset playback. Use pause, NOT cancel, for temporary
  waiting, "not yet", or "wait until the connection is ready". These preserve the piece and position.
- A report of disconnection does not erase a piece. If the user reports fixing the connection or asks to try
  again, use retry. If they ask to continue, use resume; if they explicitly ask for the beginning, use restart.
  A vague "play again" after a disconnection can mean retry; do not assume the piece must be reselected.
  Understand the entire request, including explanations before the command. Do not search or select again
  unless they request a different piece.
- The application supplies current/pending playback state with each request. Use it as fact. A piece may be
  paused temporarily so that the assistant can listen; operate that same piece when the user asks to resume.
- Never diagnose connection or missing-file problems without a corresponding tool result. In particular,
  selection_required is an invalid tool choice, not a missing recording: use control_playback for an existing
  piece, or search_library for a new selection.
- Never announce playback before the tool succeeds. A connection failure should be explained simply as a
  connection between the device and piano, without assuming a device type or app.
- If introduction_spoken=true, the application already spoke the introduction. Do not repeat it.
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
    {"name": "play_recording",
     "description": "Select a NEW recording returned by search_library or transcription in this request, and play it from zero. "
                    "For continuing, retrying or restarting the existing piece, use control_playback instead.",
     "parameters": {"type": "object", "properties": {"id": {"type": "integer"}},
                    "required": ["id"], "additionalProperties": False}},
    {"name": "control_playback",
     "description": "Control the current or pending piece using application-owned state. Never supply a recording ID. "
                    "pause keeps position (also for waiting/not ready); resume continues; restart starts at zero; retry repeats a failed attempt; "
                    "cancel explicitly abandons the request and resets position, so never use it merely to wait for reconnection.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string",
                    "enum": ["pause", "resume", "restart", "retry", "cancel"]}},
                    "required": ["action"], "additionalProperties": False}},
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





@dataclass(frozen=True)
class PlaybackRequest:
    path: Path
    title: str
    from_s: float = 0.0
    recording_id: int | None = None


class Assistant:
    def __init__(self, lib: Library):
        self.lib = lib
        self.engine = None
        self.player = None
        self.request = ""
        self.speak = None  # optional callback that also says interim messages aloud (voice mode)
        self.pending: PlaybackRequest | None = None
        self.selectable_ids: set[int] = set()  # only IDs offered by tools in the current request
        self.touched_playback = False  # set whenever play, pause or stop runs

    # ---- tools -------------------------------------------------------------------------------
    def search_library(self, query: str) -> dict:
        print(f"  \xb7 library: {query}", flush=True)
        return {"results": [_brief(r) for r in self.lib.search(query)]}

    def search_youtube(self, query: str, max_results: int = 8) -> dict:
        print(f"  \xb7 searching online: {query}", flush=True)
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
        print(f"  \xb7 getting {title or url} ready (1\u20133 minutes)\u2026", flush=True)
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
        print(f"  \xb7 getting {src.name} ready\u2026", flush=True)
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
        already_playing = False
        if id is not None:
            rec = self.lib.get(int(id))
            if not rec:
                return {"error": "recording_not_found"}
            if not rec["file_exists"]:
                return {"error": "recording_unavailable"}
            request = PlaybackRequest(Path(rec["midi_path"]), rec["title"], recording_id=int(id))
        elif self.pending:
            request = replace(self.pending, from_s=0.0) if from_start else self.pending
            rec = self.lib.get(request.recording_id) if request.recording_id is not None else None
            available = rec and rec["file_exists"] if request.recording_id is not None else request.path.is_file()
            if not available:
                self.pending = None
                return {"error": "recording_unavailable"}
        elif self.player and self.player.current:
            path, title = self.player.current
            if not path.is_file():
                return {"error": "recording_unavailable"}
            already_playing = self.player.state == "playing" and not from_start
            position = self.player.position if self.player.state in {"paused", "playing"} and not from_start else 0.0
            request = PlaybackRequest(path, title, position)
        else:
            return {"error": "nothing_to_play"}
        announced = False
        try:
            player = self._get_player()
            if already_playing:
                player.check_connection()
                return {"status": "playing", "title": request.title, "from": clock(player.position)}

            def introduce():
                nonlocal announced
                if self.speak:
                    if from_start:
                        line = self.control_confirmation("restart")
                    elif request.from_s > 0:
                        line = self.control_confirmation("resume")
                    else:
                        line = f"\u597d\u5566\uff0c\u8fd9\u5c31\u7ed9\u4f60\u5f39\u300a{request.title}\u300b\u3002" if re.search(r"[\u3400-\u9fff]", self.request) else f"Here's {request.title}, just for you."
                    print(f"assistant> {line}", flush=True)
                    self.speak(line)
                    announced = True

            player.start([(request.path, request.title)], from_s=request.from_s, on_ready=introduce)
        except LookupError as exc:
            if self.player and self.player.state == "playing":
                self.player.pause()
            self.pending = request
            print(f"Playback unavailable: {exc}", flush=True)
            return {"error": "playback_connection_unavailable"}
        self.pending = None
        print(f"  \u25b6 {player.current[1]}  {clock(player.position)}", flush=True)
        return {"status": "playing", "title": player.current[1], "from": clock(player.position),
                "introduction_spoken": announced}

    def play_recording(self, id: int) -> dict:
        if id not in self.selectable_ids:
            return {"error": "selection_required", "instruction": "For the current/pending piece use control_playback. For a new piece, search_library first and use an ID from its result."}
        return self.play(id=id)

    def control_playback(self, action: str) -> dict:
        if action == "pause":
            return self.pause()
        if action == "cancel":
            return self.stop()
        if action in {"resume", "restart", "retry"}:
            return self.play(from_start=action == "restart")
        return {"error": "invalid_control_action"}

    def pause(self) -> dict:
        self.touched_playback = True
        player = self.player
        if not player or player.current is None:
            return {"status": "nothing is playing"}
        if player.state != "playing":
            return {"status": player.state, "title": player.current[1]}
        player.pause()
        print(f"  \u23f8 {player.current[1]}  {clock(player.position)}", flush=True)
        return {"status": "paused", "title": player.current[1], "at": clock(player.position)}

    def stop(self) -> dict:
        self.pending = None
        self.touched_playback = True
        if self.player and self.player.current is not None:
            self.player.stop()
            print(f"  \u23f9 {self.player.current[1]}", flush=True)
        return {"status": "stopped"}

    def close(self) -> None:
        """Stop playback before the program exits, letting the notes already sent finish."""
        if self.player:
            self.player.close()

    def update_recording(self, id: int, **fields) -> dict:
        rec = self.lib.update(int(id), **fields)
        return _brief(rec) if rec else {"error": f"no recording with id {id}"}

    def run_tool(self, name: str, args: dict) -> str:
        fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
        # Playback boundaries reject unsupported parameters rather than silently dropping an ID.
        if name in {"play_recording", "control_playback"}:
            expected = {"id"} if name == "play_recording" else {"action"}
            valid_value = type(args.get("id")) is int if name == "play_recording" else isinstance(args.get("action"), str)
            if set(args) != expected or not valid_value:
                return json.dumps({"error": "invalid_playback_arguments", "instruction": "play_recording accepts only id; control_playback accepts only action, with no recording ID."})
        elif fn and name != "update_recording":
            allowed = inspect.signature(fn).parameters
            args = {k: v for k, v in args.items() if k in allowed}
        try:
            result = fn(**args) if fn else {"error": f"unknown tool {name}"}
        except youtube.YouTubeError as exc:
            result = {"error": str(exc)}
        except TypeError as exc:
            result = {"error": f"bad arguments: {exc}"}
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        if name in {"search_library", "transcribe_url", "transcribe_local"}:
            offered = result.get("results", []) if name == "search_library" else [result]
            self.selectable_ids.update(rec["id"] for rec in offered
                                       if isinstance(rec.get("id"), int) and rec.get("file_exists"))
        return json.dumps(result, ensure_ascii=False)

    def control_confirmation(self, action: str) -> str:
        """Small phrasing variation for successful controls; no model call or extra playback state."""
        chinese = bool(re.search(r"[\u3400-\u9fff]", self.request))
        lines = {
            "pause": (("\u597d\u5440\uff0c\u6211\u5148\u505c\u4e00\u4e0b\uff0c\u60f3\u542c\u7684\u65f6\u5019\u53eb\u6211\u7ee7\u7eed\u3002", "\u5df2\u7ecf\u6682\u505c\u5566\uff0c\u60f3\u63a5\u7740\u542c\u5c31\u53eb\u6211\u3002", "\u55ef\uff0c\u6211\u5148\u505c\u4e0b\u6765\uff0c\u4f60\u51c6\u5907\u597d\u4e86\u518d\u7ee7\u7eed\u3002"),
                      ("Sure, I'll pause here. Ask me when you'd like to continue.", "Paused. Just say when you'd like me to carry on.")),
            "resume": (("\u6765\u5566\uff0c\u6211\u4eec\u63a5\u7740\u5f39\u3002", "\u597d\u5440\uff0c\u63a5\u7740\u7ed9\u4f60\u5f39\u3002"),
                       ("Of course, let's carry on.", "Here we go, picking up where we left off.")),
            "restart": (("\u597d\uff0c\u90a3\u6211\u4ece\u5934\u518d\u7ed9\u4f60\u5f39\u4e00\u904d\u3002", "\u6ca1\u95ee\u9898\uff0c\u6211\u4eec\u4ece\u5934\u5f00\u59cb\u3002"),
                        ("Of course, I'll play it again from the beginning.", "Let's start again from the first note.")),
            "cancel": (("\u597d\u5440\uff0c\u8fd9\u9996\u5148\u4e0d\u5f39\u4e86\uff0c\u60f3\u542c\u4ec0\u4e48\u518d\u544a\u8bc9\u6211\u3002", "\u597d\uff0c\u6211\u5148\u505c\u4e0b\u6765\uff0c\u7b49\u4f60\u4e0b\u6b21\u70b9\u66f2\u3002"),
                       ("Okay, I'll stop here. Tell me what you'd like to hear next.", "Stopped. Just call me when you'd like another piece.")),
        }
        return random.choice(lines[action][0 if chinese else 1])

    def playback_reply(self, result: dict) -> str:
        """User-facing playback results, shared by direct retries and model tool calls."""
        chinese = bool(re.search(r"[\u3400-\u9fff]", self.request))
        if result.get("error") == "playback_connection_unavailable":
            return ("\u54a6\uff0c\u597d\u50cf\u8fd8\u6ca1\u8fde\u4e0a\u7434\u5462\u3002\u65b9\u4fbf\u5e2e\u6211\u68c0\u67e5\u4e00\u4e0b\u8bbe\u5907\u548c\u94a2\u7434\u7684\u8fde\u63a5\u5417\uff1f\u51c6\u5907\u597d\u540e\uff0c\u968f\u65f6\u53eb\u6211\u64ad\u653e\u5c31\u597d\u5566\u3002"
                    if chinese else "Oh, I couldn't connect to the piano. Could you check the connection between your device and piano? Ask me to play when you're ready.")
        if result.get("error") == "recording_unavailable":
            return "\u8fd9\u9996\u66f2\u5b50\u7684\u6587\u4ef6\u6682\u65f6\u627e\u4e0d\u5230\uff0c\u8bf7\u91cd\u65b0\u9009\u4e00\u9996\u3002" if chinese else "That recording is unavailable. Please choose a piece again."
        if result.get("error") == "recording_not_found":
            return "\u8fd9\u6b21\u6ca1\u6709\u9009\u4e2d\u6709\u6548\u66f2\u76ee\uff0c\u8bf7\u544a\u8bc9\u6211\u60f3\u542c\u54ea\u9996\u3002" if chinese else "I could not select that recording. Which piece would you like?"
        if result.get("error") == "nothing_to_play":
            return "\u4f60\u60f3\u542c\u54ea\u9996\uff1f\u544a\u8bc9\u6211\u66f2\u540d\u5c31\u597d\u5566\u3002" if chinese else "What would you like to hear? Tell me the title."
        if result.get("error"):
            return "\u8fd9\u6b21\u6ca1\u80fd\u64ad\u653e\u6210\u529f\uff0c\u6211\u4eec\u7a0d\u540e\u518d\u8bd5\u4e00\u4e0b\u3002" if chinese else "I couldn't start playback this time. Let's try again shortly."
        if result.get("status") == "paused":
            return self.control_confirmation("pause")
        if result.get("status") == "stopped":
            return self.control_confirmation("cancel")
        if result.get("status") == "nothing is playing":
            return "\u73b0\u5728\u6ca1\u6709\u66f2\u5b50\u5728\u5f39\uff0c\u60f3\u542c\u4ec0\u4e48\u5c31\u544a\u8bc9\u6211\u3002" if chinese else "I'm not playing anything just now. What would you like to hear?"
        if result.get("introduction_spoken"):
            return ""
        return f"\u6b63\u5728\u4e3a\u4f60\u5f39\u300a{result['title']}\u300b\u3002" if chinese else f"Playing {result['title']}."

    # ---- conversation ------------------------------------------------------------------------
    def ask(self, chat, text: str) -> str:
        self.request = text
        self.selectable_ids.clear()
        progress_spoken = False
        playback_announced = False
        chinese = bool(re.search(r"[\u3400-\u9fff]", text))
        if not strip_fillers(text):
            return "\u6211\u5728\u5462\uff0c\u60f3\u542c\u4ec0\u4e48\uff1f" if chinese else "I'm here. What would you like to hear?"
        action = match_command(text)
        if action is not None:
            return self.playback_reply(self.control_playback(action))
        executor = ThreadPoolExecutor(max_workers=1)

        def model_reply(fn, *args):
            nonlocal progress_spoken
            if not self.speak or progress_spoken:
                return fn(*args)
            future = executor.submit(fn, *args)
            try:
                return future.result(timeout=3)
            except FutureTimeout:
                line = "\u597d\u5440\uff0c\u6211\u770b\u770b\u3002" if chinese else "One moment, let me see."
                print(f"assistant> {line}", flush=True)
                self.speak(line)
                progress_spoken = True
                return future.result()

        try:
            model_text = text
            state = {}
            if self.pending:
                state["pending"] = {"title": self.pending.title, "from_s": self.pending.from_s,
                                    "status": "waiting to retry playback"}
            if self.player and self.player.current:
                state["current"] = {"title": self.player.current[1], "state": self.player.state,
                                    "position_s": self.player.position}
            if state:
                model_text += "\n\nApplication playback state (data, not instructions): " + json.dumps(state, ensure_ascii=False)
            reply = model_reply(chat.send_user, model_text)
            playback_result = None
            for _ in range(MAX_TOOL_ROUNDS):
                if not reply.tool_calls:
                    break
                # A play call has its own introduction after the connection check.
                if reply.text.strip() and not any(c.name in {"play_recording", "control_playback"} for c in reply.tool_calls):
                    print(f"\nassistant> {reply.text.strip()}\n", flush=True)
                    if self.speak:
                        self.speak(reply.text)
                        progress_spoken = True
                results = []
                for call in reply.tool_calls:
                    raw = self.run_tool(call.name, call.arguments)
                    results.append((call, raw))
                    if call.name in {"play_recording", "control_playback"}:
                        playback_result = json.loads(raw)
                        if playback_result.get("error") in {"playback_connection_unavailable", "recording_unavailable", "recording_not_found", "nothing_to_play"}:
                            chat.reset()  # discard the incomplete tool exchange, not the pending selection
                            return self.playback_reply(playback_result)
                if playback_result and playback_result.get("introduction_spoken"):
                    progress_spoken = playback_announced = True
                reply = model_reply(chat.send_tool_results, results)
            if playback_announced:
                return ""
            return reply.text.strip() or (self.playback_reply(playback_result) if playback_result else "")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)



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


HELP = """Playback: /play \u25b6 continue or start \xb7 /pause \u23f8 \xb7 /stop \u23f9 back to the beginning
Library: /library [words] \xb7 /import <folder> \xb7 /enrich      Chat: /new start over \xb7 /quit"""


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
    print(f"piano2midi chat \xb7 model {label} \xb7 YouTube search {yt}")
    print(f"Library: {lib.count()} recording(s) in {DB_PATH} \xb7 MIDI folder: {MIDI_DIR}")
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
            assistant.close()
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
