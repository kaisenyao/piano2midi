"""Voice: say "Hey Jarvis", then ask for a piece. The same assistant as ./run.sh chat, with ears and a voice.

    ./run.sh voice

Wake word on this computer (openWakeWord), speech recognition on this computer (Whisper large-v3-turbo through
MLX, Apple silicon), spoken replies from OpenAI text-to-speech (the macOS voice without OPENAI_API_KEY).
Install the extras once: uv pip install --python .venv/bin/python -e ".[voice]"
"""

from __future__ import annotations

import os
import re
import statistics
import subprocess
import sys
import unicodedata
import warnings
from collections import deque

import numpy as np

warnings.filterwarnings("ignore")

SAMPLE_RATE = 16000
FRAME = 1280  # 80 ms, the frame size openWakeWord works with
FRAME_S = FRAME / SAMPLE_RATE
WAKE_WORD = os.environ.get("PIANO2MIDI_WAKE_WORD", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("PIANO2MIDI_WAKE_THRESHOLD", "0.5"))
STT_MODEL = os.environ.get("PIANO2MIDI_STT_MODEL", "mlx-community/whisper-large-v3-turbo")
TTS_MODEL = os.environ.get("PIANO2MIDI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("PIANO2MIDI_TTS_VOICE", "coral")
TTS_STYLE = "Warm, relaxed and natural, like a friend sitting at the piano. Not too fast."
START_TIMEOUT_S = 6.0  # wait this long for speech to begin after the wake word
END_SILENCE_S = 0.8  # this much silence ends the request
MAX_REQUEST_S = 15.0

VOICE_NOTE = """

Your replies are spoken aloud. Use one short sentence when you can, never lists, links or file paths."""

# Short commands handled without the model. Chinese phrases are written as escapes because the repository is
# English-only; the pinyin is in the comments.
COMMANDS = {
    "pause": {"stop", "pause", "stop playing", "stop it", "enough", "that's enough",
              "\u505c", "\u505c\u4e00\u4e0b", "\u6682\u505c", "\u505c\u6b62", "\u591f\u4e86", "\u522b\u5f39\u4e86"},
    # ting, ting yixia, zanting, tingzhi, gou le, bie tan le
    "resume": {"continue", "keep playing", "resume", "go on",
               "\u7ee7\u7eed", "\u7ee7\u7eed\u5f39", "\u63a5\u7740\u5f39"},
    # jixu, jixu tan, jiezhe tan
    "restart": {"start over", "from the beginning", "play it again",
                "\u4ece\u5934\u5f39", "\u4ece\u5934\u5f00\u59cb", "\u91cd\u65b0\u5f39"},
    # congtou tan, congtou kaishi, chongxin tan
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


class Transcriber:
    """Local Whisper through MLX; the model is the one already in the Hugging Face cache."""

    def __init__(self, model: str = STT_MODEL):
        import mlx_whisper

        self.transcribe_fn, self.model = mlx_whisper.transcribe, model
        self(np.zeros(SAMPLE_RATE, dtype=np.float32))  # load the model now, not on the first request

    def __call__(self, audio: np.ndarray) -> str:
        result = self.transcribe_fn(audio, path_or_hf_repo=self.model, temperature=0.0,
                                    condition_on_previous_text=False, verbose=None)
        segments = result.get("segments") or []
        if not segments or all(s.get("no_speech_prob", 0) > 0.6 for s in segments):
            return ""
        return result.get("text", "").strip()


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


def handle_command(assistant, action: str) -> None:
    if action == "pause":
        player = assistant.player
        already_paused = player is not None and player.state == "paused"  # paused while listening
        assistant.pause()
        if already_paused:
            from .play import clock

            print(f"  ⏸ {player.current[1]}  {clock(player.position)}", flush=True)
    else:
        result = assistant.play(from_start=action == "restart")
        if "error" in result:
            print(f"  {result['error']}")


def main(argv: list[str] | None = None) -> int:
    from .chat import INSTRUCTIONS, TOOLS, Assistant, index_midi_folder
    from .library import Library
    from .llm import make_chat

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
    voice = "OpenAI " + TTS_VOICE if speaker.client else "macOS"
    print(f'piano2midi voice · model {label} · voice {voice}\nSay "Hey Jarvis", then your request. '
          "Control+C quits.\n", flush=True)
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
            text = stt(audio) if audio is not None else ""
            if text:
                print(f"you> {text}", flush=True)
                action = match_command(text)
                if action:
                    handle_command(assistant, action)
                else:
                    try:
                        answer = assistant.ask(chat, text)
                    except Exception as exc:
                        chat.reset()
                        print(f"Model error: {exc}")
                        answer = ""
                    if answer:
                        print(f"assistant> {answer}\n", flush=True)
                        speaker.say(answer)
            if was_playing and not assistant.touched_playback and assistant.player.state == "paused":
                assistant.player.resume()
            mic.drain()
            wake.reset()
    except KeyboardInterrupt:
        assistant.stop()
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
