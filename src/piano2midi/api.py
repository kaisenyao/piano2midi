"""Minimal HTTP API (for the cloud version).

    uvicorn piano2midi.api:app --host 0.0.0.0 --port 8000

POST /transcribe  (multipart field "file")  ->  audio/midi
GET  /health
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from .audio import AUDIO_EXTENSIONS
from .pipeline import Options, midi_bytes, transcribe_audio

app = FastAPI(title="piano2midi", version="0.1.0")
_engine = None


def engine():
    global _engine
    if _engine is None:
        from .engine import TranskunEngine

        _engine = TranskunEngine()
    return _engine


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": _engine is not None}


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    legato: bool = Query(True, description="close short melody gaps"),
    keep_soft_pedal: bool = Query(False),
    pedal_ramps: bool = Query(False),
):
    suffix = Path(file.filename or "audio").suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        raise HTTPException(415, f"unsupported file type {suffix!r}")
    options = Options(legato=legato, keep_soft_pedal=keep_soft_pedal, pedal_ramps=pedal_ramps)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"input{suffix}"
        path.write_bytes(await file.read())
        try:
            _, cleaned, info = await run_in_threadpool(transcribe_audio, engine(), path, options)
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)) from exc
    name = f"{Path(file.filename or 'audio').stem}.mid"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "X-Piano2MIDI-Info": quote(json.dumps(info["output"])),
    }
    return Response(midi_bytes(cleaned), media_type="audio/midi", headers=headers)
