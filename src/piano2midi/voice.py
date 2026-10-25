"""Voice: say "Hey Jarvis", then ask for a piece. The same assistant as ./run.sh chat, with ears and a voice.

    ./run.sh voice

The wake word runs on this computer (openWakeWord). Speech recognition uses OpenAI gpt-transcribe
(OPENAI_API_KEY). Replies are spoken by ElevenLabs (ELEVENLABS_API_KEY and PIANO2MIDI_VOICE_ID), or by the
macOS voice without them. Add --debug to see how close the wake word came when it did not trigger.
Install the extras once: uv pip install --python .venv/bin/python -e ".[voice]"
"""

from __future__ import annotations

import io
import json
import os
import re
import statistics
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import warnings
import wave
from collections import deque

import numpy as np

warnings.filterwarnings("ignore")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 ms, the frame size openWakeWord works with
FRAME_S = FRAME / SAMPLE_RATE
WAKE_WORD = os.environ.get("PIANO2MIDI_WAKE_WORD", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("PIANO2MIDI_WAKE_THRESHOLD", "0.3"))
WAKE_VAD = 0.5  # only score the wake word while someone is speaking, so a low threshold stays safe
STT_MODEL = os.environ.get("PIANO2MIDI_STT_MODEL", "gpt-transcribe")
LANGUAGES = [x.strip() for x in os.environ.get("PIANO2MIDI_LANGUAGES", "").split(",") if x.strip()]  # e.g. zh,en
TTS_MODEL = os.environ.get("PIANO2MIDI_TTS_MODEL", "eleven_v4_turbo")
VOICE_ID = os.environ.get("PIANO2MIDI_VOICE_ID", "")
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?output_format=pcm_24000"
# The ElevenLabs defaults, as on its text-to-speech page: stability 0.5, similarity 0.75, no style exaggeration.
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True, "speed": 1.0}
START_TIMEOUT_S = 6.0  # wait this long for speech to begin after the wake word
END_SILENCE_S = 0.8  # this much silence ends the request
MAX_REQUEST_S = 15.0

# For Chinese speakers (PIANO2MIDI_LANGUAGES contains zh): a simplified-Chinese example sentence with an English
# title, so the transcript uses simplified characters and keeps English song names in English.
STT_PROMPT = "\u4ee5\u4e0b\u662f\u666e\u901a\u8bdd\u548c\u82f1\u8bed\u7684\u53e5\u5b50\u3002\u6211\u60f3\u542c Adele \u7684 Someone Like You\u3002\u6682\u505c\u3002\u7ee7\u7eed\u3002"

PROMPT_ECHOES = {STT_PROMPT, STT_PROMPT.split("\u3002", 1)[-1], "\u6682\u505c\u3002\u7ee7\u7eed\u3002"}

VOICE_NOTE = """

Your replies are spoken aloud. Use one short sentence when you can, never lists, links or file paths."""

# Short commands handled without the model. Chinese phrases are written as escapes because the repository is
# English-only; the pinyin is in the comments.
COMMANDS = {  # simplified and traditional forms
    "pause": {"stop", "pause", "stop playing", "stop it", "stop the music", "enough", "that's enough",
              "\u505c", "\u505c\u4e00\u4e0b", "\u6682\u505c", "\u66ab\u505c", "\u505c\u6b62", "\u505c\u6b62\u64ad\u653e", "\u6682\u505c\u64ad\u653e", "\u66ab\u505c\u64ad\u653e", "\u591f\u4e86", "\u5920\u4e86", "\u522b\u5f39\u4e86", "\u5225\u5f48\u4e86", "\u5148\u505c\u4e00\u4e0b"},
    # ting, ting yixia, zanting, tingzhi, tingzhi bofang, zanting bofang, gou le, bie tan le, xian ting yixia
    "resume": {"play", "continue", "keep playing", "resume", "go on", "start playing", "continue playing",
               "\u64ad\u653e", "\u5f00\u59cb\u64ad\u653e", "\u958b\u59cb\u64ad\u653e", "\u7ee7\u7eed", "\u7e7c\u7e8c", "\u7ee7\u7eed\u5f39", "\u7e7c\u7e8c\u5f48", "\u7ee7\u7eed\u64ad\u653e", "\u7e7c\u7e8c\u64ad\u653e", "\u63a5\u7740\u5f39", "\u63a5\u8457\u5f48", "\u63a5\u7740\u64ad\u653e", "\u63a5\u8457\u64ad\u653e"},
    # bofang, kaishi bofang, jixu, jixu tan, jixu bofang, jiezhe tan, jiezhe bofang
    "restart": {"start over", "from the beginning", "play it again", "play it from the beginning",
                "\u4ece\u5934\u5f39", "\u5f9e\u982d\u5f48", "\u4ece\u5934\u5f00\u59cb", "\u5f9e\u982d\u958b\u59cb", "\u4ece\u5934\u64ad\u653e", "\u5f9e\u982d\u64ad\u653e", "\u91cd\u65b0\u5f39", "\u91cd\u65b0\u5f48"},
    # congtou tan, congtou kaishi, congtou bofang, chongxin tan
}
FILLERS_START = ("hey jarvis", "jarvis", "please", "okay", "ok", "uh", "um", "so",
                 "\u8bf7", "\u5443", "\u55ef", "\u90a3\u4e2a", "\u90a3", "\u597d\u7684", "\u597d", "\u54e6", "\u554a")
# qing, e, en, nage, na, hao de, hao, o, a
FILLERS_END = ("please", "\u5427", "\u554a", "\u5440", "\u54e6")  # ba, a, ya, o


def match_command(text: str) -> str | None:
    """'pause', 'resume' or 'restart' when the whole request is one of the short commands."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"[^\w\s']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    changed = True
    while changed and t:
        changed = False
        for f in FILLERS_START:
            whole_word = t == f or t.startswith(f + " ") if f.isascii() else t.startswith(f)  # "so" is not "sorry"
            if whole_word:
                t, changed = t[len(f):].strip(), True
        for f in FILLERS_END:
            if t.endswith(f) and t != f:
                t, changed = t[: -len(f)].strip(), True
    for action, phrases in COMMANDS.items():
        if t in phrases:
            return action
    return None


def speakable(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [title](link) -> title
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[`*_#>\[\]]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def hints(lib, limit: int = 50) -> list[str]:
    """Titles, composers and performers in the library, to help recognize names."""
    words: list[str] = []
    for r in reversed(lib.playlist()):
        core = re.sub(r"\s*\(.*?\)", "", r["title"].split(" - ", 1)[-1]).strip()  # "Someone Like You"
        for w in (core, r["title"], r["composer"], r["performer"]):
            if w and w not in words:
                words.append(w)
    return words[:limit]


def clean_transcript(text: str, prompt: str | None) -> str:
    text = text.strip()
    if prompt and text and (text[:6] == prompt[:6] or text in PROMPT_ECHOES):
        return ""  # the recognizer repeating its prompt on noise, not something the user said
    return text


def to_wav(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


class Microphone:
    def __init__(self):
        import sounddevice as sd

        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME)
        self.stream.start()

    def read(self) -> np.ndarray:
        data, _ = self.stream.read(FRAME)
        return data[:, 0]

    def drain(self) -> None:
        """Drop audio that piled up while we were busy or speaking."""
        n = self.stream.read_available
        if n:
            self.stream.read(n)


class WakeWord:
    def __init__(self, name: str = WAKE_WORD, threshold: float = WAKE_THRESHOLD):
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        options = dict(wakeword_models=[name], inference_framework="onnx", vad_threshold=WAKE_VAD)
        try:
            self.model = Model(**options)
        except Exception:  # first run: fetch the model files
            download_models([name])
            self.model = Model(**options)
        self.threshold = threshold

    def score(self, frame: np.ndarray) -> float:
        return float(max(self.model.predict(frame).values()))

    def reset(self) -> None:
        self.model.reset()


def record(mic, noise_rms: float) -> np.ndarray | None:
    """Record one request: wait for speech, stop after a short silence. Float32 at 16 kHz, or None."""
    threshold = min(max(3 * noise_rms, 300.0), 1500.0)
    frames, started, silent, elapsed = [], False, 0.0, 0.0
    while elapsed < MAX_REQUEST_S:
        frame = mic.read()
        elapsed += FRAME_S
        frames.append(frame)
        if rms(frame) > threshold:
            started, silent = True, 0.0
        elif started:
            silent += FRAME_S
            if silent >= END_SILENCE_S:
                break
        elif elapsed > START_TIMEOUT_S:
            return None
    return np.concatenate(frames).astype(np.float32) / 32768.0 if started else None


class Transcriber:
    """OpenAI speech recognition; the library's titles and names are passed as spelling hints."""

    def __init__(self, model: str = STT_MODEL):
        from openai import OpenAI

        self.client, self.model = OpenAI(), model
        self.prompt = STT_PROMPT if "zh" in LANGUAGES else None
        self.label = f"OpenAI {model}"

    def __call__(self, audio: np.ndarray, lib) -> str:
        extra = {}
        if self.model == "gpt-transcribe":  # spelling hints and expected languages
            extra = {"keywords": hints(lib), **({"languages": LANGUAGES} if LANGUAGES else {})}
        request = dict(model=self.model, file=("request.wav", to_wav(audio), "audio/wav"), response_format="text",
                       **({"prompt": self.prompt} if self.prompt else {}))
        try:
            text = self.client.audio.transcriptions.create(**request, **extra)
        except Exception:
            if not extra:
                raise
            text = self.client.audio.transcriptions.create(**request)  # without the optional hints
        return clean_transcript(str(text), self.prompt)


class Speaker:
    """ElevenLabs text-to-speech, played while it streams in; the macOS voice as a fallback."""

    def __init__(self):
        self.key = os.environ.get("ELEVENLABS_API_KEY", "")
        self.cloud = bool(self.key and VOICE_ID)
        self.label = f"ElevenLabs {TTS_MODEL}" if self.cloud else "macOS (no ELEVENLABS_API_KEY / PIANO2MIDI_VOICE_ID)"

    def say(self, text: str) -> None:
        text = speakable(text)
        if not text:
            return
        if self.cloud:
            try:
                return self._eleven(text)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:200]
                print(f"  (ElevenLabs error {exc.code}: {detail}; using the Mac voice)")
                if exc.code in (401, 402, 403):  # key, payment or permission: stop trying
                    self.cloud = False
            except Exception as exc:  # network trouble: the Mac voice for this sentence only
                print(f"  (ElevenLabs failed: {exc}; using the Mac voice)")
        voice = ["-v", "Tingting"] if re.search(r"[\u3400-\u9fff]", text) else []
        subprocess.run(["say", *voice, text], check=False)

    def _eleven(self, text: str) -> None:
        import sounddevice as sd

        request = urllib.request.Request(
            ELEVENLABS_URL.format(voice=VOICE_ID), data=json.dumps({"text": text, "model_id": TTS_MODEL, "voice_settings": VOICE_SETTINGS}).encode(),
            headers={"xi-api-key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as resp:
            with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as out:
                rest = b""
                while chunk := resp.read(4800):  # 0.1 s of audio
                    chunk, rest = rest + chunk, b""
                    if len(chunk) % 2:
                        chunk, rest = chunk[:-1], chunk[-1:]
                    out.write(chunk)


def beep(freq: float = 880.0, seconds: float = 0.12) -> None:
    import sounddevice as sd

    t = np.arange(int(24000 * seconds)) / 24000
    tone = 0.2 * np.sin(2 * np.pi * freq * t) * np.hanning(t.size)
    sd.play(tone.astype(np.float32), 24000)
    sd.wait()


def handle_command(assistant, action: str) -> bool:
    """Run a short command; False when it cannot apply (nothing played yet), so the model answers instead."""
    if action == "pause":
        assistant.pause()
        return True
    return "error" not in assistant.play(from_start=action == "restart")


def main(argv: list[str] | None = None) -> int:
    from .chat import INSTRUCTIONS, TOOLS, Assistant, index_midi_folder
    from .library import Library
    from .llm import make_chat
    from .play import clock

    lib = Library()
    index_midi_folder(lib)
    lib.write_playlist()
    try:
        chat, label = make_chat(INSTRUCTIONS + VOICE_NOTE, TOOLS)
    except Exception as exc:
        print(f"Cannot start the chat model: {exc}", file=sys.stderr)
        return 2
    speaker = Speaker()
    assistant = Assistant(lib)
    assistant.speak = speaker.say  # "one moment" before a long search is spoken too
    try:
        print("Loading speech recognition and the wake word…", flush=True)
        stt, wake, mic = Transcriber(), WakeWord(), Microphone()
    except ImportError as exc:
        print(f"Voice needs the extras: uv pip install --python .venv/bin/python -e \".[voice]\" ({exc})",
              file=sys.stderr)
        return 2
    debug = "--debug" in (argv if argv is not None else sys.argv[1:])
    print(f"piano2midi voice · model {label} · hearing {stt.label} · voice {speaker.label}")
    print('Say "Hey Jarvis", then your request. Control+C quits.\n', flush=True)
    noise: deque[float] = deque(maxlen=25)  # about two seconds of background level
    last_hint = 0.0
    try:
        while True:
            frame = mic.read()
            noise.append(rms(frame))
            score = wake.score(frame)
            if score < wake.threshold:
                if debug and score >= 0.1 and time.time() - last_hint > 1:  # a near miss
                    print(f"  (wake word {score:.2f}, needs {wake.threshold:.2f})", flush=True)
                    last_hint = time.time()
                continue
            player = assistant.player
            was_playing = player is not None and player.state == "playing"
            if was_playing:
                player.pause()  # so the piano does not drown out the request
            assistant.touched_playback = False
            beep()
            mic.drain()
            audio = record(mic, statistics.median(noise) if noise else 0.0)
            try:
                text = stt(audio, lib) if audio is not None else ""
            except Exception as exc:
                print(f"  (speech recognition failed: {exc})")
                text = ""
            if text:
                print(f"you> {text}", flush=True)
                action = match_command(text)
                if not (action and handle_command(assistant, action)):
                    try:
                        answer = assistant.ask(chat, text)
                    except Exception as exc:
                        chat.reset()
                        print(f"Model error: {exc}")
                        answer = ""
                    if answer:
                        print(f"assistant> {answer}\n", flush=True)
                        speaker.say(answer)
            if was_playing and assistant.player.state == "paused":
                if assistant.touched_playback:  # paused while listening and asked to stay paused
                    print(f"  ⏸ {assistant.player.current[1]}  {clock(assistant.player.position)}", flush=True)
                else:
                    assistant.player.resume()
            mic.drain()
            wake.reset()
    except KeyboardInterrupt:
        assistant.stop()
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
