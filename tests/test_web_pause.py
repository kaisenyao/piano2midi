"""Pause/resume regressions: no network APIs, real microphone or MIDI devices."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np
from fastapi.testclient import TestClient
from piano2midi import voice
from piano2midi import web_voice as web
from piano2midi.play import Player

FIRST, QUEUED, PRIVATE = (np.full(voice.FRAME, n, dtype='<i2').tobytes() for n in (1, 2, 3))


def wait_for(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(.01)
    return True


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.control = web.PauseControl()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.pool.shutdown, wait=True)
        self.addCleanup(self.control.close)

    def test_task_finishes_current_step_and_waits_before_next(self):
        entered, finish, held = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        effects = []
        def operation():
            entered.set()
            finish.wait(2)
            effects.append('current')
        checkpoint = self.control.checkpoint
        def observed():
            if self.control.paused:
                held.set()
            checkpoint()
        self.control.checkpoint = observed
        def task():
            self.control.guard(operation)()
            effects.append('next')
        future = self.pool.submit(task)
        self.assertTrue(entered.wait(2))
        self.control.set_paused(True)
        finish.set()
        self.assertTrue(held.wait(2))
        self.assertEqual(effects, ['current'])
        self.control.set_paused(False)
        future.result(timeout=2)
        self.assertEqual(effects, ['current', 'next'])

    def test_close_unblocks_a_paused_worker_without_running_next_step(self):
        self.control.set_paused(True)
        next_step = Mock()
        future = self.pool.submit(self.control.guard(next_step))
        self.control.close()
        with self.assertRaises(web.SessionEnded):
            future.result(timeout=2)
        next_step.assert_not_called()

    def test_disconnect_stops_music_before_waiting_for_task_cleanup(self):
        self.control.player = Mock(state='playing')
        self.control.close()
        self.control.player.stop.assert_called_once()

    def test_followup_clock_excludes_pause_duration(self):
        with patch.object(voice.time, 'monotonic', return_value=100) as clock:
            self.control.set_paused(True)
            clock.return_value = 300
            self.assertEqual(self.control.now(), 100)
            self.control.set_paused(False)
            self.assertEqual(self.control.now(), 100)
            clock.return_value = 305
            self.assertEqual(self.control.now(), 105)

    def test_only_music_playing_before_the_pause_resumes(self):
        player = self.control.player = Mock(state='playing')
        player.pause.side_effect = lambda: setattr(player, 'state', 'paused')
        player.resume.side_effect = lambda: setattr(player, 'state', 'playing')
        self.control.set_paused(True)
        self.control.set_paused(True)
        player.pause.assert_called_once()
        self.control.set_paused(False)
        self.control.set_paused(False)
        player.resume.assert_called_once()
        player.state = 'paused'
        self.control.set_paused(True)
        self.control.set_paused(False)
        player.resume.assert_called_once()

    def test_phone_music_is_timed_like_the_mac(self):
        # The same Player() as ./run.sh voice, so notes leave one lead early with their time.
        with patch.object(Player, '__init__', return_value=None) as init:
            web.phone_player(self.control)
        self.control.player = None  # never set up, so nothing to stop
        init.assert_called_once_with()

    def test_new_music_cannot_start_while_intro_is_paused(self):
        # Use the real Player setup/start path, replacing only physical MIDI and the playback loop.
        with patch('piano2midi.play.find_port', return_value='test'), \
             patch('piano2midi.play.default_lead', return_value=0.0), \
             patch('mido.open_output', return_value=Mock()), \
             patch.object(Player, 'check_connection'), patch.object(Player, 'play') as play:
            player = web.phone_player(self.control)
            self.addCleanup(player.close)
            introduced, held = threading.Event(), threading.Event()
            checkpoint = self.control.checkpoint
            def observed():
                if self.control.paused:
                    held.set()
                checkpoint()
            self.control.checkpoint = observed
            def introduction():
                self.control.set_paused(True)
                introduced.set()
            future = self.pool.submit(player.start, [(Path('test.mid'), 'test')], 7.25, introduction)
            self.assertTrue(introduced.wait(2))
            self.assertTrue(held.wait(2))
            play.assert_not_called()
            self.control.set_paused(False)
            future.result(timeout=2)
            player._thread.join(timeout=2)
            self.assertEqual(player._offset, 7.25)
            self.assertEqual(play.call_args.args[1], 7.25)

    def test_music_starts_after_her_introduction_has_played(self):
        with patch('piano2midi.play.find_port', return_value='test'), \
             patch('piano2midi.play.default_lead', return_value=0.0), \
             patch('mido.open_output', return_value=Mock()), \
             patch.object(Player, 'check_connection'), patch.object(Player, 'play') as play:
            player = web.phone_player(self.control)
            self.addCleanup(player.close)
            def introduction():
                self.control.speech_until = self.control.now() + .2
            started = time.monotonic()
            player.start([(Path('test.mid'), 'test')], 0.0, introduction)
            player._thread.join(timeout=2)
            self.assertGreaterEqual(time.monotonic() - started, .25)
            play.assert_called_once()
            self.control.speech_until = self.control.now() + 5
            started = time.monotonic()
            player.start([(Path('test.mid'), 'test')], 0.0)
            self.assertLess(time.monotonic() - started, 1, 'Only an introduction is waited for')

    def test_actual_assistant_holds_model_result_and_tools_until_continue(self):
        entered, release, held = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        lib, chat = Mock(), Mock()
        lib.search.return_value = []
        def model(text):
            entered.set()
            release.wait(2)
            return SimpleNamespace(text='', tool_calls=[SimpleNamespace(name='search_library', arguments={'query': 'moon'}, id='1')])
        chat.send_user.side_effect = model
        chat.send_tool_results.return_value = SimpleNamespace(text='Found it.', tool_calls=[])
        checkpoint = self.control.checkpoint
        def observed():
            if self.control.paused:
                held.set()
            checkpoint()
        self.control.checkpoint = observed
        with patch('piano2midi.library.Library', return_value=lib), \
             patch('piano2midi.chat.index_midi_folder'), \
             patch('piano2midi.llm.make_chat', return_value=(chat, 'test')), \
             patch.object(web, 'voice_parts', return_value=Mock()):
            session = web.open_session(lambda event: None, self.control)
        self.addCleanup(session.close)
        future = self.pool.submit(session.assistant.ask, session.chat, 'find moon')
        self.assertTrue(entered.wait(2))
        self.control.set_paused(True)
        release.set()
        self.assertTrue(held.wait(2))
        lib.search.assert_not_called()
        self.control.set_paused(False)
        self.assertEqual(future.result(timeout=2), 'Found it.')
        lib.search.assert_called_once_with('moon')


class PauseTransportTests(unittest.TestCase):
    def test_controls_remain_responsive_during_task_and_preserve_session(self):
        entered, release, held, closed = (threading.Event() for _ in range(4))
        frames = []
        instances = []
        class Session:
            def __init__(self, emit, control):
                self.emit, self.control = emit, control
                instances.append(self)
                checkpoint = control.checkpoint
                def observed():
                    if control.paused:
                        held.set()
                    checkpoint()
                control.checkpoint = observed
                emit({'type': 'ready'})
            def frame(self, data):
                frames.append(data.tobytes())
                entered.set()
                release.wait(2)
                self.control.checkpoint()
                self.emit({'type': 'completed'})
            def resume(self):
                self.emit({'type': 'resumed'})
            def close(self):
                closed.set()
        with patch.object(web, 'open_session', Session), TestClient(web.app) as client:
            try:
                with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as socket:
                    socket.send_json({'token': web.TOKEN})
                    self.assertEqual(socket.receive_json()['type'], 'ready')
                    socket.send_bytes(FIRST)
                    self.assertTrue(entered.wait(2))
                    socket.send_bytes(QUEUED)
                    socket.send_json({'type': 'pause_session'})
                    socket.send_bytes(PRIVATE)
                    release.set()
                    self.assertTrue(held.wait(2))
                    self.assertEqual(frames, [FIRST])
                    socket.send_json({'type': 'continue_session'})
                    self.assertEqual(socket.receive_json(), {'type': 'completed'})
                    socket.send_json({'type': 'resume'})
                    self.assertEqual(socket.receive_json(), {'type': 'resumed'})
                    self.assertEqual(frames, [FIRST], 'Queued pre-pause microphone data must not replay')
                    self.assertEqual(len(instances), 1)
                    socket.send_json({'type': 'pause_session'})
                    self.assertTrue(wait_for(lambda: instances[0].control.paused))
                self.assertTrue(closed.wait(2), 'Disconnect must release the paused session')
            finally:
                release.set()

    def test_disconnect_while_worker_is_paused_releases_owner(self):
        entered, released, held, closed = (threading.Event() for _ in range(4))
        controls = []
        class Session:
            def __init__(self, emit, control):
                self.emit, self.control = emit, control
                controls.append(control)
                emit({'type': 'ready'})
            def frame(self, data):
                entered.set()
                released.wait(2)
                held.set()
                self.control.checkpoint()
                raise AssertionError('Disconnected task must not continue')
            def close(self):
                closed.set()
        with patch.object(web, 'open_session', Session), TestClient(web.app) as client:
            try:
                with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as socket:
                    socket.send_json({'token': web.TOKEN})
                    self.assertEqual(socket.receive_json()['type'], 'ready')
                    socket.send_bytes(FIRST)
                    self.assertTrue(entered.wait(2))
                    socket.send_json({'type': 'pause_session'})
                    self.assertTrue(wait_for(lambda: controls[0].paused))
                    released.set()
                    self.assertTrue(held.wait(2))
                self.assertTrue(closed.wait(2))
                with client.websocket_connect('/voice', headers={'origin': 'http://testserver'}) as again:
                    again.send_json({'token': web.TOKEN})
                    self.assertEqual(again.receive_json()['type'], 'ready')
            finally:
                released.set()
