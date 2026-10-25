"""Play library MIDI files on a MIDI output, for example Pianoteq on an iPad over a network MIDI session.

    ./run.sh play someone like you        # best match in the library
    ./run.sh play path/to/file.mid
    ./run.sh play --all                   # the whole playlist, in order
    ./run.sh play --ports                 # list MIDI outputs

The output is the first MIDI port whose name contains --port, PIANO2MIDI_PORT or "piano2midi".

On macOS each message leaves a little early (PIANO2MIDI_LEAD seconds, default 1) together with the time it
should sound. A network MIDI session carries that time along, so the receiver plays every note on schedule even
when Wi-Fi delivers it late. Pause and stop take effect after the same lead.
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
DEFAULT_LEAD_S = 1.0  # how early each message leaves on macOS
GAP_S = 2.0  # silence between pieces
KEEPALIVE_S = 0.02  # with keepalive on, send MIDI Active Sensing at least this often so a Wi-Fi link stays awake
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


def default_lead() -> float:
    text = os.environ.get("PIANO2MIDI_LEAD", "").strip()
    try:
        return max(0.0, float(text)) if text else DEFAULT_LEAD_S
    except ValueError:
        return DEFAULT_LEAD_S


def clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


class CoreMidiOut:
    """A macOS MIDI output that stamps every message with the time it should sound.

    mido cannot pass these times on, so this calls CoreMIDI directly. Times are perf_counter() values.
    """

    def __init__(self, name: str):
        import ctypes
        from ctypes import POINTER, byref, c_char_p, c_int32, c_uint32, c_uint64, c_ulong, c_void_p

        import rtmidi

        cm = ctypes.CDLL("/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p
        cm.MIDIClientCreate.argtypes = [c_void_p, c_void_p, c_void_p, POINTER(c_uint32)]
        cm.MIDIClientCreate.restype = c_int32
        cm.MIDIOutputPortCreate.argtypes = [c_uint32, c_void_p, POINTER(c_uint32)]
        cm.MIDIOutputPortCreate.restype = c_int32
        cm.MIDIGetNumberOfDestinations.restype = c_ulong
        cm.MIDIGetDestination.argtypes = [c_ulong]
        cm.MIDIGetDestination.restype = c_uint32
        cm.MIDIPacketListInit.argtypes = [c_void_p]
        cm.MIDIPacketListInit.restype = c_void_p
        cm.MIDIPacketListAdd.argtypes = [c_void_p, c_ulong, c_void_p, c_uint64, c_ulong, c_void_p]
        cm.MIDIPacketListAdd.restype = c_void_p
        cm.MIDISend.argtypes = [c_uint32, c_uint32, c_void_p]
        cm.MIDISend.restype = c_int32
        cm.MIDIPortDispose.argtypes = [c_uint32]
        cm.MIDIClientDispose.argtypes = [c_uint32]
        system.mach_absolute_time.restype = c_uint64

        class Timebase(ctypes.Structure):
            _fields_ = [("numer", c_uint32), ("denom", c_uint32)]

        timebase = Timebase()
        system.mach_timebase_info(byref(timebase))
        self._ticks_per_s = 1e9 * timebase.denom / timebase.numer
        self._cm, self._host_time, self._ubyte = cm, system.mach_absolute_time, ctypes.c_ubyte

        # rtmidi lists the CoreMIDI destinations in CoreMIDI's own order, so a name's position is its index.
        # (mido's list can be in another order: it puts names that are also inputs first.)
        rt = rtmidi.MidiOut()
        names = rt.get_ports()
        rt.delete()
        if name not in names or len(names) != cm.MIDIGetNumberOfDestinations():
            raise LookupError(f'CoreMIDI has no output "{name}"')
        self._dest = cm.MIDIGetDestination(names.index(name))

        def cfstring(text: str):
            return cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)  # UTF-8

        self._client, self._port = c_uint32(), c_uint32()
        if cm.MIDIClientCreate(cfstring("piano2midi"), None, None, byref(self._client)) != 0 or \
                cm.MIDIOutputPortCreate(self._client, cfstring("piano2midi out"), byref(self._port)) != 0:
            raise OSError("CoreMIDI did not open an output")
        self._buffer = ctypes.create_string_buffer(1024)
        self._lock = threading.Lock()

    def send(self, msg, when: float | None = None) -> None:
        """Send a mido message to sound at perf_counter() time when; at once if when is None or past."""
        data = msg.bytes()
        with self._lock:
            stamp = 0  # zero means now
            if when is not None:
                stamp = max(0, self._host_time() + round((when - time.perf_counter()) * self._ticks_per_s))
            packet = self._cm.MIDIPacketListInit(self._buffer)
            packet = self._cm.MIDIPacketListAdd(self._buffer, len(self._buffer), packet, stamp, len(data),
                                                (self._ubyte * len(data))(*data))
            if not packet or self._cm.MIDISend(self._port, self._dest, self._buffer) != 0:
                raise OSError("CoreMIDI could not send")

    def close(self) -> None:
        self._cm.MIDIPortDispose(self._port)
        self._cm.MIDIClientDispose(self._client)


class Player:
    """Sends MIDI files to one output port, in the foreground (play) or in a background thread (start).

    Like a media player: pause keeps the position, resume continues from it, stop goes back to the beginning.
    With a lead, every message leaves that many seconds early, stamped with the time it should sound, and
    pause or stop lets the notes already sent finish first.
    """

    def __init__(self, port_name: str | None = None, port=None, lead: float | None = None):
        if port is None:
            port_name = find_port(port_name)
            lead = default_lead() if lead is None else max(0.0, lead)
            if lead and sys.platform == "darwin":
                try:
                    port = CoreMidiOut(port_name)
                except Exception as exc:
                    print(f"(cannot send notes ahead, sending each one when it is due: {exc})", file=sys.stderr)
            if port is None:
                import mido

                port, lead = mido.open_output(port_name), 0.0
        self.port, self.port_name = port, port_name or "test"
        self.lead = lead or 0.0  # seconds each message leaves before it should sound; 0 sends it when due
        self.current: tuple[Path, str] | None = None  # the piece playing, paused or last played
        self.state = "stopped"  # playing, paused or stopped
        self._offset = 0.0  # seconds into the current piece while paused
        self._t0: float | None = None  # perf_counter() when 0:00 of the current piece sounds
        self._from = 0.0  # where the current run started, in seconds into the piece
        self._sent_to = 0.0  # seconds into the piece up to which every message has been sent
        self._last_when = 0.0  # the latest perf_counter() time a message was stamped for
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._down: set[tuple[int, int]] = set()
        self.lateness: list[float] | None = None  # seconds each message left late, when measuring
        self.keepalive = os.environ.get("PIANO2MIDI_KEEPALIVE", "") not in {"", "0", "off", "no"}

    @property
    def position(self) -> float:
        """Seconds into the current piece, as heard."""
        if self.state == "playing" and self._t0 is not None:
            return max(self._from, time.perf_counter() - self._t0)
        return self._offset

    @property
    def sounding_for(self) -> float:
        """Seconds until the notes already sent have played out, after a pause or stop."""
        return max(0.0, self._last_when - time.perf_counter())

    @property
    def now_playing(self) -> str | None:
        return self.current[1] if self.current and self.state == "playing" else None

    def play(self, items: list[tuple[Path, str]], from_s: float = 0.0, on_start=None) -> None:
        """Play (path, title) items in order, the first from from_s; returns when everything is sent, or paused
        or stopped. With a lead, the music goes on for that long after this returns."""
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
        self._t0, self._from, self._sent_to = time.perf_counter() + self.lead - from_s, from_s, from_s
        self._thread = threading.Thread(target=self.play, args=(items, from_s), daemon=True)
        self._thread.start()

    def pause(self) -> None:
        """Pause where the notes already sent run out (about one lead from now)."""
        if self.state == "playing":
            self._halt()
            self.state, self._offset = "paused", self._sent_to

    def resume(self) -> None:
        if self.current is not None:
            self.start([self.current], self._offset if self.state == "paused" else 0.0)

    def stop(self) -> None:
        self._halt()
        self.state, self._offset = "stopped", 0.0

    def close(self) -> None:
        """Stop, let the notes already sent finish, and close the output. Call before the program exits."""
        self.stop()
        wait = min(self._last_when - time.perf_counter(), self.lead)
        if wait > 0:
            time.sleep(wait + 0.05)
        self.port.close()

    def _halt(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None

    def _send(self, msg, when: float | None = None) -> None:
        if self.lead and when is not None:
            self.port.send(msg, when)
            self._last_when = max(self._last_when, when)
        else:
            self.port.send(msg)

    def _play_file(self, path: Path, from_s: float = 0.0) -> bool:
        """Send one file from from_s seconds, each message one lead early. True when it reached the end."""
        import mido

        pedal: dict[int, int] = {}  # sustain value per channel at from_s, restored before continuing
        self._t0 = time.perf_counter() + self.lead - from_s
        self._from = self._sent_to = from_s
        at, restored = 0.0, from_s <= 0
        try:
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
                            self._send(mido.Message("control_change", channel=channel, control=64, value=value),
                                       self._t0 + from_s)
                    restored = True
                due = self._t0 + at - self.lead  # when this message should leave
                if self._wait_until(due):
                    return False
                self._send(msg, self._t0 + at)
                self._sent_to = at
                if self.lateness is not None:
                    self.lateness.append(time.perf_counter() - due)
                if msg.type == "note_on" and msg.velocity > 0:
                    self._down.add((msg.channel, msg.note))
                elif msg.type in {"note_on", "note_off"}:
                    self._down.discard((msg.channel, msg.note))
            return True
        finally:  # everything due to leave by now has left, so the music runs up to here
            self._sent_to = max(self._sent_to, time.perf_counter() - self._t0 + self.lead)

    def _wait_until(self, when: float) -> bool:
        """Sleep until perf_counter() reaches when, sending keepalives if enabled. True if stopped meanwhile."""
        import mido

        while True:
            delay = when - time.perf_counter()
            if delay <= 0:
                return self._stop.is_set()
            if not self.keepalive:
                return self._stop.wait(delay) or self._stop.is_set()
            if self._stop.wait(min(delay, KEEPALIVE_S)):
                return True
            if when - time.perf_counter() > 0.002:  # not just before a real message
                self._send(mido.Message("active_sensing"))

    def release(self) -> None:
        """Lift every key that is still down and the sustain pedal, timed for where the music stops."""
        import mido

        when = self._t0 + self._sent_to if self.lead and self._t0 is not None else None
        for channel, note in sorted(self._down):
            self._send(mido.Message("note_off", channel=channel, note=note, velocity=0), when)
        self._down.clear()
        for channel in range(16):
            self._send(mido.Message("control_change", channel=channel, control=64, value=0), when)


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
    p.add_argument("--lead", type=float, metavar="SECONDS",
                   help=f"on macOS, send each note this early with its time (default: PIANO2MIDI_LEAD or "
                        f"{DEFAULT_LEAD_S:g}; 0 sends each note when it is due)")
    p.add_argument("--timing", action="store_true", help="report how punctually this computer sent the notes")
    p.add_argument("--keepalive", action="store_true",
                   help="send MIDI Active Sensing every 20 ms so a Wi-Fi link does not doze (PIANO2MIDI_KEEPALIVE=1)")
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
        player = Player(args.port, lead=args.lead)
    except LookupError as exc:
        print(f"Cannot play: {exc}", file=sys.stderr)
        return 2

    def announce(path: Path, title: str) -> None:
        import mido

        print(f"▶ {title} ({clock(mido.MidiFile(path).length)})", flush=True)

    player.keepalive = player.keepalive or args.keepalive
    how = [f"notes sent {player.lead:g} s ahead" if player.lead else "notes sent when due"]
    if player.keepalive:
        how.append("keepalive")
    print(f"Playing on {player.port_name} ({', '.join(how)}). Ctrl+C to stop.")
    if args.timing:
        player.lateness = []
    try:
        player.play(items, on_start=announce)
    except KeyboardInterrupt:  # play() has already timed the release of the keys and the pedal
        print("\nStopped.")
    try:
        player.close()  # the notes already sent still play
    except KeyboardInterrupt:
        pass
    if player.lateness:
        late = sorted(x * 1000 for x in player.lateness)
        print(f"Timing on this computer: {len(late)} messages, median {late[len(late) // 2]:.1f} ms late, "
              f"99% within {late[int(len(late) * 0.99)]:.1f} ms, worst {late[-1]:.1f} ms.")
        if player.lead:
            print(f"Each message left {player.lead:g} s before its time, so delays on the way shorter than "
                  "that do not change the rhythm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
