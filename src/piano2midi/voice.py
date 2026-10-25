"""Voice: say "Hey Jasmine" (or the Chinese name), then ask for a piece. The same assistant as ./run.sh chat,
with ears and a voice.

    ./run.sh voice

The wake words are heard on this computer by sherpa-onnx keyword spotting, which takes any name without
training; set them with PIANO2MIDI_WAKE_WORDS (comma-separated). Speech recognition uses OpenAI gpt-transcribe
(OPENAI_API_KEY). Replies are spoken by ElevenLabs (ELEVENLABS_API_KEY and PIANO2MIDI_VOICE_ID), or by the
macOS voice without them. Pause after the name and she says she is listening; or say the name and the request
in one breath. Add --debug to see which wake word was heard and what she answered, or --greetings to hear
every greeting once.
Install the extras once: uv pip install --python .venv/bin/python -e ".[voice]"
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.error
import urllib.request
import warnings
import wave
from collections import deque
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 ms of audio per read
FRAME_S = FRAME / SAMPLE_RATE
# Chinese characters or English words. The default Chinese name is xiao ming tongxue.
WAKE_WORDS = [w.strip() for w in os.environ.get("PIANO2MIDI_WAKE_WORDS", "\u5c0f\u8317\u540c\u5b66,Hey Jasmine").split(",")
              if w.strip()]
WAKE_THRESHOLD = float(os.environ.get("PIANO2MIDI_WAKE_THRESHOLD", "0.25"))  # lower hears more, wakes more by mistake
WAKE_MODEL = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"  # Chinese and English, about 33 MB to download
WAKE_MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{WAKE_MODEL}.tar.bz2"
WAKE_PARTS = {"encoder": "encoder-epoch-13-avg-2-chunk-16-left-64.onnx",
              "decoder": "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
              "joiner": "joiner-epoch-13-avg-2-chunk-16-left-64.onnx",
              "tokens": "tokens.txt", "lexicon": "en.phone"}
WAKE_SCORE = 1.5  # how strongly the spotter favors the wake words; 1.5 suited a test with the owner's voice
# Other ways to say English words in wake words, besides the dictionary's. Chinese speakers often say
# "jas-MEEN"; with these a test recording of the owner went from 0 to 9 wakes out of 10.
EXTRA_PRONUNCIATIONS = {"JASMINE": ["JH AE1 Z M IY1 N", "JH AE1 S M IY1 N"]}
STT_MODEL = os.environ.get("PIANO2MIDI_STT_MODEL", "gpt-transcribe")
LANGUAGES = [x.strip() for x in os.environ.get("PIANO2MIDI_LANGUAGES", "").split(",") if x.strip()]  # e.g. zh,en
TTS_MODEL = os.environ.get("PIANO2MIDI_TTS_MODEL", "eleven_v4_turbo")
VOICE_ID = os.environ.get("PIANO2MIDI_VOICE_ID", "")
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?output_format=pcm_24000"
# The ElevenLabs defaults, as on its text-to-speech page: stability 0.5, similarity 0.75, no style exaggeration.
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True, "speed": 1.0}
# What she says when woken and you pause, in the language of the name you used; made once with the ElevenLabs
# voice and kept in output/.cache/voice. Without ElevenLabs she beeps instead.
GREETINGS = {
    "zh": ["\u5728\u5462\u5728\u5462\uff01", "\u563f\uff0c\u6211\u5728\uff01", "\u6765\u5566\u6765\u5566\uff01", "\u6211\u5728\u5462\uff0c\u600e\u4e48\u5566\uff1f",
           "\u55e8\uff0c\u6211\u5728\uff01", "\u4f60\u8bf4\u4f60\u8bf4\uff01"],
    # zai ne zai ne! / hei, wo zai! / lai la lai la! / wo zai ne, zenme la? / hai, wo zai! / ni shuo ni shuo!
    "en": ["Yes! I'm here!", "Hey! What's up?", "I'm all ears!", "Yes? I'm listening!", "Hi there!", "Here I am!"],
}
# Greetings are said brightly: an ElevenLabs audio tag, a little less steady than replies, and a touch faster.
GREETING_TAG = "[excited] "
GREETING_SETTINGS = {**VOICE_SETTINGS, "stability": 0.35, "speed": 1.1}
GO_ON_FRAMES = 4  # after the wake word, 0.32 s to tell whether you go straight on with the request
START_TIMEOUT_S = 6.0  # wait this long for speech to begin after the wake word
END_SILENCE_S = 0.8  # this much silence ends the request
MAX_REQUEST_S = 15.0

# For Chinese speakers (PIANO2MIDI_LANGUAGES contains zh): a simplified-Chinese example sentence with an English
# title, so the transcript uses simplified characters and keeps English song names in English.
STT_PROMPT = "\u4ee5\u4e0b\u662f\u666e\u901a\u8bdd\u548c\u82f1\u8bed\u7684\u53e5\u5b50\u3002\u6211\u60f3\u542c Adele \u7684 Someone Like You\u3002\u6682\u505c\u3002\u7ee7\u7eed\u3002"

PROMPT_ECHOES = {STT_PROMPT, STT_PROMPT.split("\u3002", 1)[-1], "\u6682\u505c\u3002\u7ee7\u7eed\u3002"}
# Spelling hints for Chinese speakers besides the library's names: the commands that are easiest to mishear.
STT_COMMAND_HINTS = ["\u6682\u505c", "\u505c\u6b62\u64ad\u653e", "\u7ee7\u7eed\u64ad\u653e", "\u4ece\u5934\u5f39"]
# zanting, tingzhi bofang, jixu bofang, congtou tan

VOICE_NOTE = """

Your names are {names}; people say one of them to wake you, and you use the one that fits the language of the
conversation. Your replies are spoken aloud by a young, cheerful voice, so sound natural and a little playful. Use one short
sentence when you can and no lists. If the user asks for a link or a file path, say it is on the screen and include
it; it is shown but not read aloud."""

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


def assistant_names(words: list[str]) -> list[str]:
    """The names inside the wake words: "Hey Jasmine" -> "Jasmine"."""
    return list(dict.fromkeys(re.sub(r"(?i)^hey\s+", "", w) for w in words))


FILLERS_START = (*dict.fromkeys(w.casefold() for w in WAKE_WORDS + assistant_names(WAKE_WORDS)),
                 "please", "okay", "ok", "uh", "um", "so",
                 "\u8bf7", "\u5443", "\u55ef", "\u90a3\u4e2a", "\u90a3", "\u597d\u7684", "\u597d", "\u54e6", "\u554a")
# qing, e, en, nage, na, hao de, hao, o, a
FILLERS_END = ("please", "\u5427", "\u554a", "\u5440", "\u54e6", "\u5443", "\u55ef")  # ba, a, ya, o, e, en


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


def wake_model() -> Path:
    """The keyword spotting model's folder, downloaded on first use (only the files needed are kept)."""
    from .library import CACHE_DIR

    folder = CACHE_DIR / "models" / WAKE_MODEL
    if all((folder / name).is_file() for name in WAKE_PARTS.values()):
        return folder
    print("Downloading the wake word model (about 33 MB, once)…", flush=True)
    folder.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(WAKE_MODEL_URL, timeout=60) as resp, tarfile.open(fileobj=resp, mode="r|bz2") as tar:
        for member in tar:
            name = member.name.rsplit("/", 1)[-1]
            if member.isfile() and name in WAKE_PARTS.values():
                part = folder / (name + ".part")
                part.write_bytes(tar.extractfile(member).read())
                part.replace(folder / name)
    missing = [name for name in WAKE_PARTS.values() if not (folder / name).is_file()]
    if missing:
        raise RuntimeError(f"the wake word download lacks {', '.join(missing)}")
    return folder


def keyword_lines(phrase: str, lexicon: dict[str, list[str]], limit: int = 8) -> list[str]:
    """A wake word in the model's spelling, one line per way of saying it: Chinese as split pinyin, English as
    phonemes from the lexicon (word -> pronunciations).

    "Hey Jasmine" -> ["HH EY1 JH AE1 Z M AH0 N @Hey_Jasmine", "HH EY1 JH AE1 Z M IY1 N @Hey_Jasmine", ...]
    """
    from pypinyin import pinyin
    from pypinyin.contrib.tone_convert import to_finals_tone, to_initials

    spellings = [""]
    for word in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z']+", phrase):
        if word.isascii():
            if word.upper() not in lexicon:
                raise ValueError(f'the wake word "{phrase}" uses "{word}", which the model does not know')
            ways = lexicon[word.upper()]
        else:
            parts = []
            for syllable, *_ in pinyin(word):
                parts += [p for p in (to_initials(syllable, strict=False), to_finals_tone(syllable, strict=False))
                          if p] or [syllable]
            ways = [" ".join(parts)]
        spellings = [f"{s} {w}".strip() for s in spellings for w in ways][:limit]
    if spellings == [""]:
        raise ValueError(f'the wake word "{phrase}" has no Chinese characters or English words')
    return [f"{s} @" + "_".join(phrase.split()) for s in spellings]


class WakeWord:
    """Listens for the wake words on this computer, with sherpa-onnx keyword spotting (no training needed)."""

    def __init__(self, words: list[str] = WAKE_WORDS, threshold: float = WAKE_THRESHOLD):
        import sherpa_onnx

        folder = wake_model()
        lexicon: dict[str, list[str]] = {}
        for line in (folder / WAKE_PARTS["lexicon"]).read_text(encoding="utf-8").splitlines():
            word, *phones = line.split()
            lexicon.setdefault(word, []).append(" ".join(phones))
        for word, ways in EXTRA_PRONUNCIATIONS.items():
            lexicon.setdefault(word, []).extend(w for w in ways if w not in lexicon[word])
        known = {line.split()[0] for line in (folder / WAKE_PARTS["tokens"]).read_text(encoding="utf-8").splitlines()}
        lines = [line for w in words for line in keyword_lines(w, lexicon)]
        for line in lines:
            unknown = [t for t in line.rsplit(" @", 1)[0].split() if t not in known]
            if unknown:
                raise ValueError(f"the wake word {line.rsplit('@', 1)[-1]} cannot be spelled for the model: {unknown}")
        keywords = folder / "keywords.txt"
        keywords.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(folder / WAKE_PARTS["tokens"]), encoder=str(folder / WAKE_PARTS["encoder"]),
            decoder=str(folder / WAKE_PARTS["decoder"]), joiner=str(folder / WAKE_PARTS["joiner"]),
            keywords_file=str(keywords), keywords_threshold=threshold, keywords_score=WAKE_SCORE, num_threads=1,
            provider="cpu")
        self.words, self.threshold = words, threshold
        self.reset()

    def heard(self, frame: np.ndarray) -> str | None:
        """Feed one frame of 16-bit audio; the wake word if it was just said."""
        self.stream.accept_waveform(SAMPLE_RATE, frame.astype(np.float32) / 32768.0)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
            found = self.spotter.get_result(self.stream)
            if found:
                self.reset()
                return found.replace("_", " ")
        return None

    def reset(self) -> None:
        self.stream = self.spotter.create_stream()


def speech_level(noise_rms: float) -> float:
    """Loudness above which a frame counts as speech."""
    return min(max(3 * noise_rms, 300.0), 1500.0)


def record(mic, noise_rms: float, first: list[np.ndarray] | None = None) -> np.ndarray | None:
    """Record one request, starting with the frames in first: wait for speech, stop after a short silence.
    Float32 at 16 kHz, or None.

    Speech counts as begun after 0.24 s of sound, so the beep or a click does not start the request.
    """
    threshold = speech_level(noise_rms)
    pending = list(first or [])
    frames, loud, started, silent, elapsed = [], 0, False, 0.0, 0.0
    while elapsed < MAX_REQUEST_S:
        frame = pending.pop(0) if pending else mic.read()
        elapsed += FRAME_S
        frames.append(frame)
        if rms(frame) > threshold:
            loud, silent = loud + 1, 0.0
            started = started or loud >= 3
        else:
            loud = 0
            if started:
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
            fixed = [*WAKE_WORDS, *(STT_COMMAND_HINTS if "zh" in LANGUAGES else [])]
            extra = {"keywords": fixed + hints(lib, limit=50 - len(fixed)),
                     **({"languages": LANGUAGES} if LANGUAGES else {})}
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

    def _request(self, text: str, settings: dict = VOICE_SETTINGS) -> urllib.request.Request:
        body = {"text": text, "model_id": TTS_MODEL, "voice_settings": settings}
        return urllib.request.Request(ELEVENLABS_URL.format(voice=VOICE_ID), data=json.dumps(body).encode(),
                                      headers={"xi-api-key": self.key, "Content-Type": "application/json"})

    def clip_path(self, text: str, settings: dict = VOICE_SETTINGS) -> Path:
        from .library import CACHE_DIR

        key = hashlib.sha1(json.dumps([VOICE_ID, TTS_MODEL, settings, text]).encode()).hexdigest()[:16]
        return CACHE_DIR / "voice" / f"{key}.wav"

    def clip(self, text: str, settings: dict = VOICE_SETTINGS) -> np.ndarray:
        """The voice saying text as 24 kHz audio, made once and then read from output/.cache/voice."""
        path = self.clip_path(text, settings)
        if not path.is_file():
            with urllib.request.urlopen(self._request(text, settings), timeout=30) as resp:
                pcm = resp.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(".part")
            with wave.open(str(part), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(pcm[: len(pcm) // 2 * 2])
            part.replace(path)
        with wave.open(str(path)) as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    def _eleven(self, text: str) -> None:
        import sounddevice as sd

        with urllib.request.urlopen(self._request(text), timeout=30) as resp:
            with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as out:
                rest = b""
                while chunk := resp.read(4800):  # 0.1 s of audio
                    chunk, rest = rest + chunk, b""
                    if len(chunk) % 2:
                        chunk, rest = chunk[:-1], chunk[-1:]
                    out.write(chunk)


def beep(freq: float = 880.0, seconds: float = 0.12) -> None:
    """A short tone; returns at once so listening goes on while it plays."""
    import sounddevice as sd

    t = np.arange(int(24000 * seconds)) / 24000
    tone = 0.2 * np.sin(2 * np.pi * freq * t) * np.hanning(t.size)
    sd.play(tone.astype(np.float32), 24000)


class Greeter:
    """Answers the wake word with a short "I'm here" in the language of the name used, or a beep."""

    def __init__(self, speaker: Speaker):
        self.clips: dict[str, list[tuple[str, np.ndarray]]] = {lang: [] for lang in GREETINGS}
        self.last = ""
        if not speaker.cloud:
            return
        texts = [(lang, t) for lang, lines in GREETINGS.items() for t in lines]
        if any(not speaker.clip_path(GREETING_TAG + t, GREETING_SETTINGS).is_file() for _, t in texts):
            print("Preparing her greetings (once)…", flush=True)
        for lang, text in texts:
            try:
                self.clips[lang].append((text, speaker.clip(GREETING_TAG + text, GREETING_SETTINGS)))
            except Exception as exc:  # that one is left out; a beep stands in if none are left
                print(f"  (could not prepare \"{text}\": {exc})")
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 402, 403):
                    break  # key, payment or permission: the others would fail too

    def __call__(self, wake_word: str) -> str:
        """Say a greeting (or beep) and return when it is over; returns what was said."""
        import sounddevice as sd

        lang = "zh" if re.search(r"[\u4e00-\u9fff]", wake_word) else "en"
        choices = [c for c in self.clips.get(lang, []) if c[0] != self.last] or self.clips.get(lang, [])
        if not choices:
            beep()
            return "(beep)"
        text, clip = random.choice(choices)
        self.last = text
        sd.play(clip, 24000)
        sd.wait()
        return text

    def preview(self) -> None:
        """Say every greeting once, to hear how they sound."""
        import sounddevice as sd

        for lang, clips in self.clips.items():
            for text, clip in clips:
                print(f"  {lang}: {text}", flush=True)
                sd.play(clip, 24000)
                sd.wait()
                time.sleep(0.4)
        if not any(self.clips.values()):
            print("No greetings: set ELEVENLABS_API_KEY and PIANO2MIDI_VOICE_ID.")


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

    args = argv if argv is not None else sys.argv[1:]
    if "--greetings" in args:  # just listen to her greetings
        Greeter(Speaker()).preview()
        return 0
    lib = Library()
    index_midi_folder(lib)
    lib.write_playlist()
    try:
        names = " and ".join(assistant_names(WAKE_WORDS))
        chat, label = make_chat(INSTRUCTIONS + VOICE_NOTE.format(names=names), TOOLS)
    except Exception as exc:
        print(f"Cannot start the chat model: {exc}", file=sys.stderr)
        return 2
    speaker = Speaker()
    assistant = Assistant(lib)
    assistant.speak = speaker.say  # "one moment" before a long search is spoken too
    try:
        print("Loading speech recognition and the wake word…", flush=True)
        stt, wake, mic = Transcriber(), WakeWord(), Microphone()
        greet = Greeter(speaker)
    except ImportError as exc:
        print(f"Voice needs the extras: uv pip install --python .venv/bin/python -e \".[voice]\" ({exc})",
              file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as exc:  # no network for the first download, or an unusable name
        print(f"Cannot start the wake word: {exc}", file=sys.stderr)
        return 2
    debug = "--debug" in args
    print(f"piano2midi voice · model {label} · hearing {stt.label} · voice {speaker.label}")
    said = " or ".join(f'"{w}"' for w in wake.words)
    print(f"Say {said}, then your request. Control+C quits.\n", flush=True)
    noise: deque[float] = deque(maxlen=50)  # four seconds of loudness, measured while not playing
    try:
        while True:
            frame = mic.read()
            if assistant.player is None or assistant.player.state != "playing":
                noise.append(rms(frame))
            word = wake.heard(frame)
            if not word:
                continue
            player = assistant.player
            was_playing = player is not None and player.state == "playing"
            if was_playing:
                player.pause()  # so the piano does not drown out the request
            assistant.touched_playback = False
            background = float(np.percentile(noise, 20)) if noise else 0.0  # the quiet between words
            first: list[np.ndarray] = []
            if player is None or player.sounding_for == 0:  # quiet: did you go straight on with the request?
                first = [mic.read() for _ in range(GO_ON_FRAMES)]
            if sum(rms(f) > speech_level(background) for f in first) >= 2:
                said = "nothing, you went on"  # "<name>, play ..." in one breath: keep listening
            else:
                said = greet(word)
                if player is not None and player.sounding_for:
                    time.sleep(player.sounding_for + 0.2)  # the notes already sent, and their decay
                mic.drain()  # her greeting or the piano, not the request
                first = []
            if debug:
                print(f"  (heard {word}; said {said})", flush=True)
            audio = record(mic, background, first)
            if audio is None and debug:
                print("  (no speech after the beep)", flush=True)
            try:
                text = stt(audio, lib) if audio is not None else ""
            except Exception as exc:
                print(f"  (speech recognition failed: {exc})")
                text = ""
            if audio is not None and not text and debug:
                print("  (could not make out any words)", flush=True)
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
        assistant.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
