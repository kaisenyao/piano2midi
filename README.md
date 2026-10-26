<h1 align="center">piano2midi</h1>

<p align="center">
  Piano recordings in, performance MIDI out, and back to a piano.<br>
  Notes, timing, velocity and sustain pedal, kept in a library you can ask for by chat or by voice.
</p>

<p align="center">
  <img src="docs/how-it-works.svg" alt="You ask for a piece; piano2midi on the Mac finds it in the library (or finds a recording and transcribes it) and sends the notes over Wi-Fi to Pianoteq on an iPad, which plays them through a digital piano" width="900">
</p>

piano2midi turns a piano recording into a performance MIDI file with
[Transkun](https://github.com/Yujia-Yan/Transkun): a video from YouTube, bilibili or any of the
many other sites [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports, or an audio file of your
own. The files go into a local library. You can then ask for a piece in plain language, typed or
spoken, and hear it on your piano: played by Pianoteq on an iPad that drives a digital piano, by
the digital piano's own sound, or by Pianoteq on the same Mac.

## Quick start

```bash
./setup.sh                  # once, then follow Setup below
./run.sh chat               # type "play Clair de Lune"
./run.sh voice              # or say "Hey Jasmine, play Clair de Lune"
```

## Transcription demo

<p align="center">
  <img src="demo/pianoteq-playback.gif" alt="A transcribed Glenn Gould recording playing in Pianoteq" width="720">
</p>

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

## Setup

Five steps the first time; step 5 is only for the voice assistant.

### 1. Install

Requires Python 3.10+ (or [uv](https://docs.astral.sh/uv/)). Transcription runs on macOS or
Linux; playback with exact timing and the voice assistant are made for macOS. YouTube downloads
also need [Deno](https://deno.com) or Node.js. From the project directory:

```bash
./setup.sh
```

Setup creates `.venv` and an empty `tasks.csv`, and checks that the transcription model loads.
ffmpeg comes with a dependency.

### 2. Add your keys

Keys go in your shell profile (`~/.zshrc` on a Mac), never in the repository.

| Key | Needed for | Where to get it |
|---|---|---|
| `OPENAI_API_KEY` | the chat model and, for voice, speech recognition | [platform.openai.com](https://platform.openai.com) > API keys |
| `ANTHROPIC_API_KEY` | instead of OpenAI for the chat model (voice still needs OpenAI) | [console.anthropic.com](https://console.anthropic.com) > API keys |
| `YOUTUBE_API_KEY` | finding recordings on YouTube | [Google Cloud console](https://console.cloud.google.com): enable "YouTube Data API v3", then Credentials > Create API key |
| `ELEVENLABS_API_KEY` | her voice (optional, see step 5) | [elevenlabs.io](https://elevenlabs.io), under API keys in your account |

```bash
echo 'export OPENAI_API_KEY="..."' >> ~/.zshrc
echo 'export YOUTUBE_API_KEY="..."' >> ~/.zshrc
source ~/.zshrc
```

Without a YouTube key the chat still plays your library and recordings you link or point it to.

### 3. Choose where the sound comes from

piano2midi plays by sending notes and pedal over MIDI, like someone playing a MIDI keyboard. Pick
one way and set it up once:

| | Sound | Connection | Settings |
|---|---|---|---|
| **A** | Pianoteq on an iPad (default) | Mac → network MIDI over Wi-Fi → iPad → USB → digital piano | none |
| **B** | The piano's own sound | Mac paired with the piano's Bluetooth MIDI | `PIANO2MIDI_PORT`, `PIANO2MIDI_LEAD=0` |
| **C** | The piano's own sound | USB cable from the Mac to the piano | `PIANO2MIDI_PORT`, `PIANO2MIDI_LEAD=0` |
| **D** | Pianoteq on the same Mac | IAC Driver, inside the Mac | `PIANO2MIDI_PORT=IAC`, `PIANO2MIDI_LEAD=0` |

**A. Pianoteq on an iPad (the iPad can stay connected to the piano by USB)**

1. Open Pianoteq on the iPad.
2. On the Mac, open Audio MIDI Setup, choose Window > Show MIDI Studio, and click the globe
   button to open MIDI Network Setup.
3. Under My Sessions, click +, name the session `piano2midi` and tick it. Leave Latency at 0 ms.
4. Select the iPad under Sessions and Directories and click Connect. It now shows under
   Participants.
5. In Pianoteq on the iPad, enable the network session as a MIDI input.

**B. The piano's own sound, over Bluetooth**

1. Turn on Bluetooth MIDI on the piano (see its manual).
2. On the Mac, open Audio MIDI Setup, choose Window > Show MIDI Studio and click the Bluetooth
   button. Click Connect next to the piano. Pair it here, not in System Settings, which connects
   Bluetooth audio instead.

**C. The piano's own sound, over USB**

1. Connect the Mac to the piano's USB port. If an iPad uses that port, unplug it first.

**D. Pianoteq on the same Mac**

1. In Audio MIDI Setup > Window > Show MIDI Studio, double-click IAC Driver and tick "Device is
   online".
2. In Pianoteq, enable "IAC Driver Bus 1" as a MIDI input.

For B, C and D, find the output's name and keep it in your profile. Any part of the name will
do. A Roland FP-40, for example, shows up as `FP-40 MIDI Bluetooth` over Bluetooth and
`Roland Digital Piano` over USB:

```bash
./run.sh play --ports                                  # lists the MIDI outputs
echo 'export PIANO2MIDI_PORT="FP-40"' >> ~/.zshrc      # part of the name you chose
echo 'export PIANO2MIDI_LEAD=0' >> ~/.zshrc
source ~/.zshrc
```

`PIANO2MIDI_LEAD=0` is there because only Wi-Fi needs notes sent early (see
[Playback](#playback)). To try another way for a single run, put the settings in front of the
command, for example `PIANO2MIDI_PORT=Roland PIANO2MIDI_LEAD=0 ./run.sh voice`.

### 4. Test

The repository includes a demo MIDI file, so you can test before your library has anything in
it:

```bash
./run.sh play demo/gould-partita-2-rehearsal.mid
```

It prints `Playing on ...` with the output it found, and the piano should start within about a
second. Control+C stops it. Then try the chat: `./run.sh chat`.

### 5. Voice (optional)

Install the voice extras once:

```bash
uv pip install --python .venv/bin/python -e ".[voice]"    # or: .venv/bin/python -m pip install -e ".[voice]"
```

For her voice, add your ElevenLabs key and a voice ID. Copy the ID from a voice under My Voices
on elevenlabs.io; voices from the shared library need paid credits. If you speak more than one
language, list them:

```bash
echo 'export ELEVENLABS_API_KEY="..."' >> ~/.zshrc
echo 'export PIANO2MIDI_VOICE_ID="..."' >> ~/.zshrc
echo 'export PIANO2MIDI_LANGUAGES=zh,en' >> ~/.zshrc     # optional
source ~/.zshrc
./run.sh voice --greetings     # she says each greeting once
./run.sh voice                 # the first run asks for microphone access
```

Without ElevenLabs the macOS voice speaks and a beep stands in for her greetings. The first run
downloads two small models (about 34 MB) into `output/.cache/models`.

Tip: to run piano2midi from any folder, add an alias to your profile, for example
`alias p2m="$HOME/Documents/piano2midi/run.sh"`, then use `p2m chat`, `p2m voice` and so on.

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
`tasks.csv` (see [Transcribe](#transcribe)).

| Command | What it does |
|---|---|
| `/play` ▶ | continue after a pause, or start the current piece again |
| `/pause` ⏸ | pause and keep the position |
| `/stop` ⏹ | stop and go back to the beginning |
| `/library [words]` | list or search the library |
| `/import <folder>` | add MIDI files |
| `/enrich` | fill in composers and other-language titles |
| `/new` | start a new conversation |
| `/quit` | exit (Control+C only interrupts a request) |

Plain language works too: "stop playing" pauses, "continue" picks up where it paused, "start
over" plays from the beginning, and asking for another piece replaces the current one.

The model is OpenAI `gpt-6-luna`, or Claude Haiku 5.5 when only `ANTHROPIC_API_KEY` is set.
Choose another with, for example, `export PIANO2MIDI_MODEL=anthropic:claude-haiku-5-5`.

## Voice

```bash
./run.sh voice
```

Say "Hey Jasmine", or her Chinese name Xiaoming Tongxue, and ask for a piece. Say it in one
breath ("Hey Jasmine, stop"), or pause after the name: she answers in the language of the name
you used, then listens. Anything that is playing pauses while you speak and continues
afterwards, unless you asked to stop or for something else. "Stop", "continue" and "start over"
(also in Chinese) act at once; everything else goes to the same assistant as the chat, and she
speaks the answer. Control+C quits.

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
- **Listening:** OpenAI `gpt-transcribe`, with your library's titles and names as spelling
  hints and the languages in `PIANO2MIDI_LANGUAGES`.
- **Speaking:** ElevenLabs, with the voice in `PIANO2MIDI_VOICE_ID`. The default model is
  `eleven_v4_turbo`; `PIANO2MIDI_TTS_MODEL=eleven_v4` is richer and slower.
- **Greetings:** made once with her voice and kept in `output/.cache/voice`. Change them in
  `GREETINGS` in `voice.py`.

`./run.sh voice --debug` shows which name she heard, what she answered, and when nothing was
heard after the name.

## Playback

```bash
./run.sh play someone like you    # best match in the library
./run.sh play path/to/file.mid
./run.sh play --all               # the whole playlist, in order
./run.sh play --ports             # list MIDI outputs
```

`--port` and `--lead` override `PIANO2MIDI_PORT` and `PIANO2MIDI_LEAD` for one run. Only notes and
controllers are sent, so the sound settings on the receiving end stay as they are. Pausing or
stopping lifts all keys and the pedal, and continuing puts the pedal back where it was.
Control+C stops `play`.

Wi-Fi delivers notes unevenly, which breaks the rhythm. So on a Mac each note leaves one second
early, together with the time it should sound, and the iPad plays it on time:

<p align="center">
  <img src="docs/timing.svg" alt="Sent when due, notes reach the piano unevenly; sent one second early with their time, the iPad waits and plays them evenly" width="900">
</p>

In return, playback starts, pauses and stops about one second after you ask. Set
`PIANO2MIDI_LEAD` in seconds to change the lead: `2` for an unsteady network, `0.5` for a quicker
response, `0` to send each note when it is due, as over Bluetooth or USB. `--timing` reports how
punctually this computer sent the notes.

If nothing sounds, check the connection rather than the program. For the iPad, it must still be
listed under Participants in MIDI Network Setup (the session can drop when a computer sleeps),
with Pianoteq open. For Bluetooth, the piano must show as connected in Audio MIDI Setup. After
reconnecting, start the chat or the voice assistant again.

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

## Settings

Set these in your shell profile; [Setup](#setup) says which ones you need.

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
