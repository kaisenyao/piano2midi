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

1. Add video URLs to `tasks.csv`, one per row. Only the `url` column is required:

   ```csv
   url
   https://www.youtube.com/watch?v=VIDEO_ID
   ```

2. Run from the project directory:

   ```bash
   ./run.sh
   ```

3. Find your MIDI files in `output/MIDI Files/`. Filenames use video titles
   automatically; add an optional `name` column to choose your own titles.

The runner fills in task status and file paths. Completed tasks are skipped.
Run the same command again to retry failures or resume after an interruption.
Each finished MIDI is also added to the local library and its playlist (see
[Chat](#chat)). Work files stay in the hidden `output/.cache/` folder: the original
audio (no intermediate MP3), raw MIDI and statistics. Do not edit the CSV while
it is running, and run only one process per task list.

## Installation

Requires macOS or Linux and Python 3.10+ (or uv). Run from the project directory:

```bash
./setup.sh
```

Setup creates an empty `tasks.csv` without overwriting an existing list. ffmpeg
is provided by a dependency. YouTube downloads also require an installed Deno
or Node.js runtime. Your `tasks.csv` and generated files in `output/` are excluded
from Git.

## Other options

```bash
# Choose a task list, device and CPU thread count
./run.sh --csv tasks.csv --device cpu --threads 2

# Transcribe again while reusing valid cached audio
./run.sh --csv tasks.csv --overwrite

# Convert local audio files or folders; no input directory is required
./run.sh /path/to/song.mp3

# Write MIDI and work files to another folder, outside the library
./run.sh /path/to/recordings -o ~/Desktop/transcriptions
```

CSV columns are `url,name,status,audio_path,midi_path,error`; only `url` is required.
Status is `pending`, `processing`, `done` or `failed`; a blank status means pending.
Keep generated path columns to resume tasks. Add a new row when changing a URL.
Paths are stored relative to the CSV's directory. Assigned output paths stay fixed
on retries; new tasks use the directory selected with `-o` (default `output/.cache`).
Exit codes are 0 for success, 1 for task failures, 2 for task-list or usage errors,
and 130 for interruption.

By default, cleanup trims same-pitch overlaps, removes soft-pedal events and
ensures the sustain pedal is released at the end. Use `--legato`,
`--keep-soft-pedal` and `--pedal-ramps` to adjust the behavior. See `./run.sh --help`.

## Chat

Ask for a piece in plain language. The assistant checks your local library first.
If nothing matches, it searches YouTube, picks a real performance (it avoids
falling-notes videos, tutorials and easy arrangements), runs it through the CSV
flow above, and saves the MIDI in `output/MIDI Files/`. It tells you which video
it chose and why.

Set the keys once. They stay in your shell profile, never in the repository:

```bash
echo 'export OPENAI_API_KEY="..."' >> ~/.zshrc     # or ANTHROPIC_API_KEY
echo 'export YOUTUBE_API_KEY="..."' >> ~/.zshrc    # YouTube Data API v3
source ~/.zshrc
```

Then run:

```bash
./run.sh chat
```

Example requests: "Play Chopin's Nocturne Op. 9 No. 2 by Rubinstein",
"something gentle by Debussy", "transcribe ~/Music/my-recording.m4a".
In the chat, `/library` lists or searches the library, `/import <folder>` adds MIDI
files, `/enrich` fills in composers and other-language titles, `/new` starts over
and `/quit` exits.

The default model is OpenAI `gpt-6-luna`, or Claude Haiku 5.5 when only
`ANTHROPIC_API_KEY` is set. Choose another with, for example,
`export PIANO2MIDI_MODEL=anthropic:claude-haiku-5-5`.

The library is a single file, `output/library.db`. Manage it from the terminal:

```bash
./run.sh library import ~/Desktop/midi    # copy existing MIDI files in
./run.sh library list
./run.sh library enrich                   # add composers, aliases and tags
```

MIDI files you place in `output/MIDI Files/` yourself are added when the chat starts.
`output/MIDI Files/playlist.m3u` lists the whole library in the order it was added and
is kept up to date. The chat deletes its own downloads once a MIDI is saved.
Everything under `output/` stays out of Git.

## Limits and maintenance

Works best on solo piano. There is no vocal or accompaniment separation, and
transcription may miss notes or predict incorrect ones. Sustain pedal is on/off.
Download only individual videos you are permitted to access and download. Playlist
expansion, live streams and bypassing access restrictions are not supported.
Website support depends on yt-dlp; download failures are recorded in the CSV.
To update the downloader:

```bash
uv pip install --python .venv/bin/python --upgrade 'yt-dlp[default]'
```

## License and credits

piano2midi is MIT licensed. Transcription uses
[Transkun](https://github.com/Yujia-Yan/Transkun). Its pretrained weights were
trained on the MAESTRO dataset (CC BY-NC-SA 4.0); check the applicable terms before
commercial use.
