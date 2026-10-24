#!/usr/bin/env bash
# Double-click on a Mac: converts every audio file in ./input into ./output
cd "$(dirname "$0")"
if [ ! -x .venv/bin/piano2midi ]; then
  echo "First run ./setup.sh in Terminal (one time)."
  read -r -p "Press Enter to close…" _; exit 1
fi
.venv/bin/piano2midi input -o output
open output 2>/dev/null || true
read -r -p "Press Enter to close…" _
