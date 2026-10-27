"""iPhone Voice: the phone is her microphone and speaker; the Mac runs the same conversation as ./run.sh voice."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import logging
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import urlsplit

import anyio
import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .chat import Worker
from .voice import Conversation, PauseControl, SessionEnded, VoiceParts, voice_chat

WEB = Path(__file__).with_name('web')
ASSETS = {'app.js', 'orb.js', 'cues.js', 'capture.js', 'style.css', 'manifest.webmanifest',
          'icon.png', 'cue-active.mp3', 'cue-paused.mp3'}
TOKEN = secrets.token_urlsafe(32)  # main() uses the saved one
PORT = 8765
app = FastAPI(title='piano2midi', docs_url=None, redoc_url=None, openapi_url=None)
# One phone owns this server's playback core at a time. Local Voice/Chat are separate processes.
_owner = asyncio.Lock()
_parts = None
DEBUG = False


def saved_token() -> str:
    """The phone's access token, kept so the address and a Home Screen app survive restarts."""
    from .library import CACHE_DIR
    path = CACHE_DIR / 'phone-token'
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.write_text(secrets.token_urlsafe(32))
    return path.read_text().strip()


def voice_parts() -> VoiceParts:
    """Loaded once per server, so that connecting is quick."""
    global _parts
    if _parts is None:
        _parts = VoiceParts()
    return _parts


def phone_player(control):
    from .play import Player

    class PhonePlayer(Player):
        def start(self, items, from_s=0.0, on_ready=None):
            def ready():
                on_ready()
                control.wait_for_speech()  # as on the Mac, she finishes speaking before the piano starts
            super().start(items, from_s, ready if on_ready else None)

        def _launch(self, items, from_s):
            with control.condition:  # never while the phone is paused
                control.checkpoint()
                super()._launch(items, from_s)

    # Timed like ./run.sh voice: notes leave one lead early, so Wi-Fi delays do not shake the rhythm.
    player = PhonePlayer()
    with control.condition:
        control.player = player
    return player


class Phone:
    """Her voice goes to the phone in pieces as it is made; the phone plays them back to back, then asks
    to listen on (resume) or to wait for her name (sleep)."""

    def __init__(self, emit, control: PauseControl):
        self.emit, self.control = emit, control

    def status(self, state):
        self.emit({'type': 'state', 'state': state})

    def speak(self, pieces):
        for pcm in pieces:
            pcm = np.asarray(pcm, dtype='<i2')
            self.control.speech_until = max(self.control.speech_until, self.control.now()) + len(pcm) / 24000
            self.emit({'type': 'audio', 'data': base64.b64encode(pcm.tobytes()).decode()})

    def turn_end(self, conversation, sleep):
        self.emit({'type': 'turn_end', 'sleep': sleep})


def open_session(emit, control: PauseControl) -> Conversation:
    """The connected phone's conversation. Model and library work stays on one worker thread."""
    from .chat import Assistant, index_midi_folder
    from .library import Library
    lib = Library()
    assistant = Assistant(lib)
    try:
        index_midi_folder(lib)
        chat, _ = voice_chat()
        chat.send_user = control.guard(chat.send_user)
        chat.send_tool_results = control.guard(chat.send_tool_results)
        assistant.clock = control.now  # a pause does not count as a quiet spell
        assistant.run_tool = control.guard(assistant.run_tool)
        assistant.control_playback = control.guard(assistant.control_playback)
        def get_player():
            if assistant.player is None:
                assistant.player = phone_player(control)
            return assistant.player
        assistant._get_player = get_player
        conversation = Conversation(assistant, chat, lib, voice_parts(), Phone(emit, control), control, DEBUG)
    except Exception:
        lib.db.close()
        raise
    emit({'type': 'ready'})
    conversation.status('waiting')
    return conversation


@app.get('/')
def index():
    return FileResponse(WEB / 'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/{name}')
def asset(name: str):
    if name not in ASSETS:
        raise HTTPException(404)
    media = 'application/manifest+json' if name == 'manifest.webmanifest' else None
    return FileResponse(WEB / name, media_type=media, headers={'Cache-Control': 'no-cache'})


@app.websocket('/voice')
async def voice_socket(socket: WebSocket):
    # Require same-origin, plus a secret sent in the first message (never in request logs).
    origin = urlsplit(socket.headers.get('origin', ''))
    if origin.netloc != socket.headers.get('host') or origin.scheme not in {'http', 'https'}:
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        hello = await asyncio.wait_for(socket.receive_json(), 10)
        provided = hello.get('token') if isinstance(hello, dict) else None
        if not isinstance(provided, str) or not hmac.compare_digest(provided.encode(), TOKEN.encode()):
            await socket.close(code=1008, reason='Invalid access token')
            return
    except WebSocketDisconnect:
        return
    except (TimeoutError, ValueError, KeyError, TypeError):
        await socket.close(code=1008)
        return
    if _owner.locked():
        await socket.close(code=1013, reason='Another phone is connected')
        return
    async with _owner:
        print('  · iPhone connected', flush=True)
        loop = asyncio.get_running_loop()
        events = asyncio.Queue(maxsize=64)
        def emit(event):
            asyncio.run_coroutine_threadsafe(events.put(event), loop).result(timeout=10)
        async def sender():
            while True:
                await socket.send_json(await events.get())
        sending = asyncio.create_task(sender())
        worker = Worker()  # model and library work, in order, off the event loop
        control = PauseControl()
        pending = asyncio.Queue(maxsize=8)
        session = None
        tasks = [sending]

        async def work(fn, *args):
            return await asyncio.wrap_future(worker.submit(fn, *args))

        def frame(epoch, data):
            # Ignore microphone packets queued before the pause, including ones
            # whose worker was waiting at the gate when the pause arrived.
            control.checkpoint()
            if epoch == control.epoch:
                session.frame(np.frombuffer(data, dtype='<i2'))

        async def process():
            while True:
                epoch, command, data = await pending.get()
                if command == 'frame':
                    await work(frame, epoch, data)
                else:
                    await work(control.guard(getattr(session, command)))

        async def receive():
            while True:
                message = await socket.receive()
                if message['type'] == 'websocket.disconnect':
                    return
                if message.get('bytes') is not None:
                    if control.paused:
                        continue
                    item = (control.epoch, 'frame', message['bytes'])
                elif message.get('text'):
                    try:
                        payload = json.loads(message['text'])
                    except json.JSONDecodeError:
                        await socket.close(code=1008, reason='Malformed voice command')
                        return
                    commands = {'resume', 'sleep', 'pause_session', 'continue_session'}
                    if not isinstance(payload, dict) or payload.get('type') not in commands or set(payload) != {'type'}:
                        await socket.close(code=1008, reason='Unsupported voice command')
                        return
                    command = payload['type']
                    if command in {'pause_session', 'continue_session'}:
                        # Applied before the next message is read, so later audio is already paused.
                        try:
                            await asyncio.to_thread(control.set_paused, command == 'pause_session')
                            continue
                        except LookupError as exc:  # continuing the piano found it gone: she says so, and it waits
                            session.assistant.piano_missing(exc)
                            item = (control.epoch, 'piano_unavailable', None)
                    else:
                        item = (control.epoch, command, None)
                else:
                    await socket.close(code=1008, reason='Unsupported voice message')
                    return
                try:
                    pending.put_nowait(item)
                except asyncio.QueueFull:
                    await socket.close(code=1013, reason='Voice input overloaded')
                    return

        try:
            session = await work(open_session, emit, control)
            tasks += [asyncio.create_task(process()), asyncio.create_task(receive())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, SessionEnded):
            pass
        except Exception:  # the phone sees the connection close and turns gray
            import traceback
            traceback.print_exc()
        finally:
            control.close()  # the piano stops, and a paused worker wakes to unwind
            try:
                with anyio.CancelScope(shield=True):  # a closed connection still releases everything
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    if session:
                        await work(session.close)  # SQLite, the model and the player, after the current step
            except asyncio.CancelledError:
                pass  # quitting does not wait for that step: the worker's thread ends with the program
            finally:
                worker.shutdown()
                print('  · iPhone disconnected', flush=True)


def main(argv=None):
    global TOKEN, DEBUG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--debug', action='store_true', help='Also show each time she hears her name')
    DEBUG = parser.parse_args(argv).debug
    from .library import CACHE_DIR
    from .web_tls import CA_FILE, local_name, prepare_https
    TOKEN = saved_token()
    try:
        _, label = voice_chat()  # as ./run.sh voice: a missing key shows now, not on the phone
    except Exception as exc:
        parser.exit(2, f'Cannot start the chat model: {exc}\n')
    print('Loading speech recognition and the wake word…', flush=True)
    try:
        import uvicorn
        parts = voice_parts()
    except ImportError as exc:
        parser.exit(2, f'iPhone Voice needs the extras: uv pip install --python .venv/bin/python '
                       f'-e ".[web-voice]" ({exc})\n')
    except (OSError, RuntimeError, ValueError) as exc:  # no network for the first download, or an unusable name
        parser.exit(2, f'Cannot start the wake word: {exc}\n')
    tls = CACHE_DIR / 'phone-tls'
    try:
        host, new = prepare_https(tls)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'Cannot set up HTTPS: {exc}\n')
    print(parts.describe(label, 'iPhone Voice', address=f'https://{host}:{PORT}/#token={TOKEN}'))
    if new:  # the one step by hand, shown only now: the iPhone learns to trust this Mac
        print(f'\nFirst time on this Mac: let the iPhone trust it, once.\n\n'
              f'  1. Send     Finder shows {CA_FILE}: right-click > Share > AirDrop\n'
              f'  2. Install  iPhone Settings > General > VPN & Device Management > Install\n'
              f'  3. Trust    Settings > General > About > Certificate Trust Settings\n'
              f'              > turn on "piano2midi on {local_name().removesuffix(".local")}"\n'
              f'  4. Open     the address above in Safari > Share > Add to Home Screen\n\n'
              f'  The file: {tls / CA_FILE}\n', flush=True)
        if sys.platform == 'darwin':
            subprocess.run(['open', '-R', str(tls / CA_FILE)], check=False)
    said = ' or '.join(f'"{w}"' for w in parts.wake.words)
    print(f'Keep the address private; one phone at a time.\n'
          f'Tap the orb and say {said}, then your request. Control+C quits.\n', flush=True)
    # Only what matters shows, as with ./run.sh voice: no request log, no "running on 0.0.0.0" to mistake for the
    # address, and no error for quitting while a phone in the background never says goodbye (it stops in 2 s).
    logging.getLogger('uvicorn.error').addFilter(
        lambda record: 'timeout graceful shutdown exceeded' not in record.getMessage())
    uvicorn.run(app, host='0.0.0.0', port=PORT, ssl_certfile=str(tls / 'cert.pem'), ssl_keyfile=str(tls / 'key.pem'),
                ws_max_size=8192, ws_max_queue=4, timeout_graceful_shutdown=2, log_level='warning')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
