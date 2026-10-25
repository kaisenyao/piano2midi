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
    <th width="50%">Original recording</th>
    <th width="50%">Transcription, played in Pianoteq 9</th>
  </tr>
  <tr>
    <td valign="top">
      <video src="https://github.com/user-attachments/assets/4d7ad6ef-2430-4907-9b99-27893429c51e" width="100%" controls></video>
    </td>
    <td valign="top">
      <video src="https://github.com/user-attachments/assets/66c23753-2c38-4d77-9b6e-b4bc9aa24dfe" width="100%" controls></video>
    </td>
  </tr>
  <tr>
    <td><sub>Source: <a href="https://www.youtube.com/watch?v=WqwZC-yLYI4">Glenn Gould Official Channel on YouTube</a>.</sub></td>
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

## Batch video URLs from a CSV

1. Copy `tasks.example.csv` to `tasks.csv` in the repository root. Replace its
   example with your video URLs. Only `url` is required; video titles are used
   automatically. Add an optional `name` column to choose your own filenames.

   ```csv
   url
   https://www.youtube.com/watch?v=WqwZC-yLYI4
   ```

2. Run `./run.sh --csv tasks.csv`.
3. Find the MIDI in `output/`, original audio in `output/audio/`, and raw MIDI
   and statistics in `output/extras/`. No `input/` folder is needed for this mode.

Existing installations can update with `uv pip install --python .venv/bin/python -e .`
(or `.venv/bin/python -m pip install -e .` when pip is installed). The added
`yt-dlp[default]` dependency includes its normal download and YouTube extraction
helpers. YouTube extraction may also require a supported JavaScript runtime;
this runner enables installed Deno or Node.js. If neither is installed, install
one before downloading from YouTube. No additional ffmpeg installation is needed.
Audio is kept in its original container and decoded by ffmpeg without an
intermediate MP3 conversion.
Some sites may only provide audio in a combined audio/video container.

`status` becomes `processing`, then `done` or `failed`. Run the same command
again to retry failures and interrupted tasks. Valid completed MIDI is skipped;
valid cached audio is reused without another download. Missing or corrupt output
is rebuilt. `--overwrite` forces transcription again while retaining valid audio.
Duplicate URLs share results, and one failed task does not stop later rows.

Cache filenames use a URL hash. MIDI filenames use your `name`, or the video
title when blank; that name and output path are saved before transcription.
Unsafe filename characters are replaced, and collisions get a stable URL hash
suffix. Keep the generated `audio_path` and `midi_path` cells when resuming;
they identify the files belonging to the task. To change a URL, add a new row
instead of keeping the old row's generated paths. Existing assigned paths are
retained on retries, including when `-o` changes; new tasks use the new folder.

Paths written into the CSV are relative to the CSV's directory. `-o` retains the
local CLI's behavior: relative to the working directory (`run.sh` first changes
to the repository). UTF-8 CSV with or without a BOM is supported, including a
file with only the `url` header. The runner adds missing standard columns and
preserves additional columns. Do not edit the CSV while it is running; run only
one process per CSV. Each update replaces the CSV atomically. Ctrl+C stops the
batch; run the same command to resume. Exit codes: 0 for success, 1 for task
failures, 2 for task-list/usage errors, 130 for interruption.

Use single video links you are permitted to download. Playlist/channel expansion,
live streams and bypassing DRM or access restrictions are not supported. Site
changes or restrictions may cause individual downloads to fail; the error is
recorded in the CSV. Use `uv pip install --python .venv/bin/python --upgrade 'yt-dlp[default]'`
to update the downloader when needed.

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
piano2midi --csv TASKS.csv [same options]
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
- Local audio and individual video URLs via CSV are supported; website support
  depends on yt-dlp and the source's availability.

## Development

```bash
.venv/bin/pip install -e ".[dev,api]"
.venv/bin/pytest
```

## Credits and licenses

- piano2midi: MIT (see [`LICENSE`](LICENSE)).
- [Transkun](https://github.com/Yujia-Yan/Transkun): MIT, by Yujia Yan and Zhiyao Duan. The pretrained weights ship inside the `transkun` pip package and were trained on the MAESTRO dataset (CC BY-NC-SA 4.0); check those terms before any commercial use.
