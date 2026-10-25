"""Voice: say "Hey Jarvis", then ask for a piece. The same assistant as ./run.sh chat, with ears and a voice.

    ./run.sh voice

The wake word runs on this computer (openWakeWord). Speech recognition and the spoken replies use OpenAI
(gpt-transcribe, gpt-4o-mini-tts); without OPENAI_API_KEY, or with PIANO2MIDI_STT=local, recognition runs on
this computer (Whisper large-v3-turbo through MLX, Apple silicon) and replies use the macOS voice.
Install the extras once: uv pip install --python .venv/bin/python -e ".[voice]"
"""

from __future__ import annotations

import io
import os
import re
import statistics
import subprocess
import sys
import unicodedata
import warnings
import wave
from collections import deque

import numpy as np

warnings.filterwarnings("ignore")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 ms, the frame size openWakeWord works with
FRAME_S = FRAME / SAMPLE_RATE
WAKE_WORD = os.environ.get("PIANO2MIDI_WAKE_WORD", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("PIANO2MIDI_WAKE_THRESHOLD", "0.5"))
STT = os.environ.get("PIANO2MIDI_STT") or ("openai" if os.environ.get("OPENAI_API_KEY") else "local")
CLOUD_STT_MODEL = os.environ.get("PIANO2MIDI_STT_CLOUD_MODEL", "gpt-transcribe")
LOCAL_STT_MODEL = os.environ.get("PIANO2MIDI_STT_MODEL", "mlx-community/whisper-large-v3-turbo")
LANGUAGES = [x.strip() for x in os.environ.get("PIANO2MIDI_LANGUAGES", "").split(",") if x.strip()]  # e.g. zh,en
TTS_MODEL = os.environ.get("PIANO2MIDI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("PIANO2MIDI_TTS_VOICE", "marin")
TTS_STYLE = ("Speak like a warm, relaxed friend sitting at the piano, in a natural conversational tone, not too fast. "
             "When the text is Chinese, speak natural, native Mandarin.")
START_TIMEOUT_S = 6.0  # wait this long for speech to begin after the wake word
END_SILENCE_S = 0.8  # this much silence ends the request
MAX_REQUEST_S = 15.0

# A simplified-Chinese example sentence with an English title: Whisper then writes simplified characters and keeps
# English song names in English. The library titles are added as spelling hints.
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
FILLERS_START = ("hey jarvis", "jarvis", "please", "\u8bf7")  # qing
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
            if t.startswith(f):
                t, changed = t[len(f):].strip(), True
        for f in FILLERS_END:
            if t.endswith(f) and t != f:
                t, changed = t[: -len(f)].strip(), True
    for action, phrases in COMMANDS.items():
        if t in phrases:
            return action
    return None


def speakable(text: str) -> str:
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

        try:
            self.model = Model(wakeword_models=[name], inference_framework="onnx")
        except Exception:  # first run: fetch the model files
            download_models([name])
            self.model = Model(wakeword_models=[name], inference_framework="onnx")
        self.threshold = threshold

    def __call__(self, frame: np.ndarray) -> bool:
        return max(self.model.predict(frame).values()) >= self.threshold

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


class CloudTranscriber:
    """OpenAI speech recognition: the best accuracy for mixed Chinese and English."""

    def __init__(self, model: str = CLOUD_STT_MODEL):
        from openai import OpenAI

        self.client, self.model = OpenAI(), model
        self.label = f"OpenAI {model}"

    def __call__(self, audio: np.ndarray, lib) -> str:
        extra = {}
        if self.model == "gpt-transcribe":  # spelling hints and expected languages
            extra = {"keywords": hints(lib), **({"languages": LANGUAGES} if LANGUAGES else {})}
        request = dict(model=self.model, file=("request.wav", to_wav(audio), "audio/wav"), prompt=STT_PROMPT,
                       response_format="text")
        try:
            text = self.client.audio.transcriptions.create(**request, **extra)
        except Exception:
            if not extra:
                raise
            text = self.client.audio.transcriptions.create(**request)  # without the optional hints
        return clean_transcript(str(text), STT_PROMPT)


class LocalTranscriber:
    """Whisper on this computer through MLX (Apple silicon); the model is downloaded on first use."""

    def __init__(self, model: str = LOCAL_STT_MODEL):
        import mlx_whisper

        self.transcribe_fn, self.model = mlx_whisper.transcribe, model
        self.label = "local Whisper"
        self.transcribe_fn(np.zeros(SAMPLE_RATE, dtype=np.float32), path_or_hf_repo=model)  # load it now

    def __call__(self, audio: np.ndarray, lib) -> str:
        prompt = f"{STT_PROMPT} {'; '.join(hints(lib, 8))}".strip()
        result = self.transcribe_fn(audio, path_or_hf_repo=self.model, temperature=0.0,
                                    condition_on_previous_text=False, initial_prompt=prompt, verbose=None)
        segments = result.get("segments") or []
        if not segments or all(s.get("no_speech_prob", 0) > 0.6 for s in segments):
            return ""
        return clean_transcript(result.get("text", ""), prompt)


def make_transcriber():
    return LocalTranscriber() if STT == "local" else CloudTranscriber()


class Speaker:
    """OpenAI text-to-speech, streamed as it arrives; the macOS voice as a fallback."""

    def __init__(self):
        self.client = None
        if os.environ.get("OPENAI_API_KEY"):
            from openai import OpenAI

            self.client = OpenAI()

    def say(self, text: str) -> None:
        text = speakable(text)
        if not text:
            return
        if self.client is not None:
            try:
                return self._cloud(text)
            except Exception as exc:
                print(f"  (cloud voice failed: {exc}; using the Mac voice from now on)")
                self.client = None
        voice = ["-v", "Tingting"] if re.search(r"[\u3400-\u9fff]", text) else []
        subprocess.run(["say", *voice, text], check=False)

    def _cloud(self, text: str) -> None:
        import sounddevice as sd

        extra = {"instructions": TTS_STYLE} if TTS_MODEL.startswith("gpt-") else {}
        with self.client.audio.speech.with_streaming_response.create(
                model=TTS_MODEL, voice=TTS_VOICE, input=text, response_format="pcm", **extra) as resp:
            with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as out:
                rest = b""
                for chunk in resp.iter_bytes(4800):
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
        stt, wake, mic = make_transcriber(), WakeWord(), Microphone()
    except ImportError as exc:
        print(f"Voice needs the extras: uv pip install --python .venv/bin/python -e \".[voice]\" ({exc})",
              file=sys.stderr)
        return 2
    voice = "OpenAI " + TTS_VOICE if speaker.client else "macOS"
    print(f"piano2midi voice · model {label} · hearing {stt.label} · voice {voice}")
    print('Say "Hey Jarvis", then your request. Control+C quits.\n', flush=True)
    noise: deque[float] = deque(maxlen=25)  # about two seconds of background level
    try:
        while True:
            frame = mic.read()
            noise.append(rms(frame))
            if not wake(frame):
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
