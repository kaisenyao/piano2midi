"""Voice: say "Hey Jasmine" (or the Chinese name), then ask for a piece. The same assistant as ./run.sh chat,
with ears and a voice.

    ./run.sh voice

The wake words are heard on this computer by sherpa-onnx keyword spotting, which takes any name without
training; set them with PIANO2MIDI_WAKE_WORDS (comma-separated). Speech recognition uses OpenAI gpt-transcribe
(OPENAI_API_KEY). Replies are spoken by ElevenLabs (ELEVENLABS_API_KEY and PIANO2MIDI_VOICE_ID), or by the
macOS voice without them. Pause after the name and she says she is listening; or say the name and the request
in one breath. After her reply you may go on without the name, unless the piano plays. Add --debug to see
which wake word was heard, or --greetings to hear every greeting once.
Install the extras once: uv pip install --python .venv/bin/python -e ".[voice]"
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import queue
import random
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import warnings
import wave
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from .playback_commands import WAKE_WORDS, assistant_names, is_chinese, strip_fillers

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

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
# voice and kept in output/.cache/voice. Without ElevenLabs the macOS voice says them.
GREETINGS = {
    "zh": ["诶，我在呢！", "来啦～想听点什么呀？", "嗯嗯，你说～"],
    "en": ["Hi! I'm here.", "Sure, I'm listening!", "Mm-hmm, go ahead!"],
}
# How the greetings are said, picked by ear by the owner: freer than replies, with some character, quick.
GREETING_SETTINGS = {**VOICE_SETTINGS, "stability": 0.25, "style": 0.4, "speed": 1.15}
# Silero voice activity detection tells speech from silence and the piano.
VAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
GO_ON_FRAMES = 8  # after the wake word, up to 0.64 s to tell whether you go straight on with the request
PRE_ROLL_FRAMES = 20  # in that case the request is sent from 1.6 s before the wake word was heard, name included
START_TIMEOUT_S = 6.0  # after her greeting, wait this long for the request to begin
END_SILENCE_S = 0.7  # this much silence (after the detector's own 0.3 s) ends the request
MAX_REQUEST_S = 15.0
FOLLOW_UP_S = 8.0  # after her reply you may go on without her name, unless the piano is playing

# For Chinese speakers (PIANO2MIDI_LANGUAGES contains zh): a simplified-Chinese example sentence with an English
# title, so the transcript uses simplified characters and keeps English song names in English.
STT_PROMPT = "以下是普通话和英语的句子。我想听 Adele 的 Someone Like You。暂停。继续。"

PROMPT_ECHOES = {STT_PROMPT, STT_PROMPT.split("。", 1)[-1], "暂停。继续。"}
# Spelling hints for Chinese speakers besides the library's names: the commands that are easiest to mishear.
STT_COMMAND_HINTS = ["暂停", "停止播放", "继续播放", "从头弹"]

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
    """The default input in 80 ms frames. macOS can break a running stream when audio devices change; then
    no audio arrives, and the microphone is opened again. Waiting for audio never blocks Control+C."""

    def __init__(self):
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)  # 16 s while she is busy
        self.stream = None
        self.open()

    def open(self) -> None:
        import sounddevice as sd

        if self.stream is not None:
            self.stream.close()
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME,
                                     callback=self._received)
        self.stream.start()

    def _received(self, data, *_) -> None:
        with contextlib.suppress(queue.Full):
            self.frames.put_nowait(data[:, 0].copy())

    def read(self) -> np.ndarray:
        try:
            return self.frames.get(timeout=2)
        except queue.Empty:
            print("  (the microphone stopped; opening it again)", flush=True)
            self.open()
        try:
            return self.frames.get(timeout=5)
        except queue.Empty:
            raise RuntimeError("the microphone sends no audio; check the input in System Settings > Sound") from None

    def drain(self) -> None:
        """Drop audio that piled up while we were busy or speaking."""
        with contextlib.suppress(queue.Empty):
            while True:
                self.frames.get_nowait()


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
    """Her voice: ElevenLabs, arriving while it is made; the macOS voice without it, or when it fails."""

    def __init__(self):
        self.key = os.environ.get("ELEVENLABS_API_KEY", "")
        self.cloud = bool(self.key and VOICE_ID)
        self.label = f"ElevenLabs {TTS_MODEL}" if self.cloud else "macOS (no ELEVENLABS_API_KEY / PIANO2MIDI_VOICE_ID)"

    def stream(self, text: str) -> Iterator[np.ndarray]:
        """text as pieces of 24 kHz PCM, the first as soon as it arrives."""
        text = speakable(text)
        if not text:
            return
        if self.cloud:
            sent = False
            try:
                with urllib.request.urlopen(self._request(text), timeout=30) as resp:
                    rest = b""
                    while chunk := resp.read(4800):  # 0.1 s of audio
                        chunk, rest = rest + chunk, b""
                        if len(chunk) % 2:
                            chunk, rest = chunk[:-1], chunk[-1:]
                        if chunk:
                            sent = True
                            yield np.frombuffer(chunk, dtype="<i2")
                return
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:200]
                print(f"  (ElevenLabs error {exc.code}: {detail}; using the Mac voice)", flush=True)
                if exc.code in (401, 402, 403):  # key, payment or permission: stop trying
                    self.cloud = False
            except Exception as exc:  # network trouble: the Mac voice for this sentence only
                if sent:
                    print(f"  (ElevenLabs stopped: {exc})", flush=True)
                    return
                print(f"  (ElevenLabs failed: {exc}; using the Mac voice)", flush=True)
        yield mac_voice(text)

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


def mac_voice(text: str) -> np.ndarray:
    """The macOS voice saying text, as 24 kHz PCM; Chinese in a Chinese voice."""
    from .audio import ffmpeg_exe

    voice = ["-v", "Tingting"] if is_chinese(text) else []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "voice.aiff"
        subprocess.run(["say", *voice, "-o", str(path), text], check=True, timeout=60)
        pcm = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-ar", "24000", "-ac", "1",
                              "-f", "s16le", "-"], check=True, capture_output=True, timeout=60).stdout
    return np.frombuffer(pcm, dtype="<i2")


class Greeter:
    """Her ready-made greetings, made once with her ElevenLabs voice."""

    def __init__(self, speaker: Speaker):
        self.clips: dict[str, list[tuple[str, np.ndarray]]] = {lang: [] for lang in GREETINGS}
        self.last = ""
        if not speaker.cloud:
            return
        texts = [(lang, t) for lang, lines in GREETINGS.items() for t in lines]
        if any(not speaker.clip_path(t, GREETING_SETTINGS).is_file() for _, t in texts):
            print("Preparing her greetings (once)…", flush=True)
        for lang, text in texts:
            try:
                self.clips[lang].append((text, speaker.clip(text, GREETING_SETTINGS)))
            except Exception as exc:  # that one is left out
                print(f"  (could not prepare \"{text}\": {exc})")
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 402, 403):
                    break  # key, payment or permission: the others would fail too

    def pick(self, wake_word: str) -> tuple[str, np.ndarray] | None:
        """A greeting in the language of the name used, not the last one again; None without clips."""
        clips = self.clips["zh" if is_chinese(wake_word) else "en"]
        choices = [c for c in clips if c[0] != self.last] or clips
        if not choices:
            return None
        picked = random.choice(choices)
        self.last = picked[0]
        return picked

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
    "zh": ["这首弹完啦，希望你喜欢。", "余韵还在呢，想再听一首吗？", "谢谢你听我弹完，还想听点什么？"],
    "en": ["That was lovely to play for you. Fancy another?", "And that's the last note. I hope you enjoyed it.",
           "Thanks for listening. What shall we play next?"],
}
GOODBYES = {"再见", "拜拜", "goodbye", "bye"}


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
            print(f"  ⏸ {player.current[1]}  {clock(player.position)}", flush=True)
        else:
            try:
                # start checks the route, says the reply, then resumes; no second check or second reply.
                player.start([player.current], from_s=player.position, on_ready=lambda: say(answer))
            except LookupError:
                say(assistant.playback_reply({"error": "playback_connection_unavailable"}))
    else:
        say(answer)


class SessionEnded(Exception):
    """Unwind a conversation that has ended, at the next safe step of a task."""


class PauseControl:
    """The conversation's clock. The phone can pause it: long tasks then wait at their next step,
    and the piano pauses where it is. Mac Voice is never paused."""

    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.paused = self.closed = False
        self.epoch = 0
        self.player = None
        self.resume_player = False
        self.paused_at = None
        self.elapsed_pause = 0.0
        self.speech_until = 0.0  # on this clock: when the phone will have played what was sent

    def checkpoint(self):
        with self.condition:
            self.condition.wait_for(lambda: not self.paused or self.closed)
            if self.closed:
                raise SessionEnded()

    def guard(self, fn):
        def guarded(*args, **kwargs):
            self.checkpoint()
            result = fn(*args, **kwargs)
            self.checkpoint()
            return result
        return guarded

    def now(self):
        with self.condition:
            return (self.paused_at if self.paused else time.monotonic()) - self.elapsed_pause

    def wait_for_speech(self):
        """Return once the phone has played the speech sent so far (with a little room for the network)."""
        with self.condition:
            while not self.closed and (left := self.speech_until + 0.1 - self.now()) > 0:
                self.condition.wait(None if self.paused else left)
            if self.closed:
                raise SessionEnded()

    def set_paused(self, paused):
        with self.condition:
            if self.closed or self.paused == paused:
                return
            if paused:
                self.paused = True
                self.epoch += 1
                self.paused_at = time.monotonic()
                self.resume_player = self.player is not None and self.player.state == "playing"
                if self.resume_player:
                    self.player.pause()  # keeps the position; the notes already sent still play
            else:
                self.elapsed_pause += time.monotonic() - self.paused_at
                self.paused_at = None
                self.paused = False
                try:
                    if self.resume_player:
                        self.player.resume()
                finally:
                    self.resume_player = False
                    self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
            if self.player is not None:
                self.player.stop()


class VoiceParts:
    """The slow parts, loaded once and used by one conversation after another."""

    def __init__(self):
        self.stt, self.wake, self.speech = Transcriber(), WakeWord(), Speech()
        self.speaker = Speaker()
        self.greeter = Greeter(self.speaker)

    def describe(self, model: str) -> str:
        return f"piano2midi voice · model {model} · hearing {self.stt.label} · voice {self.speaker.label}"


def voice_chat():
    """The chat model, told her names and that her replies are spoken."""
    from .chat import INSTRUCTIONS, TOOLS
    from .llm import make_chat

    return make_chat(INSTRUCTIONS + VOICE_NOTE.format(names=" and ".join(assistant_names(WAKE_WORDS))), TOOLS)


class Conversation:
    """One conversation with her, fed 80 ms microphone frames: from the Mac's own microphone or the phone's.

    out decides where her voice goes. status(state) reports what she is doing; speak(pieces) plays 24 kHz PCM
    pieces; turn_end(conversation, sleep) follows each reply once it has been played, and then calls
    conversation.sleep() (wait for her name again) or conversation.resume() (listen on without it).
    """

    def __init__(self, assistant, chat, lib, parts: VoiceParts, out, control: PauseControl, debug: bool = False):
        self.assistant, self.chat, self.lib, self.out, self.control, self.debug = (
            assistant, chat, lib, out, control, debug)
        self.stt, self.wake, self.speech = parts.stt, parts.wake, parts.speech
        self.speaker, self.greeter = parts.speaker, parts.greeter
        self.wake.reset()
        self.speech.reset()
        assistant.speak = self.say  # "one moment" and the introductions are spoken too
        self.recent: deque[np.ndarray] = deque(maxlen=PRE_ROLL_FRAMES)  # for one-breath requests
        self.frames: list[np.ndarray] = []
        self.state = "waiting"
        self.started = False
        self.silent = self.elapsed = 0.0
        self.dialogue_until = 0.0
        self.was_playing = False
        self.woken, self.go_on = None, 0
        self.follow_up_s = FOLLOW_UP_S
        self.language, self.last_remark = "zh", ""

    def status(self, state: str) -> None:
        self.state = state
        self.out.status(state)

    def speak(self, pieces) -> None:
        """Her voice out; a failure loses this sentence only."""
        try:
            self.control.checkpoint()
            self.out.speak(pieces)
        except SessionEnded:
            raise
        except Exception:
            traceback.print_exc()

    def say(self, text: str) -> None:
        if text:
            self.speak(self.speaker.stream(text))

    def end_turn(self, sleep: bool | None = None) -> None:
        """Her reply is out. Listen on without her name, but not while the piano plays: its sound would be
        taken for speech."""
        if sleep is None:
            player = self.assistant.player
            sleep = player is not None and player.state == "playing"
        if self.state != "speaking":
            self.status("speaking")
        self.out.turn_end(self, sleep)

    def greet(self, word: str) -> None:
        """Her name and a pause: a ready-made greeting, then she listens for the request."""
        self.woken = None
        self.status("speaking")
        picked = self.greeter.pick(word)
        line = picked[0] if picked else GREETINGS["zh" if is_chinese(word) else "en"][0]
        print(f"assistant> {line}", flush=True)
        if picked:
            self.speak([picked[1]])
        else:  # no ElevenLabs greetings: the Mac voice says one
            self.say(line)
        self.follow_up_s = START_TIMEOUT_S  # the request should start soon
        self.end_turn(sleep=False)

    def remark(self) -> None:
        """A piece ended on its own: say so once, in the language of the last request."""
        line = finished_remark(self.assistant.player, self.language, self.last_remark)
        if line:
            self.last_remark = line
            self.status("speaking")
            print(f"assistant> {line}\n", flush=True)
            self.say(line)
            self.follow_up_s = FOLLOW_UP_S
            self.end_turn()

    def begin(self) -> None:
        """Start listening to a request; the piano pauses meanwhile."""
        player = self.assistant.player
        self.was_playing = self.was_playing or (player is not None and player.state == "playing")
        if player is not None:
            player.completed = None  # a new request takes priority over a closing remark
        if self.was_playing:
            player.pause()
        self.assistant.touched_playback = False
        self.woken = None
        self.frames = list(self.recent)
        self.started = False
        self.silent = self.elapsed = 0.0
        self.status("listening")

    def resume(self) -> None:
        """Listen on without her name, for a while."""
        self.frames.clear()
        self.recent.clear()
        self.speech.reset()
        self.wake.reset()
        self.dialogue_until = self.control.now() + self.follow_up_s
        self.status("conversation")

    def finish_playback(self, answer: str = "") -> None:
        self.control.checkpoint()
        finish_interaction(self.assistant, self, answer, self.was_playing)
        self.was_playing = False

    def sleep(self) -> None:
        """Wait for her name again; a piece paused for the request plays on."""
        self.finish_playback()
        self.frames.clear()
        self.recent.clear()
        self.speech.reset()
        self.wake.reset()
        self.woken, self.dialogue_until = None, 0.0
        self.status("waiting")

    def frame(self, pcm: np.ndarray) -> None:
        """Feed 80 ms of 16 kHz mono PCM16."""
        self.control.checkpoint()
        if len(pcm) != FRAME:
            raise ValueError("expected 1280 samples of 16 kHz mono PCM16")
        if self.state in {"thinking", "speaking"}:
            return  # her own voice, or frames sent before the phone heard she was busy
        self.recent.append(pcm)
        speaking = self.speech(pcm)
        if self.state == "conversation":
            if self.control.now() > self.dialogue_until:
                self.sleep()
            elif speaking:
                self.begin()
            else:
                return
        if self.state == "waiting":
            word = self.wake.heard(pcm)
            if not word:
                self.remark()
                return
            if self.debug:
                print(f"  (heard {word})", flush=True)
            self.begin()
            if speaking:  # does the request follow in the same breath?
                self.woken, self.go_on = word, 0
            else:
                self.greet(word)
            return
        if self.woken and self.go_on < GO_ON_FRAMES:  # past it: one breath, name and request sent together
            if not speaking:
                self.greet(self.woken)
                return
            self.go_on += 1
        self.frames.append(pcm)
        self.elapsed += FRAME_S
        if speaking:
            self.started, self.silent = True, 0.0
        else:
            self.silent += FRAME_S
        if self.elapsed >= MAX_REQUEST_S or (self.silent >= END_SILENCE_S and (self.started or self.elapsed >= 1.2)):
            self.respond()

    def respond(self) -> None:
        self.control.checkpoint()
        self.status("thinking")
        try:
            text = self.stt(np.concatenate(self.frames).astype(np.float32) / 32768, self.lib)
            print(f"you> {text}" if text else "  (nothing clear was heard)", flush=True)
        except Exception as exc:  # this request is lost, not the conversation
            print(f"  (speech recognition failed: {exc})", flush=True)
            text = ""
        self.control.checkpoint()
        request = strip_fillers(text)
        if not request:
            if self.woken:  # only her name, in one breath: greet and listen, as after a pause
                self.greet(self.woken)
            else:  # nothing to answer: wait for her name again
                self.sleep()
            return
        self.language = "zh" if is_chinese(text) else "en"
        if request in GOODBYES:
            line = "再见，需要我时再叫我。" if self.language == "zh" else "Bye! Call me when you need me."
            print(f"assistant> {line}\n", flush=True)
            self.say(line)
            self.end_turn(sleep=True)
            return
        answer = ""
        try:
            answer = self.assistant.ask(self.chat, text)
        except SessionEnded:
            raise
        except Exception as exc:  # start the chat afresh; a paused piece plays on
            print(f"Model error: {exc}", flush=True)
            self.chat.reset()
        self.finish_playback(answer)
        self.follow_up_s = FOLLOW_UP_S
        self.end_turn()

    def close(self) -> None:
        try:
            self.assistant.close()
        finally:
            self.lib.db.close()


class Speakers:
    """The Mac's own speakers. She speaks while her voice is being made; afterwards the microphone's backlog,
    her own voice, is dropped before listening goes on."""

    def __init__(self, mic: Microphone):
        self.mic = mic

    def status(self, state: str) -> None:
        pass

    def speak(self, pieces) -> None:
        import sounddevice as sd

        with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as out:
            for pcm in pieces:
                out.write(np.ascontiguousarray(pcm, dtype="<i2").tobytes())

    def turn_end(self, conversation: Conversation, sleep: bool) -> None:
        self.mic.drain()
        if sleep:
            conversation.sleep()
        else:
            conversation.resume()


def main(argv: list[str] | None = None) -> int:
    from .chat import Assistant, index_midi_folder
    from .library import Library

    args = argv if argv is not None else sys.argv[1:]
    if "--greetings" in args:  # just listen to her greetings
        Greeter(Speaker()).preview()
        return 0

    lib = Library()
    index_midi_folder(lib)
    try:
        chat, label = voice_chat()
    except Exception as exc:
        print(f"Cannot start the chat model: {exc}", file=sys.stderr)
        return 2
    try:
        print("Loading speech recognition and the wake word…", flush=True)
        parts, mic = VoiceParts(), Microphone()
    except ImportError as exc:
        print(f"Voice needs the extras: uv pip install --python .venv/bin/python -e \".[voice]\" ({exc})",
              file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as exc:  # no network for the first download, or an unusable name
        print(f"Cannot start the wake word: {exc}", file=sys.stderr)
        return 2
    conversation = Conversation(Assistant(lib), chat, lib, parts, Speakers(mic), PauseControl(),
                                debug="--debug" in args)
    print(parts.describe(label))
    said = " or ".join(f'"{w}"' for w in parts.wake.words)
    print(f"Say {said}, then your request. Control+C quits.\n", flush=True)
    try:
        while True:
            conversation.frame(mic.read())
    except KeyboardInterrupt:
        conversation.assistant.stop()
        print()
        return 0
    except RuntimeError as exc:  # the microphone could not be opened again
        print(f"Voice stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        conversation.close()


if __name__ == "__main__":
    raise SystemExit(main())
