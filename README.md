<h1 align="center">piano2midi</h1>

<p align="center">
  Solo piano recording in, performance MIDI out.<br>
  Notes, timing, velocity and sustain pedal, ready for Pianoteq or a self-playing piano.
</p>

<p align="center">
  <img src="demo/pianoteq-playback.gif" alt="A transcribed Glenn Gould recording playing in Pianoteq" width="720">
</p>

<p align="center">
  <sub>Glenn Gould's rehearsal of Bach's Partita No. 2, transcribed by piano2midi and played back in Pianoteq 9 (20-second excerpt).</sub>
</p>

<p align="center">
  <a href="#demo">Demo</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-piano2midi-changes">What it changes</a> ·
  <a href="#http-api">HTTP API</a> ·
  <a href="#limits">Limits</a>
</p>

## Why piano2midi

- **Keeps the performance.** Every note keeps its real onset, release and velocity. Nothing is quantized, so rubato, voicing and articulation survive.
- **Sustain pedal included.** Pedal presses come out as standard CC64 events.
- **Batch friendly.** Drop recordings into a folder and convert them all at once. Files that are already done are skipped.
- **Runs on your machine.** A CPU is enough. No account, no API key. An HTTP API and a Dockerfile are included for servers.

Transcription is done by [Transkun](https://github.com/Yujia-Yan/Transkun) (Yan & Duan), a piano-specific model that predicts notes, velocities and pedals. piano2midi adds audio decoding, a small conservative clean-up, batch processing, a command line tool and an HTTP API.

## Demo

**Glenn Gould: J.S. Bach, Partita No. 2 in C minor, BWV 826 (rehearsal recording)**

<table>
  <tr>
    <th width="50%">Original (YouTube)</th>
    <th width="50%">Transcription, played in Pianoteq 9</th>
  </tr>
  <tr>
    <td><a href="https://www.youtube.com/watch?v=WqwZC-yLYI4"><img src="https://img.youtube.com/vi/WqwZC-yLYI4/hqdefault.jpg" alt="Glenn Gould, Partita No. 2 rehearsal, on YouTube" width="400"></a></td>
    <td><video src="https://github.com/user-attachments/assets/66c23753-2c38-4d77-9b6e-b4bc9aa24dfe" width="400" controls></video></td>
  </tr>
  <tr>
    <td><sub>Click to watch on YouTube.</sub></td>
    <td><sub>NY Steinway D Classical preset, on iPad. Full length, with sound.</sub></td>
  </tr>
</table>

| Audio | Notes | Velocity | Sustain pedal | Settings | Time |
|---|---|---|---|---|---|
| 1 min 47 s | 1,229 | 36–93 | 1 short press | default, no manual edits | about 1 min on 2 CPU cores |

Gould plays this music almost without pedal, and the transcription reflects that. Download the MIDI: [`demo/gould-partita-2-rehearsal.mid`](demo/gould-partita-2-rehearsal.mid)

## Quick start

**Requirements:** macOS or Linux, Python 3.10+ (or [uv](https://github.com/astral-sh/uv)), about 1–2 GB of disk space.

1. Install once:

   ```bash
   git clone https://github.com/kaisenyao/piano2midi.git
   cd piano2midi
   ./setup.sh
   ```

2. Put your recordings (mp3, m4a, wav, flac) in `input/`.
3. Double-click **Convert.command** (macOS) or run `./run.sh`.
4. Play the MIDI files from `output/`.

To convert a single file, run `./run.sh path/to/song.mp3`. Already converted files are skipped; add `--overwrite` to convert them again.

```text
output/
  song.mid               the file to play
  extras/song.raw.mid    untouched model output, for comparison
  extras/song.json       duration, note count, velocity range, pedal count, timing
```

## What piano2midi changes

The model output is kept as is, apart from three small fixes that are on by default. Onsets, pitches and velocities are never changed.

| Fix | Default | Why |
|---|---|---|
| Trim same-pitch overlaps | on | A key can't be pressed again before it is released. |
| Drop the soft pedal (CC67) | on | Its prediction is unreliable and colors whole passages. Keep it with `--keep-soft-pedal`. |
| Release the pedal at the end | on | No hanging pedal. |
| Close short melody gaps | off | `--legato` holds the top voice 20 ms into the next note when the gap is 100 ms or less. Good for cantabile pieces. On Gould's detached Bach it would have changed about 18% of the notes. |
| Pedal ramps | off | `--pedal-ramps` turns instant 0/127 jumps into about 50 ms of foot travel. |

Not done on purpose: quantization, velocity "humanizing" or rescaling, hand separation, score correction, source separation.

## Options

```text
piano2midi INPUT... [-o OUTPUT_DIR] [--overwrite] [--legato] [--keep-soft-pedal]
                    [--pedal-ramps] [--device auto|cpu|cuda|mps] [--threads N]
```

`--device auto` uses CUDA when available and the CPU otherwise. The CPU is reliable on Apple Silicon; `mps` is opt-in.

## HTTP API

```bash
pip install -e ".[api]"
uvicorn piano2midi.api:app --port 8000
curl -F file=@song.mp3 "http://localhost:8000/transcribe" -o song.mid
```

| Endpoint | Description |
|---|---|
| `GET /health` | Status, and whether the model is loaded. |
| `POST /transcribe` | Multipart field `file`. Optional query flags `legato`, `keep_soft_pedal`, `pedal_ramps` (all false by default). Returns the MIDI file, with a summary in the `X-Piano2MIDI-Info` header. |

A CPU `Dockerfile` is included. Each process handles one request at a time; on 2 CPU cores a recording takes about half to three quarters of its own length.

## Limits

- Works best on **solo piano**. Songs with vocals or a band produce many wrong notes; there is no source separation yet.
- The pedal is on/off. Half-pedaling is not recovered.
- Expect some missed soft inner notes and occasional wrong notes in dense passages. This is a performance transcription, not a score.
- Bring your own audio. This project does not download from streaming services.

## Development

```bash
.venv/bin/pip install -e ".[dev,api]"
.venv/bin/pytest
```

## Credits and licenses

- piano2midi: MIT (see [`LICENSE`](LICENSE)).
- [Transkun](https://github.com/Yujia-Yan/Transkun): MIT, by Yujia Yan and Zhiyao Duan. The pretrained weights ship inside the `transkun` pip package and were trained on the MAESTRO dataset (CC BY-NC-SA 4.0); check those terms before any commercial use.
