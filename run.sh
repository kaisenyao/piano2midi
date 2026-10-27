#!/usr/bin/env bash
# ./run.sh                    -> tasks.csv -> output/MIDI Files/
# ./run.sh song.mp3            -> output/MIDI Files/song.mid
# ./run.sh --csv other.csv --threads 2
# ./run.sh chat                -> ask for a piece in plain language
# ./run.sh library list        -> show the local MIDI library
# ./run.sh play some words     -> play a library piece on Pianoteq over MIDI
# ./run.sh voice               -> say "Hey Jasmine", then ask for a piece
# ./run.sh web-voice           -> the same, with your iPhone as microphone and speaker
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/piano2midi ]; then echo "Run ./setup.sh first."; exit 1; fi
case "${1:-}" in
  chat) shift; exec .venv/bin/python -m piano2midi.chat "$@" ;;
  library) shift; exec .venv/bin/python -m piano2midi.library "$@" ;;
  play) shift; exec .venv/bin/python -m piano2midi.play "$@" ;;
  web-voice) shift; exec .venv/bin/python -m piano2midi.web_voice "$@" ;;
  voice) shift; exec .venv/bin/python -m piano2midi.voice "$@" ;;
esac
if [ $# -eq 0 ]; then
  if [ ! -f tasks.csv ]; then
    printf 'url\n' > tasks.csv
    echo "Created tasks.csv. Add one video URL per row, then run ./run.sh again."
    exit 0
  fi
  set -- --csv tasks.csv
fi
exec .venv/bin/piano2midi "$@"
