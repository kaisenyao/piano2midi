"""iPhone Voice: the phone as her microphone and speaker, and the server around it; no paid APIs or devices."""
import base64
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from piano2midi import voice
from piano2midi import web_voice as web


class PhoneTests(unittest.TestCase):
    def test_piano_disconnect_during_orb_pause_is_spoken_without_closing_session(self):
        from piano2midi.chat import Assistant
        from piano2midi.play import Player
        spoken, controls = [], []
        pcm = np.arange(240, dtype='<i2')
        def speech(text):
            spoken.append(text)
            return iter([pcm])
        parts = SimpleNamespace(stt=Mock(), wake=Mock(), speech=Mock(),
                                speaker=SimpleNamespace(stream=speech), greeter=Mock())
        player = Player(port=Mock())
        self.addCleanup(player.close)
        player.current, player.state = (Path('piece.mid'), '月光'), 'playing'
        player._sent_to = 22
        def opened(emit, control):
            controls.append(control)
            assistant = Assistant(Mock())
            assistant.request = '播放月光'
            assistant.player = control.player = player
            conversation = voice.Conversation(assistant, Mock(), Mock(), parts, web.Phone(emit, control), control)
            emit({'type': 'ready'})
            return conversation
        with patch.object(web, 'open_session', opened), \
             patch.object(player, 'check_connection', side_effect=LookupError('piano disconnected')), \
             TestClient(web.app) as client:
            with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as socket:
                socket.send_json({'token': web.TOKEN})
                self.assertEqual(socket.receive_json(), {'type': 'ready'})
                socket.send_json({'type': 'pause_session'})
                socket.send_json({'type': 'continue_session'})
                self.assertEqual(socket.receive_json(), {'type': 'state', 'state': 'speaking'})
                audio = socket.receive_json()
                self.assertEqual(audio['type'], 'audio')
                self.assertEqual(base64.b64decode(audio['data']), pcm.tobytes())
                self.assertEqual(len(spoken), 1)
                self.assertIn('还没连上琴', spoken[0])
                self.assertEqual(socket.receive_json(), {'type': 'turn_end', 'sleep': False})
                self.assertEqual(player.state, 'paused')
                self.assertEqual(player.position, 22)
                self.assertFalse(controls[0].paused)
                socket.send_json({'type': 'resume'})
                self.assertEqual(socket.receive_json(), {'type': 'state', 'state': 'conversation'})

    def test_missing_piano_reply_reaches_phone_from_shared_terminal_logic(self):
        from piano2midi.chat import PlaybackRequest
        from piano2midi.library import Library
        from piano2midi.play import Player
        for played_before in (False, True):
            with self.subTest(played_before=played_before), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'piece.mid'
                path.touch()
                lib = Library(Path(tmp) / 'library.db')
                events, spoken, control = [], [], web.PauseControl()
                pcm = np.arange(240, dtype='<i2')
                def speech(text, spoken=spoken, pcm=pcm):
                    spoken.append(text)
                    return iter([pcm])
                parts = SimpleNamespace(stt=Mock(return_value='继续播放'), wake=Mock(), speech=Mock(),
                                        speaker=SimpleNamespace(stream=speech), greeter=Mock())
                with patch('piano2midi.library.Library', return_value=lib), \
                     patch('piano2midi.chat.index_midi_folder'), \
                     patch.object(web, 'voice_chat', return_value=(Mock(), 'test')), \
                     patch.object(web, 'voice_parts', return_value=parts), \
                     patch('piano2midi.play.output_names', return_value=[]), \
                     patch('builtins.print'):
                    conversation = web.open_session(events.append, control)
                    if played_before:  # opened while the piano was there; it has gone since
                        with patch('piano2midi.play.output_names', return_value=['Network piano2midi']), \
                             patch('piano2midi.play.default_lead', return_value=0.0), patch('mido.open_output'):
                            conversation.assistant.player = Player()
                            conversation.assistant.player.check_connection()
                    pending = PlaybackRequest(path, '月光', from_s=22)
                    conversation.assistant.pending = pending
                    conversation.frames = [pcm]
                    conversation.respond()
                    expected = conversation.assistant.playback_reply({'error': 'playback_connection_unavailable'})
                    self.assertEqual(spoken, [expected])
                    self.assertIn('还没连上琴', expected)
                    audio = [event for event in events if event['type'] == 'audio']
                    self.assertEqual(len(audio), 1)
                    self.assertEqual(base64.b64decode(audio[0]['data']), pcm.tobytes())
                    self.assertEqual(events[-1], {'type': 'turn_end', 'sleep': False})
                    self.assertEqual(conversation.assistant.pending, pending)
                    conversation.close()

    def test_her_voice_goes_out_in_pieces_and_is_timed_for_the_piano(self):
        events, control = [], web.PauseControl()
        phone = web.Phone(events.append, control)
        phone.speak(iter([np.arange(24000, dtype=np.int16), np.zeros(12000, dtype=np.int16)]))
        self.assertEqual([e['type'] for e in events], ['audio', 'audio'])
        self.assertEqual(np.frombuffer(base64.b64decode(events[0]['data']), dtype='<i2')[:3].tolist(), [0, 1, 2])
        self.assertAlmostEqual(control.speech_until - control.now(), 1.5, delta=.05, msg='Pieces play in turn')
        control.speech_until = control.now() + .1
        started = time.monotonic()
        control.wait_for_speech()
        self.assertGreaterEqual(time.monotonic() - started, .15, 'Her speech, plus room for the network')
        control.speech_until = control.now() + 60
        control.close()
        with self.assertRaises(web.SessionEnded):
            control.wait_for_speech()

    def test_a_session_is_the_shared_conversation_on_the_phone(self):
        events, control = [], web.PauseControl()
        parts = Mock()
        parts.wake.heard.return_value = None
        with patch('piano2midi.library.Library'), patch('piano2midi.chat.index_midi_folder'), \
                patch.object(web, 'voice_chat', return_value=(Mock(), 'test')), \
                patch.object(web, 'voice_parts', return_value=parts):
            session = web.open_session(events.append, control)
        self.assertIsInstance(session, voice.Conversation)
        self.assertIsInstance(session.out, web.Phone)
        self.assertEqual(events, [{'type': 'ready'}, {'type': 'state', 'state': 'waiting'}])
        self.assertEqual(session.assistant.clock, control.now, 'A pause is not a quiet spell')
        self.assertEqual(session.assistant.speak, session.say)


class StartTests(unittest.TestCase):
    def test_one_command_serves_the_iphone_at_the_macs_address(self):
        parts = SimpleNamespace(stt=SimpleNamespace(label='OpenAI x'), speaker=SimpleNamespace(label='ElevenLabs y'))
        with tempfile.TemporaryDirectory() as tmp:
            with patch('piano2midi.library.CACHE_DIR', Path(tmp)), \
                 patch.object(web, 'voice_chat', return_value=(Mock(), 'test')), \
                 patch.object(web, 'voice_parts', return_value=SimpleNamespace(
                     describe=lambda model, name, **more: voice.VoiceParts.describe(parts, model, name, **more),
                     wake=SimpleNamespace(words=['Hey Jasmine']))), \
                 patch('piano2midi.web_tls.prepare_https', return_value=('192.168.1.20', True)), \
                 patch('piano2midi.web_tls.local_name', return_value='Test-Mac.local'), \
                 patch('uvicorn.run') as run, patch('builtins.print') as shown, \
                 patch.object(web, 'TOKEN', web.TOKEN), patch.object(web.sys, 'platform', 'darwin'), \
                 patch.object(web.subprocess, 'run') as opened:
                web.main([])
                token = web.TOKEN
        self.assertEqual(run.call_args.kwargs['host'], '0.0.0.0')
        self.assertEqual(run.call_args.kwargs['ssl_certfile'], str(Path(tmp) / 'phone-tls' / 'cert.pem'))
        self.assertLessEqual(run.call_args.kwargs['timeout_graceful_shutdown'], 3, 'One Control+C is enough')
        self.assertEqual(run.call_args.kwargs['log_level'], 'warning', 'The printed address is the only one shown')
        printed = ' '.join(str(c.args[0]) for c in shown.call_args_list if c.args)
        self.assertIn(f'https://192.168.1.20:8765/#token={token}', printed)
        self.assertIn('"piano2midi on Test-Mac"', printed, 'The certificate to trust, by its name')
        self.assertIn('AirDrop', printed, 'The first start says what to do on the iPhone')
        opened.assert_called_once_with(['open', '-R', str(Path(tmp) / 'phone-tls' / 'piano2midi-CA.cer')], check=False)


class TokenTests(unittest.TestCase):
    def test_token_is_kept_privately_across_restarts(self):
        with tempfile.TemporaryDirectory() as tmp, patch('piano2midi.library.CACHE_DIR', Path(tmp)):
            first = web.saved_token()
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(web.saved_token(), first)
            self.assertEqual((Path(tmp) / 'phone-token').stat().st_mode & 0o777, 0o600)


class TransportTests(unittest.TestCase):
    def test_pwa_assets_and_path_allowlist(self):
        with TestClient(web.app) as client:
            for name in ['', *web.ASSETS]:
                response = client.get('/' + name)
                self.assertEqual(response.status_code, 200, name)
            self.assertEqual(web.ASSETS | {'CUE_SOURCES.md', 'index.html'},
                             {path.name for path in web.WEB.iterdir() if not path.name.startswith('.')},
                             'Every page file is served (hidden files such as Finder\'s .DS_Store aside)')
            for removed in ('web_voice.py', 'CUE_SOURCES.md', '.env', 'sw.js', 'connect', 'settings',
                            'chat', 'text', 'api/chat', 'api/text', 'docs', 'redoc', 'openapi.json'):
                self.assertEqual(client.get('/' + removed).status_code, 404, removed)
            self.assertEqual({route.path for route in web.app.routes}, {'/', '/{name}', '/voice'})
            self.assertEqual(client.get('/').headers['cache-control'], 'no-store')
            self.assertNotIn('start_url', client.get('/manifest.webmanifest').json(),
                             'Add to Home Screen must keep the address with its #token')
            page = client.get('/').text
            self.assertEqual(page.count('<input'), 1)
            self.assertIn('id="voice-toggle" type="checkbox" switch role="switch"', page)
            for removed in ('<button', '<form', '<dialog', '<textarea', '<nav'):
                self.assertNotIn(removed, page)

    def test_invalid_origin_and_token_cannot_initialize_core(self):
        with patch.object(web, 'open_session') as session, TestClient(web.app) as client:
            for origin, token in [('https://other.example', web.TOKEN), ('http://testserver', 'wrong')]:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect('/voice', headers={'origin': origin}) as socket:
                        socket.send_json({'token': token})
                        socket.receive_json()
            session.assert_not_called()

    def test_invalid_handshake_payloads_are_closed_without_loading_models(self):
        with patch.object(web, 'open_session') as session, TestClient(web.app) as client:
            for payload in ({'token': None}, {'token': 123}, {'token': '无效凭证'}, []):
                with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as socket:
                    socket.send_json(payload)
                    with self.assertRaises(WebSocketDisconnect) as error:
                        socket.receive_json()
                    self.assertEqual(error.exception.code, 1008)
            with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as socket:
                socket.send_bytes(b'not a JSON handshake')
                with self.assertRaises(WebSocketDisconnect) as error:
                    socket.receive_json()
                self.assertEqual(error.exception.code, 1008)
            session.assert_not_called()

    def test_second_phone_is_rejected_and_owner_released(self):
        class Session:
            def __init__(self, emit, control):
                emit({'type': 'ready'})
            def close(self):
                pass
        with patch.object(web, 'open_session', Session), TestClient(web.app) as client:
            with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as first:
                first.send_json({'token': web.TOKEN})
                self.assertEqual(first.receive_json()['type'], 'ready')
                with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as second:
                    second.send_json({'token': web.TOKEN})
                    with self.assertRaises(WebSocketDisconnect) as error:
                        second.receive_json()
                    self.assertEqual(error.exception.code, 1013)
            with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as third:
                third.send_json({'token': web.TOKEN})
                self.assertEqual(third.receive_json()['type'], 'ready')

    def test_removed_keyboard_command_and_unknown_payloads_are_rejected(self):
        class Session:
            def __init__(self, emit, control):
                emit({'type': 'ready'})
            def close(self):
                pass
        with patch.object(web, 'open_session', Session), TestClient(web.app) as client:
            for payload in ({'type': 'text', 'text': 'old client'}, {'type': 'unknown'}, [], '{bad-json'):
                with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as socket:
                    socket.send_json({'token': web.TOKEN})
                    self.assertEqual(socket.receive_json()['type'], 'ready')
                    if isinstance(payload, str):
                        socket.send_text(payload)
                    else:
                        socket.send_json(payload)
                    with self.assertRaises(WebSocketDisconnect) as error:
                        socket.receive_json()
                    self.assertEqual(error.exception.code, 1008)

    def test_session_resources_stay_on_one_worker_thread(self):
        threads = []
        class Session:
            def __init__(self, emit, control):
                threads.append(threading.get_ident())
                self.emit = emit
                emit({'type': 'ready'})
            def frame(self, data):
                threads.append(threading.get_ident())
                self.emit({'type': 'frame', 'size': len(data)})
            def resume(self):
                threads.append(threading.get_ident())
                self.emit({'type': 'resumed'})
            def close(self):
                threads.append(threading.get_ident())
        with patch.object(web, 'open_session', Session), TestClient(web.app) as client:
            with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as socket:
                socket.send_json({'token': web.TOKEN})
                self.assertEqual(socket.receive_json()['type'], 'ready')
                socket.send_bytes(b'\0' * voice.FRAME * 2)
                self.assertEqual(socket.receive_json()['size'], voice.FRAME, 'PCM16 samples reach the session')
                socket.send_json({'type': 'resume'})
                self.assertEqual(socket.receive_json()['type'], 'resumed')
        self.assertEqual(len(threads), 4)
        self.assertEqual(len(set(threads)), 1)


if __name__ == '__main__':
    unittest.main()
