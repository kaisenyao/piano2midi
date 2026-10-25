#!/usr/bin/env bash
# One-time install into ./.venv (does not touch other Python projects).
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  echo "Using uv to create .venv (Python 3.11)…"
  uv venv .venv --python 3.11
  PIP=(uv pip install --python .venv/bin/python)
else
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "Need Python 3.10+ (or uv). On a Mac: brew install python@3.11   or   brew install uv"; exit 1
  fi
  echo "Using $PY to create .venv…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  PIP=(.venv/bin/python -m pip install)
fi

# On Linux without a GPU, set TORCH_INDEX=https://download.pytorch.org/whl/cpu to avoid the large CUDA build.
if [ -n "${TORCH_INDEX:-}" ]; then "${PIP[@]}" torch torchaudio --index-url "$TORCH_INDEX"; fi
"${PIP[@]}" -e .

echo "Checking that the model loads…"
.venv/bin/python -W ignore -c "from piano2midi.engine import TranskunEngine; e=TranskunEngine(); print('OK:', e.version, 'on', e.device)"
chmod +x run.sh
if [ ! -f tasks.csv ]; then printf 'url\n' > tasks.csv; fi
mkdir -p output
echo
echo "Done. Add video URLs to tasks.csv, then run ./run.sh. MIDI files go to output/MIDI Files/."
