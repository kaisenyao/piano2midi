"""Play library MIDI files on a MIDI output, for example Pianoteq on an iPad over a network MIDI session.

    ./run.sh play someone like you        # best match in the library
    ./run.sh play path/to/file.mid
    ./run.sh play --all                   # the whole playlist, in order
    ./run.sh play --ports                 # list MIDI outputs

The output is the first MIDI port whose name contains --port, PIANO2MIDI_PORT or "piano2midi".
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

from .library import Library

DEFAULT_PORT = "piano2midi"
GAP_S = 2.0  # silence between pieces
SENT = {"note_on", "note_off", "control_change"}  # no program changes: they could switch the Pianoteq preset


def output_names() -> list[str]:
    import mido

    return mido.get_output_names()


def find_port(wanted: str | None = None) -> str:
    wanted = wanted or os.environ.get("PIANO2MIDI_PORT") or DEFAULT_PORT
    names = output_names()
    for name in names:
        if wanted.casefold() in name.casefold():
            return name
    raise LookupError(f'no MIDI output matching "{wanted}" (available: {", ".join(names) or "none"}). '
                      "Check that the network MIDI session is on and the iPad is connected.")


def clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


class Player:
    """Sends MIDI files to one output port, in the foreground (play) or in a background thread (start).

    Like a media player: pause keeps the position, resume continues from it, stop goes back to the beginning.
    """

    def __init__(self, port_name: str | None = None, port=None):
        if port is None:
            import mido

            port_name = find_port(port_name)
            port = mido.open_output(port_name)
        self.port, self.port_name = port, port_name or "test"
        self.current: tuple[Path, str] | None = None  # the piece playing, paused or last played
        self.state = "stopped"  # playing, paused or stopped
        self._offset = 0.0  # seconds into the current piece while paused
        self._t0: float | None = None  # perf_counter() at 0:00 of the current piece
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._down: set[tuple[int, int]] = set()

    @property
    def position(self) -> float:
        if self.state == "playing" and self._t0 is not None:
            return max(0.0, time.perf_counter() - self._t0)
        return self._offset

    @property
    def now_playing(self) -> str | None:
        return self.current[1] if self.current and self.state == "playing" else None

    def play(self, items: list[tuple[Path, str]], from_s: float = 0.0, on_start=None) -> None:
        """Play (path, title) items in order, the first from from_s; returns when done, paused or stopped."""
        try:
            for i, (path, title) in enumerate(items):
                if i and self._stop.wait(GAP_S):
                    return
                if self._stop.is_set():
                    return
                self.current, self.state = (path, title), "playing"
                if on_start:
                    on_start(path, title)
                if not self._play_file(path, from_s if i == 0 else 0.0):
                    return  # pause() or stop() sets the state
            self.state, self._offset = "stopped", 0.0
        finally:
            self.release()

    def start(self, items: list[tuple[Path, str]], from_s: float = 0.0) -> None:
        self._halt()
        self._stop.clear()  # here, not in the thread, so an immediate pause() cannot be lost
        self.current, self.state, self._offset = items[0], "playing", from_s
        self._t0 = time.perf_counter() - from_s
        self._thread = threading.Thread(target=self.play, args=(items, from_s), daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self.state == "playing":
            position = self.position
            self._halt()
            self.state, self._offset = "paused", position

    def resume(self) -> None:
        if self.current is not None:
            self.start([self.current], self._offset if self.state == "paused" else 0.0)

    def stop(self) -> None:
        self._halt()
        self.state, self._offset = "stopped", 0.0

    def _halt(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None

    def _play_file(self, path: Path, from_s: float = 0.0) -> bool:
        """Send one file in real time from from_s seconds. True when it reached the end."""
        import mido

        pedal: dict[int, int] = {}  # sustain value per channel at from_s, restored before continuing
        self._t0 = time.perf_counter() - from_s
        at, restored = 0.0, from_s <= 0
        for msg in mido.MidiFile(path):  # merged tracks, msg.time in seconds since the previous message
            at += msg.time
            if msg.is_meta or msg.type not in SENT:
                continue
            if at < from_s:
                if msg.type == "control_change" and msg.control == 64:
                    pedal[msg.channel] = msg.value
                continue
            if not restored:
                for channel, value in pedal.items():
                    if value >= 64:
                        self.port.send(mido.Message("control_change", channel=channel, control=64, value=value))
                restored = True
            delay = self._t0 + at - time.perf_counter()
            if delay > 0 and self._stop.wait(delay):
                return False
            if self._stop.is_set():
                return False
            self.port.send(msg)
            if msg.type == "note_on" and msg.velocity > 0:
                self._down.add((msg.channel, msg.note))
            elif msg.type in {"note_on", "note_off"}:
                self._down.discard((msg.channel, msg.note))
        return True

    def release(self) -> None:
        """Lift every key that is still down and the sustain pedal."""
        import mido

        for channel, note in sorted(self._down):
            self.port.send(mido.Message("note_off", channel=channel, note=note, velocity=0))
        self._down.clear()
        for channel in range(16):
            self.port.send(mido.Message("control_change", channel=channel, control=64, value=0))


def resolve(lib: Library, words: list[str]) -> list[tuple[Path, str]]:
    """A MIDI file path, or the best library match for the words."""
    text = " ".join(words).strip()
    path = Path(text).expanduser()
    if path.suffix.lower() in {".mid", ".midi"} and path.is_file():
        return [(path, path.stem)]
    for rec in lib.search(text):
        if rec["file_exists"]:
            return [(Path(rec["midi_path"]), rec["title"])]
    return []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="piano2midi play", description="Play library MIDI files on a MIDI output.")
    p.add_argument("query", nargs="*", help="words to find in the library, or a MIDI file path")
    p.add_argument("--all", action="store_true", help="play the whole playlist in order")
    p.add_argument("--port", help=f'MIDI output name or part of it (default: PIANO2MIDI_PORT or "{DEFAULT_PORT}")')
    p.add_argument("--ports", action="store_true", help="list MIDI outputs and exit")
    args = p.parse_args(argv)

    if args.ports:
        print("\n".join(output_names()) or "No MIDI outputs found.")
        return 0
    lib = Library()
    if args.all:
        items = [(Path(r["midi_path"]), r["title"]) for r in lib.playlist()]
    elif args.query:
        items = resolve(lib, args.query)
    else:
        p.error("give words to search the library, a MIDI file, or --all")
    if not items:
        print("Nothing found in the library. Try ./run.sh library list", file=sys.stderr)
        return 1
    try:
        player = Player(args.port)
    except LookupError as exc:
        print(f"Cannot play: {exc}", file=sys.stderr)
        return 2

    def announce(path: Path, title: str) -> None:
        import mido

        print(f"▶ {title} ({clock(mido.MidiFile(path).length)})", flush=True)

    print(f"Playing on {player.port_name}. Ctrl+C to stop.")
    try:
        player.play(items, on_start=announce)
    except KeyboardInterrupt:  # play() has already lifted the keys and the pedal
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
