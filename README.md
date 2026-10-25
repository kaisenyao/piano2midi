<h1 align="center">piano2midi</h1>

<p align="center">
  Piano recordings in, performance MIDI out, and back to a piano.<br>
  Notes, timing, velocity and sustain pedal, kept in a library you can ask for by chat or by voice.
</p>

<p align="center">
  <img src="demo/pianoteq-playback.gif" alt="A transcribed Glenn Gould recording playing in Pianoteq" width="720">
</p>

<p align="center">
  <sub>Glenn Gould's rehearsal of Bach's Partita No. 2, transcribed by piano2midi and played back in Pianoteq 9 (20-second excerpt).</sub>
</p>

piano2midi turns a piano recording (a YouTube or bilibili video, or an audio file) into a
performance MIDI file with [Transkun](https://github.com/Yujia-Yan/Transkun). The files go into
a local library. You can then ask for a piece in plain language, typed or spoken, and hear it
played by Pianoteq: on the same Mac, or on an iPad that drives a digital piano, so the sound
comes out of the piano itself.

```
recording ──► transcription ──► library ──► MIDI over Wi-Fi ──► Pianoteq on iPad ──► digital piano
                                   ▲
                    chat or voice: "play Chopin's Nocturne Op. 9 No. 2"
```

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

## Installation

Requires Python 3.10+ (or [uv](https://docs.astral.sh/uv/)). Transcription runs on macOS or Linux;
playback with exact timing and the voice assistant are made for macOS. From the project directory:

```bash
./setup.sh
```

Setup creates `.venv` and an empty `tasks.csv`, and checks that the model loads. ffmpeg comes with
a dependency. YouTube downloads also need Deno or Node.js installed. For the voice assistant, add
its extras once:

```bash
uv pip install --python .venv/bin/python -e ".[voice]"
```

Keys go in your shell profile, never in the repository. Set the ones for the parts you use:

```bash
echo 'export OPENAI_API_KEY="..."' >> ~/.zshrc       # chat model (or ANTHROPIC_API_KEY) and speech recognition
echo 'export YOUTUBE_API_KEY="..."' >> ~/.zshrc      # YouTube Data API v3, for searching
echo 'export ELEVENLABS_API_KEY="..."' >> ~/.zshrc   # her voice (optional)
source ~/.zshrc
```

Everything piano2midi makes stays in `output/`, which is kept out of Git.

## Transcribe

Add video URLs to `tasks.csv`, one per row (only the `url` column is required), then run:

```bash
./run.sh
```

MIDI files go to `output/MIDI Files/`, named after the video titles, and into the library.
Add an optional `name` column to choose your own titles. Completed rows are skipped; run the
same command again to retry failures or resume after an interruption. Work files (the original
audio, raw MIDI and statistics) stay in the hidden `output/.cache/` folder. Do not edit the CSV
while it is running.

Other ways to run it:

```bash
./run.sh /path/to/song.mp3                           # local audio files or folders
./run.sh --csv tasks.csv --overwrite                 # transcribe again, reusing the downloaded audio
./run.sh /path/to/recordings -o ~/Desktop/out        # write somewhere else, outside the library
./run.sh --csv tasks.csv --device cpu --threads 2    # choose the device and CPU threads
```

CSV columns are `url,name,status,audio_path,midi_path,error`. Status is `pending`, `processing`,
`done` or `failed` (blank means pending). Exit codes are 0 for success, 1 for task failures,
2 for task-list or usage errors and 130 for interruption.

By default, cleanup trims same-pitch overlaps, removes soft-pedal events and makes sure the
sustain pedal is released at the end. `--legato`, `--keep-soft-pedal` and `--pedal-ramps` change
that; see `./run.sh --help`.

## Library

The library is one file, `output/library.db`, with the MIDI files in `output/MIDI Files/`.
`output/MIDI Files/playlist.m3u` lists everything in the order it was added and is kept up to
date. MIDI files you put into that folder yourself are added the next time the chat or the voice
assistant starts.

```bash
./run.sh library list
./run.sh library import ~/Desktop/midi    # copy existing MIDI files in (--in-place to leave them)
./run.sh library enrich                   # add composers, other-language titles and tags
```

## Chat

```bash
./run.sh chat
```

Ask for a piece in plain language, in English, Chinese or another language; the assistant
answers in yours. It looks in the library first. If the piece is not there, it searches YouTube
for a real performance where the piano carries the music (solo piano first, then piano with
voice, never a band mix or a remix, and no falling-notes videos or tutorials), transcribes it,
saves it and plays it. If no good piano version exists, it says so. When a request is unclear,
it asks.

Examples: "I'd like to hear Chopin's Nocturne Op. 9 No. 2 by Rubinstein", "something gentle by
Debussy", "play ~/Music/my-recording.m4a". You can also paste a video link (YouTube, bilibili or
another site yt-dlp supports) to use that recording directly. For a long list of links, use
`tasks.csv` instead.

| Command | What it does |
|---|---|
| `/play` ▶ | continue after a pause, or start the current piece again |
| `/pause` ⏸ | pause and keep the position |
| `/stop` ⏹ | stop and go back to the beginning |
| `/library [words]` | list or search the library |
| `/import <folder>` | add MIDI files |
| `/enrich` | fill in composers and other-language titles |
| `/new` | start a new conversation |
| `/quit` | exit (Control+C does not) |

Plain language works too: "stop playing" pauses, "continue" picks up where it paused, "start
over" plays from the beginning, and asking for another piece replaces the current one.

The model is OpenAI `gpt-6-luna`, or Claude Haiku 5.5 when only `ANTHROPIC_API_KEY` is set.
Choose another with, for example, `export PIANO2MIDI_MODEL=anthropic:claude-haiku-5-5`.

## Playback

piano2midi plays library pieces on Pianoteq by sending notes and pedal over MIDI, like someone
playing a MIDI keyboard. Only notes and controllers are sent, so the Pianoteq preset stays as it
is.

One-time setup for Pianoteq on an iPad (the iPad can stay connected to a digital piano by USB):

1. Open Pianoteq on the iPad.
2. On the Mac, open Audio MIDI Setup, choose Window > Show MIDI Studio, and click the globe
   button to open MIDI Network Setup.
3. Add a session under My Sessions, name it `piano2midi` and enable it. Leave Latency at 0 ms.
4. Select the iPad under Sessions and Directories and click Connect.
5. In Pianoteq on the iPad, enable the network session as a MIDI input.

For Pianoteq on the same Mac, enable the IAC Driver in Audio MIDI Setup, select it as Pianoteq's
MIDI input, and add `--port IAC` (or set `PIANO2MIDI_PORT=IAC`).

```bash
./run.sh play someone like you    # best match in the library
./run.sh play path/to/file.mid
./run.sh play --all               # the whole playlist, in order
./run.sh play --ports             # list MIDI outputs
```

Wi-Fi delivers notes unevenly, which breaks the rhythm. So on a Mac each note leaves one second
early, together with the time it should sound, and the iPad plays it on time. In return,
playback starts, pauses and stops about one second after you ask. Pausing or stopping lifts all
keys and the pedal, and continuing puts the pedal back where it was. Set `PIANO2MIDI_LEAD` (or
`--lead`) in seconds to change the lead: `2` for an unsteady network, `0.5` for a quicker
response, `0` to send each note when it is due. `--timing` reports how punctually this computer
sent the notes.

## Voice

```bash
./run.sh voice
```

Say "Hey Jasmine", or her Chinese name Xiaoming Tongxue, and ask for a piece. Say it in one
breath ("Hey Jasmine, stop"), or pause after the name: she answers in the language of the name
you used, then listens. Anything that is playing pauses while you speak and continues
afterwards, unless you asked to stop or for something else. "Stop", "continue" and "start over"
(also in Chinese) act at once; everything else goes to the same assistant as the chat, and she
speaks the answer. The first run asks for microphone access and downloads two small models
(about 34 MB) into `output/.cache/models`.

How it listens and speaks:

- **Wake words:** heard on the computer by [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
  keyword spotting, which takes any name without training. Set your own with
  `PIANO2MIDI_WAKE_WORDS`, separated by commas: Chinese characters or English words, for
  example `"Hey Maestro"`. English names are matched against a pronouncing dictionary plus a few
  common non-native ways of saying them (`EXTRA_PRONUNCIATIONS` in `voice.py`). If it misses you,
  lower `PIANO2MIDI_WAKE_THRESHOLD` (default `0.2`); if it wakes by itself, raise it.
- **When you have finished:** [Silero](https://github.com/snakers4/silero-vad) voice activity
  detection tells your voice from silence and the piano, so the music or her own greeting is not
  taken for your request.
- **Listening:** OpenAI `gpt-transcribe` (needs `OPENAI_API_KEY`), with your library's titles and
  names as spelling hints. If you mix languages, list them, for example
  `export PIANO2MIDI_LANGUAGES=zh,en`.
- **Speaking:** ElevenLabs. Set `ELEVENLABS_API_KEY` and `PIANO2MIDI_VOICE_ID` (from My Voices on
  elevenlabs.io; voices from the shared library need paid credits). The default model is
  `eleven_v4_turbo`; `PIANO2MIDI_TTS_MODEL=eleven_v4` is richer and slower. Without these
  settings, the macOS voice speaks and a beep stands in for her greetings.
- **Greetings:** made once with her voice and kept in `output/.cache/voice`. Hear them with
  `./run.sh voice --greetings`; change them in `GREETINGS` in `voice.py`.

`./run.sh voice --debug` shows which name she heard, what she answered, and when nothing was
heard after the name.

## Settings

All optional; set them in your shell profile.

| Variable | Default | Used for |
|---|---|---|
| `OPENAI_API_KEY` | | chat model and speech recognition |
| `ANTHROPIC_API_KEY` | | chat model, if you prefer Claude |
| `PIANO2MIDI_MODEL` | `openai:gpt-6-luna` | chat model |
| `YOUTUBE_API_KEY` | | searching YouTube from the chat |
| `PIANO2MIDI_PORT` | `piano2midi` | MIDI output, by part of its name |
| `PIANO2MIDI_LEAD` | `1` | seconds each note leaves early (macOS) |
| `PIANO2MIDI_WAKE_WORDS` | Xiaoming Tongxue (in Chinese), `Hey Jasmine` | wake words |
| `PIANO2MIDI_WAKE_THRESHOLD` | `0.2` | wake word sensitivity (lower hears more) |
| `PIANO2MIDI_LANGUAGES` | | languages you speak, e.g. `zh,en` |
| `PIANO2MIDI_STT_MODEL` | `gpt-transcribe` | speech recognition model |
| `ELEVENLABS_API_KEY` | | her voice |
| `PIANO2MIDI_VOICE_ID` | | which ElevenLabs voice |
| `PIANO2MIDI_TTS_MODEL` | `eleven_v4_turbo` | ElevenLabs model |

## Limits and maintenance

Works best on solo piano. There is no vocal or accompaniment separation, so the assistant looks
for piano-led recordings, and transcription may miss notes or predict incorrect ones. The sustain
pedal is on or off. Download only individual videos you are permitted to access and download.
Playlist expansion, live streams and bypassing access restrictions are not supported. Website
support depends on yt-dlp; download failures are recorded in the CSV. To update the downloader:

```bash
uv pip install --python .venv/bin/python --upgrade 'yt-dlp[default]'
```

## License and credits

piano2midi is MIT licensed. Transcription uses
[Transkun](https://github.com/Yujia-Yan/Transkun). Its pretrained weights were trained on the
MAESTRO dataset (CC BY-NC-SA 4.0); check the applicable terms before commercial use. The voice
assistant uses [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) keyword spotting and
[Silero VAD](https://github.com/snakers4/silero-vad).
