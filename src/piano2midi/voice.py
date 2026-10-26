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
import urllib.error
import urllib.request
import warnings
import wave
from collections import deque
from pathlib import Path

import numpy as np

from .playback_commands import WAKE_WORDS, assistant_names, strip_fillers

warnings.filterwarnings("ignore")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 ms of audio per read
FRAME_S = FRAME / SAMPLE_RATE
WAKE_THRESHOLD = float(os.environ.get("PIANO2MIDI_WAKE_THRESHOLD", "0.2"))  # lower hears more, wakes more by mistake
WAKE_MODEL = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"  # Chinese and English, about 33 MB to download
WAKE_MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{WAKE_MODEL}.tar.bz2"
WAKE_PARTS = {"encoder": "encoder-epoch-13-avg-2-chunk-16-left-64.onnx",
              "decoder": "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
              "joiner": "joiner-epoch-13-avg-2-chunk-16-left-64.onnx",
              "tokens": "tokens.txt", "lexicon": "en.phone"}
WAKE_SCORE = 2.0  # how strongly the spotter favors the wake words; 2.0 did best on the owner's voice with piano
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
    "zh": ["\u8bf6\uff0c\u6211\u5728\u5462\uff01", "\u6765\u5566\uff5e\u60f3\u542c\u70b9\u4ec0\u4e48\u5440\uff1f", "\u55ef\u55ef\uff0c\u4f60\u8bf4\uff5e"],
    # ei, wo zai ne! / lai la, xiang ting dian shenme ya? / en en, ni shuo
    "en": ["Hi! I'm here.", "Sure, I'm listening!", "Mm-hmm, go ahead!"],
}
# How the greetings are said, picked by ear by the owner: freer than replies, with some character, quick.
GREETING_SETTINGS = {**VOICE_SETTINGS, "stability": 0.25, "style": 0.4, "speed": 1.15}
# Silero voice activity detection tells speech from silence, the piano, and beeps.
VAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
GO_ON_FRAMES = 8  # after the wake word, up to 0.64 s to tell whether you go straight on with the request
PRE_ROLL_FRAMES = 20  # in that case the request is sent from 1.6 s before the wake word was heard, name included
START_TIMEOUT_S = 6.0  # wait this long for speech to begin after the wake word
END_SILENCE_S = 0.7  # this much silence (after the detector's own 0.3 s) ends the request
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
    print("Downloading the wake word model (about 33 MB, once)\u2026", flush=True)
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


def download(url: str, path: Path) -> Path:
    """Fetch url to path once."""
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=60) as resp:
            part.write_bytes(resp.read())
        part.replace(path)
    return path


class Speech:
    """Tells whether someone is speaking, frame by frame (Silero voice activity detection via sherpa-onnx)."""

    def __init__(self):
        import sherpa_onnx

        from .library import CACHE_DIR

        model = download(VAD_URL, CACHE_DIR / "models" / "silero_vad.onnx")
        silero = sherpa_onnx.SileroVadModelConfig(model=str(model), threshold=0.5, min_silence_duration=0.3,
                                                  min_speech_duration=0.2)
        self.vad = sherpa_onnx.VoiceActivityDetector(sherpa_onnx.VadModelConfig(silero_vad=silero,
                                                                                 sample_rate=SAMPLE_RATE), 30)

    def __call__(self, frame: np.ndarray) -> bool:
        """Feed one frame of 16-bit audio; True while speech is going on."""
        self.vad.accept_waveform(frame.astype(np.float32) / 32768.0)
        while not self.vad.empty():  # finished stretches of speech are not needed
            self.vad.pop()
        return self.vad.is_speech_detected()

    def reset(self) -> None:
        self.vad.reset()


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


def record(mic, speech: Speech, head: list[np.ndarray] | None = None) -> np.ndarray | None:
    """Record one request until it is followed by a short silence. Float32 at 16 kHz, or None if nobody spoke.

    With head (audio already heard, speech under way) it goes on from there; otherwise it first waits for speech.
    """
    frames, started, silent, elapsed = list(head or []), bool(head), 0.0, 0.0
    while elapsed < MAX_REQUEST_S:
        frame = mic.read()
        elapsed += FRAME_S
        frames.append(frame)
        if speech(frame):
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
        if any(not speaker.clip_path(t, GREETING_SETTINGS).is_file() for _, t in texts):
            print("Preparing her greetings (once)\u2026", flush=True)
        for lang, text in texts:
            try:
                self.clips[lang].append((text, speaker.clip(text, GREETING_SETTINGS)))
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


FINISH_LINES = {
    "zh": ["\u8fd9\u9996\u5f39\u5b8c\u5566\uff0c\u5e0c\u671b\u4f60\u559c\u6b22\u3002", "\u4f59\u97f5\u8fd8\u5728\u5462\uff0c\u60f3\u518d\u542c\u4e00\u9996\u5417\uff1f", "\u8c22\u8c22\u4f60\u542c\u6211\u5f39\u5b8c\uff0c\u8fd8\u60f3\u542c\u70b9\u4ec0\u4e48\uff1f"],
    "en": ["That was lovely to play for you. Fancy another?", "And that's the last note. I hope you enjoyed it.",
           "Thanks for listening. What shall we play next?"],
}


def finished_remark(player, language: str, previous: str = "") -> str | None:
    """Consume a natural finish once, one second after the scheduled audio ends."""
    if player is None or player.completed is None:
        return None
    if player.state != "stopped":
        player.completed = None
        return None
    _, ended = player.completed
    if time.perf_counter() < ended + 1.0:
        return None
    player.completed = None
    choices = FINISH_LINES[language]
    return random.choice([line for line in choices if line != previous])


def finish_interaction(assistant, speaker, answer: str, was_playing: bool) -> None:
    """Check automatic resumption before speaking a final reply, so a fault is reported once."""
    def say(text):
        if text:
            print(f"assistant> {text}\n", flush=True)
            speaker.say(text)

    player = assistant.player
    if was_playing and player is not None and player.state == "paused":
        if assistant.touched_playback:
            say(answer)
            from .play import clock
            print(f"  \u23f8 {player.current[1]}  {clock(player.position)}", flush=True)
        else:
            try:
                # start checks the route, says the reply, then resumes; no second check or second reply.
                player.start([player.current], from_s=player.position, on_ready=lambda: say(answer))
            except LookupError:
                say(assistant.playback_reply({"error": "playback_connection_unavailable"}))
    else:
        say(answer)


def main(argv: list[str] | None = None) -> int:
    from .chat import INSTRUCTIONS, TOOLS, Assistant, index_midi_folder
    from .library import Library
    from .llm import make_chat

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
        print("Loading speech recognition and the wake word\u2026", flush=True)
        stt, wake, speech, mic = Transcriber(), WakeWord(), Speech(), Microphone()
        greet = Greeter(speaker)
    except ImportError as exc:
        print(f"Voice needs the extras: uv pip install --python .venv/bin/python -e \".[voice]\" ({exc})",
              file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as exc:  # no network for the first download, or an unusable name
        print(f"Cannot start the wake word: {exc}", file=sys.stderr)
        return 2
    debug = "--debug" in args
    print(f"piano2midi voice \xb7 model {label} \xb7 hearing {stt.label} \xb7 voice {speaker.label}")
    said = " or ".join(f'"{w}"' for w in wake.words)
    print(f"Say {said}, then your request. Control+C quits.\n", flush=True)
    recent: deque[np.ndarray] = deque(maxlen=PRE_ROLL_FRAMES)  # the last moments, for one-breath requests

    def hear(head: list[np.ndarray] | None = None) -> str:
        """Record a request (see record) and turn it into text; "" if nothing was made out."""
        audio = record(mic, speech, head)
        if audio is None:
            if debug:
                print("  (no speech after the name)", flush=True)
            return ""
        try:
            text = stt(audio, lib)
        except Exception as exc:
            print(f"  (speech recognition failed: {exc})")
            return ""
        if not text and debug:
            print("  (could not make out any words)", flush=True)
        return text

    finish_language, last_finish = "zh", ""
    try:
        while True:
            frame = mic.read()
            recent.append(frame)
            speaking = speech(frame)
            word = wake.heard(frame)
            if not word:
                remark = finished_remark(assistant.player, finish_language, last_finish)
                if remark:
                    last_finish = remark
                    print(f"assistant> {remark}\n", flush=True)
                    speaker.say(remark)
                    mic.drain()
                    wake.reset()
                    speech.reset()
                    recent.clear()
                continue
            player = assistant.player
            if player is not None:
                player.completed = None  # a new request takes priority over a pending remark
            was_playing = player is not None and player.state == "playing"
            if was_playing:
                player.pause()  # so the piano does not drown out the request
            assistant.touched_playback = False
            more: list[np.ndarray] = []
            while speaking and len(more) < GO_ON_FRAMES:  # does the speech go on after the name?
                more.append(mic.read())
                speaking = speech(more[-1])
            text = ""
            if speaking:  # "<name>, play ..." in one breath: send it all, name included, and keep listening
                if debug:
                    print(f"  (heard {word}; you went on)", flush=True)
                text = hear(list(recent) + more)
                speaking = bool(strip_fillers(text))  # nothing but the name after all: answer and listen
            if not speaking:  # a pause after the name: answer, then listen
                said = greet(word)
                if debug:
                    print(f"  (heard {word}; said {said})", flush=True)
                mic.drain()  # her greeting, not the request
                speech.reset()
                text = hear()
            answer = ""
            if text:
                finish_language = "zh" if re.search(r"[\u3400-\u9fff]", text) else "en"
                print(f"you> {text}", flush=True)
                try:
                    answer = assistant.ask(chat, text)
                except Exception as exc:
                    chat.reset()
                    print(f"Model error: {exc}")
                    answer = ""
            finish_interaction(assistant, speaker, answer, was_playing)
            mic.drain()
            wake.reset()
            speech.reset()
            recent.clear()
    except KeyboardInterrupt:
        assistant.stop()
        print()
        assistant.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
