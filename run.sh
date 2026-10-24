#!/usr/bin/env bash
# ./run.sh song.mp3            -> output/song.mid
# ./run.sh input               -> every audio file in ./input
# ./run.sh a.mp3 b.flac -o ~/MIDI --overwrite
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/piano2midi ]; then echo "Run ./setup.sh first."; exit 1; fi
if [ $# -eq 0 ]; then set -- input; fi
exec .venv/bin/piano2midi "$@"
