"""The checks run with piano2midi's defaults, not with this machine's settings: PIANO2MIDI_PORT, PIANO2MIDI_WAKE_WORDS
and the others are cleared before any test imports piano2midi."""
import os

for name in [n for n in os.environ if n.startswith("PIANO2MIDI_")]:
    del os.environ[name]
