"""Playback state and tool-boundary tests; no real API calls, devices, or keys."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import mido
from piano2midi.chat import Assistant, TOOLS
from piano2midi.library import Library
from piano2midi.play import Player
from piano2midi.playback_commands import match_command
from piano2midi.voice import finished_remark, finish_interaction


def call(name, **arguments):
    return SimpleNamespace(name=name, arguments=arguments)


class ScriptedChat:
    """Only supplies model outputs; real Assistant dispatch and Player state are exercised."""
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.messages, self.results = [], []
        self.resets = 0

    def next(self):
        step = next(self.steps, 'Done.')
        return SimpleNamespace(text=step if isinstance(step, str) else '',
                               tool_calls=[] if isinstance(step, str) else step)

    def send_user(self, text):
        self.messages.append(text)
        return self.next()

    def send_tool_results(self, results):
        self.results.extend(json.loads(raw) for _, raw in results)
        return self.next()

    def reset(self):
        self.resets += 1


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.root_context = tempfile.TemporaryDirectory()
        self.addCleanup(self.root_context.cleanup)
        self.root = Path(self.root_context.name)
        self.lib = Library(self.root/'library.db')
        self.addCleanup(self.lib.db.close)
        self.records = []
        for title in ('\u8d1d\u52a0\u5c14\u6e56\u7554', '\u6708\u5149'):
            path = self.root/(title+'.mid')
            midi = mido.MidiFile(); track = mido.MidiTrack(); midi.tracks.append(track)
            track.append(mido.Message('note_on', note=60, velocity=64, time=0))
            track.append(mido.Message('note_off', note=60, time=480*240))
            midi.save(path)
            record = self.lib.add(path, title)
            metadata = ({'composer':'\u674e\u5065', 'aliases':'Li Jian \u8d1d\u52a0\u5c14\u6e56\u7554', 'tags':'piano'} if title == '\u8d1d\u52a0\u5c14\u6e56\u7554'
                        else {'composer':'Claude Debussy \u5fb7\u5f6a\u897f', 'aliases':'Clair de Lune \u6708\u5149', 'tags':'piano'})
            self.lib.update(record['id'], **metadata)
            self.records.append(self.lib.get(record['id']))
        self.a = Assistant(self.lib)
        self.player = self.a.player = Player(port=Mock())
        self.connected = True
        def check():
            if not self.connected: raise LookupError('playback_connection_unavailable')
        checker = patch.object(self.player, 'check_connection', side_effect=check)
        checker.start(); self.addCleanup(checker.stop)
        midi_io = patch.object(self.player, 'play')
        midi_io.start(); self.addCleanup(midi_io.stop)
        self.spoken = []
        self.a.speak = self.spoken.append
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__(); self.addCleanup(output.__exit__, None, None, None)
        self.addCleanup(self.a.close)

    def prime(self, offset=22):
        self.a.request = '\u64ad\u653e\u8d1d\u52a0\u5c14\u6e56\u7554'
        self.a.play(self.records[0]['id'])
        self.player._thread.join()
        self.player._sent_to = offset
        self.a.pause()
        self.spoken.clear()

    def test_tools_separate_selection_from_controls(self):
        tools = {t['name']: t for t in TOOLS}
        self.assertNotIn('play', tools)
        self.assertEqual(set(tools['play_recording']['parameters']['properties']), {'id'})
        self.assertEqual(set(tools['control_playback']['parameters']['properties']), {'action'})
        self.assertFalse(tools['control_playback']['parameters']['additionalProperties'])

    def test_unoffered_id_cannot_change_current_or_pending_piece(self):
        self.prime(); self.connected = False
        self.a.control_playback('resume')
        pending = self.a.pending
        for record_id in (999, self.records[1]['id']):
            result = json.loads(self.a.run_tool('play_recording', {'id': record_id}))
            self.assertEqual(result['error'], 'selection_required')
            self.assertEqual(self.a.pending, pending)
        self.assertEqual(self.player.current[1], '\u8d1d\u52a0\u5c14\u6e56\u7554')

    def test_fixture_has_composer_and_aliases_for_realistic_new_song_search(self):
        results = self.lib.search('\u5fb7\u5f6a\u897f \u6708\u5149 Clair de Lune Debussy piano')
        moonlight = next(r for r in results if r['title'] == '\u6708\u5149')
        self.assertIn('Debussy', moonlight['composer'])
        self.assertIn('Clair de Lune', moonlight['aliases'])

    def test_waiting_preserves_pending_request_and_position(self):
        self.prime(); self.connected = False
        self.a.control_playback('resume')
        pending = self.a.pending
        chat = ScriptedChat([call('control_playback', action='pause')], '\u597d\uff0c\u51c6\u5907\u597d\u518d\u53eb\u6211\u3002')
        self.a.ask(chat, '\u5148\u522b\u7ee7\u7eed\uff0c\u6211\u8fd8\u6ca1\u8fde\u597d')
        self.assertEqual(self.player.state, 'paused')
        self.assertEqual(self.player.position, 22)
        self.assertEqual(self.a.pending, pending)

    def test_selected_new_song_requires_actual_search_result(self):
        chat = ScriptedChat([call('search_library', query='\u6708\u5149')],
                            [call('play_recording', id=self.records[1]['id'])])
        self.a.ask(chat, '\u6211\u60f3\u542c\u6708\u5149')
        self.assertEqual(self.player.current[1], '\u6708\u5149')
        self.assertIsNone(self.a.pending)

    def test_selection_permission_expires_after_request(self):
        self.a.run_tool('search_library', {'query': '\u6708\u5149'})
        chat = ScriptedChat([call('play_recording', id=self.records[1]['id'])])
        self.a.ask(chat, '\u804a\u804a\u97f3\u4e50')
        self.assertEqual(chat.results[0]['error'], 'selection_required')
        self.assertIsNone(self.player.current)

    def test_transcription_can_offer_a_new_recording(self):
        with patch.object(self.a, 'transcribe_local', return_value=self.records[1]):
            self.a.run_tool('transcribe_local', {'path':'input.wav', 'title':'\u6708\u5149'})
        self.assertEqual(self.a.play_recording(self.records[1]['id'])['status'], 'playing')

    def test_extra_control_id_is_rejected_without_state_change(self):
        self.prime()
        result = json.loads(self.a.run_tool('control_playback', {'action':'restart', 'id':999}))
        self.assertEqual(result['error'], 'invalid_playback_arguments')
        self.assertEqual(self.player.state, 'paused')
        self.assertEqual(self.player.position, 22)

    def test_playback_argument_types_cannot_mutate_state(self):
        self.prime()
        for tool, arguments in [('play_recording', {'id': True}), ('play_recording', {'id':'1'}),
                                ('control_playback', {'action':['resume']})]:
            result = json.loads(self.a.run_tool(tool, arguments))
            self.assertEqual(result['error'], 'invalid_playback_arguments')
        self.assertEqual(self.player.state, 'paused')
        self.assertEqual(self.player.position, 22)

    def test_network_checks_skip_direct_ports_and_distinguish_unknown_from_missing(self):
        import hashlib
        import os
        import piano2midi.network_midi as network
        with patch('piano2midi.network_midi.sys.platform', 'darwin'),              patch.dict(os.environ, {'PIANO2MIDI_NETWORK_PEER':'Test Device', 'PIANO2MIDI_NETWORK_PORT':'Network RTP Session 1'}),              patch('piano2midi.library.CACHE_DIR', self.root),              patch('piano2midi.network_midi.shutil.which', return_value='/usr/bin/swiftc'):
            binary = self.root/'network-midi'/('check-'+hashlib.sha256(network._SWIFT.encode()).hexdigest()[:16])
            binary.parent.mkdir(); binary.touch()
            with patch('piano2midi.network_midi.subprocess.run') as run:
                self.assertTrue(network.check_network_midi('USB Piano'))
                self.assertTrue(network.check_network_midi('Bluetooth Piano'))
                run.assert_not_called()
            for code, status, expected in [(0,'READY',True),(0,'UNKNOWN',True),(1,'MISSING',False)]:
                with patch('piano2midi.network_midi.subprocess.run', return_value=SimpleNamespace(returncode=code,stdout=status)):
                    self.assertEqual(network.check_network_midi('Network RTP Session 1'), expected)

    def test_model_can_correct_wrong_selection_tool_to_control(self):
        self.prime()
        chat = ScriptedChat([call('play_recording', id=999)], [call('control_playback', action='resume')])
        answer = self.a.ask(chat, '\u597d\u50cf\u65ad\u5f00\u4e86\uff0c\u4f60\u53ef\u4ee5\u770b\u770b\u80fd\u5e2e\u6211\u7ee7\u7eed\u64ad\u653e\u5417\uff1f')
        self.player._thread.join()
        self.assertEqual(chat.results[0]['error'], 'selection_required')
        self.assertEqual(self.player._from, 22)
        self.assertEqual(answer, '')
        self.assertFalse(any('\u627e\u4e0d\u5230' in line or '\u9009\u4e2d' in line for line in self.spoken))

    def test_model_can_correct_control_parameters(self):
        self.prime()
        chat = ScriptedChat([call('control_playback', action='resume', id=999)],
                            [call('control_playback', action='resume')])
        self.a.ask(chat, '\u4f60\u80fd\u5e2e\u6211\u63a5\u7740\u5f39\u5417')
        self.assertEqual(chat.results[0]['error'], 'invalid_playback_arguments')
        self.assertEqual(self.player._from, 22)

    def test_long_request_model_path_matrix(self):
        cases = [
            ('\u5443\uff0c\u597d\u50cf\u65ad\u5f00\u4e86\uff0c\u4f60\u53ef\u4ee5\u518d\u5e2e\u6211\u91cd\u65b0\u64ad\u653e\u5417\uff1f', 'restart', 0),
            ('\u5c0f\u660e\u540c\u5b66\u597d\u50cf\u53c8\u65ad\u5f00\u4e86\uff0c\u4f60\u53ef\u4ee5\u770b\u770b\u80fd\u5e2e\u6211\u7ee7\u7eed\u64ad\u653e\u5417\uff1f', 'resume', 22),
            ('\u6211\u60f3\u7ee7\u7eed\u542c\u8fd9\u9996\u6b4c', 'resume', 22),
            ('\u6211\u5df2\u7ecf\u8fde\u597d\u4e86\uff0c\u4f60\u5e2e\u6211\u518d\u68c0\u67e5\u518d\u8bd5\u4e00\u6b21\u597d\u5417', 'retry', 22),
            ('\u521a\u624d\u90a3\u9996\u4ece\u5f00\u5934\u518d\u6765\u4e00\u904d\u5427', 'restart', 0),
            ('Could you carry on with the piece we were hearing?', 'resume', 22),
        ]
        for text, action, offset in cases:
            with self.subTest(text=text):
                self.prime()
                chat = ScriptedChat([call('control_playback', action=action)])
                self.a.ask(chat, text)
                self.player._thread.join()
                self.assertEqual(self.player._from, offset)
                self.assertEqual(self.player.current[1], '\u8d1d\u52a0\u5c14\u6e56\u7554')

    def test_midpiece_disconnect_then_repeated_retry_preserves_position(self):
        self.prime(); self.connected = False
        for text in ('\u597d\u50cf\u505c\u4e86\uff0c\u80fd\u518d\u5e2e\u6211\u8bd5\u4e00\u6b21\u5417', '\u6211\u5df2\u7ecf\u8fde\u63a5\u597d\u4e86'):
            chat = ScriptedChat([call('control_playback', action='retry')])
            result = self.a.ask(chat, text)
            self.assertIn('\u8bbe\u5907\u548c\u94a2\u7434', result)
            self.assertEqual(self.a.pending.from_s, 22)
        self.connected = True
        self.a.ask(ScriptedChat([call('control_playback', action='retry')]), '\u73b0\u5728\u597d\u4e86\uff0c\u5e2e\u6211\u7ee7\u7eed')
        self.assertEqual(self.player._from, 22)
        self.assertIsNone(self.a.pending)

    def test_failed_restart_retries_from_zero(self):
        self.prime(); self.connected = False
        self.a.control_playback('restart')
        self.assertEqual(self.a.pending.from_s, 0)
        self.connected = True
        self.a.control_playback('retry')
        self.assertEqual(self.player._from, 0)

    def test_failed_new_selection_preserves_old_current_until_retry(self):
        self.prime(); self.connected = False
        self.a.run_tool('search_library', {'query':'\u6708\u5149'})
        self.a.play_recording(self.records[1]['id'])
        self.assertEqual(self.player.current[1], '\u8d1d\u52a0\u5c14\u6e56\u7554')
        self.assertEqual(self.a.pending.title, '\u6708\u5149')
        self.connected = True; self.a.control_playback('retry')
        self.assertEqual(self.player.current[1], '\u6708\u5149')

    def test_cancel_clears_pending_and_stops(self):
        self.prime(); self.connected = False; self.a.control_playback('resume')
        self.a.control_playback('cancel')
        self.assertIsNone(self.a.pending)
        self.assertEqual(self.player.state, 'stopped')
        self.connected = True; self.a.control_playback('restart')
        self.assertEqual(self.player._from, 0)

    def test_no_current_control_requests_selection_without_claiming_missing_file(self):
        result = self.a.ask(ScriptedChat([call('control_playback', action='resume')]), '\u80fd\u7ee7\u7eed\u5417')
        self.assertIn('\u66f2\u540d', result)
        self.assertNotIn('\u6587\u4ef6', result)

    def test_only_actual_file_loss_reports_unavailable(self):
        self.prime(); self.player.current[0].unlink()
        result = self.a.control_playback('resume')
        self.assertEqual(result['error'], 'recording_unavailable')

    def test_current_state_is_supplied_to_model(self):
        self.prime(); chat = ScriptedChat('\u8fd9\u9996\u5f88\u52a8\u542c\u3002')
        self.a.ask(chat, '\u8fd9\u9996\u53eb\u4ec0\u4e48')
        self.assertIn('\u8d1d\u52a0\u5c14\u6e56\u7554', chat.messages[0])
        self.assertIn('22', chat.messages[0])
        self.assertIn('paused', chat.messages[0])

    def test_negations_and_new_song_titles_remain_model_requests(self):
        for text in ('\u4e0d\u8981\u7ee7\u7eed\u64ad\u653e', '\u6211\u60f3\u7ee7\u7eed\u542c\u6708\u5149', '\u4f60\u80fd\u770b\u770b\u80fd\u5e2e\u6211\u7ee7\u7eed\u5417'):
            self.assertIsNone(match_command(text))
        self.prime()
        self.a.ask(ScriptedChat('\u597d\uff0c\u5148\u4e0d\u7ee7\u7eed\u3002'), '\u5148\u522b\u7ee7\u7eed\uff0c\u542c\u6211\u8bf4')
        self.assertEqual(self.player.state, 'paused')

    def test_quick_controls_share_the_same_dispatch(self):
        self.prime()
        self.a.ask(ScriptedChat(), '\u7ee7\u7eed\u64ad\u653e')
        self.player._thread.join()
        self.player._sent_to = 30
        self.a.ask(ScriptedChat(), '\u505c\u6b62\u64ad\u653e')
        self.assertEqual(self.player.position, 30)
        self.a.ask(ScriptedChat(), '\u4ece\u5934\u5f39')
        self.assertEqual(self.player._from, 0)

    def test_finish_is_delayed_and_consumed_once(self):
        player = SimpleNamespace(completed=('Song',100), state='stopped')
        with patch('piano2midi.voice.time.perf_counter', return_value=100.9):
            self.assertIsNone(finished_remark(player,'zh'))
        with patch('piano2midi.voice.time.perf_counter', return_value=101):
            self.assertIsNotNone(finished_remark(player,'zh'))
            self.assertIsNone(finished_remark(player,'zh'))

    def test_auto_resume_failure_replaces_final_reply_with_one_fault(self):
        self.prime(); self.connected = False
        self.a.touched_playback = False
        self.a.request = '\u73b0\u5728\u597d\u50cf\u65ad\u5f00\u4e86\uff0c\u4f60\u5e2e\u6211\u770b\u4e00\u4e0b'
        speaker = Mock()
        finish_interaction(self.a, speaker, '\u505c\u572822\u79d2\uff0c\u7b49\u8fde\u63a5\u597d\u4e86\u7ee7\u7eed\u3002', True)
        speaker.say.assert_called_once()
        self.assertIn('\u8bbe\u5907\u548c\u94a2\u7434', speaker.say.call_args.args[0])
        self.assertEqual(self.player.state, 'paused')
        self.assertEqual(self.player.position, 22)

    def test_auto_resume_checks_then_speaks_then_plays(self):
        self.prime(); self.a.touched_playback = False
        events = []
        speaker = Mock(); speaker.say.side_effect = lambda text: events.append(('reply', text))
        with patch.object(self.player, 'check_connection', side_effect=lambda: events.append('check')),              patch.object(self.player, 'play', side_effect=lambda *args: events.append('play')):
            finish_interaction(self.a, speaker, '\u662f\u8d1d\u52a0\u5c14\u6e56\u7554\u3002', True)
            self.player._thread.join()
        self.assertEqual(events, ['check', ('reply','\u662f\u8d1d\u52a0\u5c14\u6e56\u7554\u3002'), 'play'])
        self.assertEqual(self.player._from, 22)

    def test_explicit_pause_does_not_auto_resume(self):
        self.prime(); self.a.touched_playback = True
        speaker = Mock()
        with patch.object(self.player, 'start') as start:
            finish_interaction(self.a, speaker, '\u597d\uff0c\u5148\u6682\u505c\u3002', True)
            start.assert_not_called()
        speaker.say.assert_called_once_with('\u597d\uff0c\u5148\u6682\u505c\u3002')

    def test_short_pause_has_one_spoken_confirmation_and_keeps_position(self):
        self.prime()
        answer = self.a.ask(ScriptedChat(), "\u505c\u6b62\u64ad\u653e")
        self.assertTrue(answer)
        self.assertNotIn("\u64ad\u653e", answer)
        speaker = Mock()
        finish_interaction(self.a, speaker, answer, True)
        speaker.say.assert_called_once_with(answer)
        self.assertEqual(self.player.state, 'paused')
        self.assertEqual(self.player.position, 22)

    def test_resume_and_restart_introductions_are_spoken_once(self):
        self.prime()
        self.a.ask(ScriptedChat(), "\u7ee7\u7eed\u64ad\u653e")
        self.player._thread.join()
        self.assertEqual(len(self.spoken), 1)
        self.assertIn("\u5f39", self.spoken[0])
        self.assertEqual(self.player._from, 22)
        self.a.pause(); self.spoken.clear()
        answer = self.a.ask(ScriptedChat(), "\u4ece\u5934\u5f39")
        self.player._thread.join()
        self.assertEqual(answer, '')
        self.assertEqual(len(self.spoken), 1)
        self.assertTrue("\u4ece\u5934" in self.spoken[0] or "\u5f00\u59cb" in self.spoken[0])
        self.assertEqual(self.player._from, 0)

    def test_model_pause_uses_one_fallback_only_if_final_reply_is_empty(self):
        for final_reply in ('', "\u597d\u5440\uff0c\u6211\u5148\u505c\u4e00\u4e0b\u3002"):
            with self.subTest(final_reply=final_reply):
                self.prime()
                chat = ScriptedChat([call('control_playback', action='pause')], final_reply)
                answer = self.a.ask(chat, "\u80fd\u5148\u6682\u505c\u4e00\u4e0b\u8fd9\u9996\u5417")
                self.assertTrue(answer)
                if final_reply: self.assertEqual(answer, final_reply)
                self.assertEqual(self.spoken, [])
                speaker = Mock()
                finish_interaction(self.a, speaker, answer, True)
                speaker.say.assert_called_once()

    def test_failed_restart_does_not_speak_a_success_confirmation(self):
        self.prime(); self.connected = False
        answer = self.a.ask(ScriptedChat(), "\u4ece\u5934\u5f39")
        self.assertIn("\u8bbe\u5907\u548c\u94a2\u7434", answer)
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.a.pending.from_s, 0)
        self.assertEqual(self.player.state, 'paused')

    def test_name_only_request_is_local_and_preserves_playback(self):
        self.prime()
        for text in ("\u5c0f\u8317\u540c\u5b66", "\u5c0f\u660e\u540c\u5b66", "Hey Jasmine"):
            with self.subTest(text=text):
                chat = Mock()
                answer = self.a.ask(chat, text)
                self.assertTrue(answer)
                self.assertNotIn("\u770b\u770b", answer)
                chat.send_user.assert_not_called()
                self.assertEqual(self.player.state, 'paused')
                self.assertEqual(self.player.position, 22)

    def test_repeated_name_after_greeting_is_not_sent_to_chat(self):
        import numpy as np
        import piano2midi.voice as voice
        mic = Mock(); mic.read.side_effect = [np.zeros(voice.FRAME, dtype=np.int16), KeyboardInterrupt()]
        wake = Mock(words=["\u5c0f\u8317\u540c\u5b66"]); wake.heard.return_value = "\u5c0f\u8317\u540c\u5b66"
        speech = Mock(return_value=False)
        greeter = Mock(return_value="\u6211\u5728\u5462")
        assistant = Mock(player=None)
        with patch('piano2midi.library.Library'),              patch('piano2midi.chat.index_midi_folder'),              patch('piano2midi.chat.Assistant', return_value=assistant),              patch('piano2midi.llm.make_chat', return_value=(Mock(), 'test')),              patch.multiple(voice, Speaker=Mock(), Transcriber=Mock(return_value=Mock(return_value="\u5c0f\u8317\u540c\u5b66")),
                            WakeWord=Mock(return_value=wake), Speech=Mock(return_value=speech),
                            Microphone=Mock(return_value=mic), Greeter=Mock(return_value=greeter),
                            record=Mock(return_value=np.zeros(voice.FRAME))):
            self.assertEqual(voice.main([]), 0)
        greeter.assert_called_once()
        assistant.ask.assert_not_called()

    def test_wait_message_only_once_during_model_delay(self):
        from concurrent.futures import TimeoutError
        reply = SimpleNamespace(text='\u60f3\u542c\u54ea\u9996\uff1f', tool_calls=[])
        future = Mock(); future.result.side_effect=[TimeoutError(), reply]
        pool = Mock(); pool.submit.return_value=future
        with patch('piano2midi.chat.ThreadPoolExecutor', return_value=pool):
            self.a.ask(Mock(), '\u60f3\u542c\u6b4c')
        self.assertEqual(self.spoken, ['\u597d\u5440\uff0c\u6211\u770b\u770b\u3002'])

    def test_silent_model_announces_before_long_tools_only_once(self):
        for first_tool in ('search_youtube', 'transcribe_url', 'transcribe_local'):
            with self.subTest(first_tool=first_tool):
                self.spoken.clear()
                chat = ScriptedChat([call(first_tool)], [call('transcribe_url')], 'Done.')
                def run_tool(name, arguments):
                    self.assertEqual(len(self.spoken), 1)  # feedback precedes the blocking work
                    return '{}'
                with patch.object(self.a, 'run_tool', side_effect=run_tool):
                    self.a.ask(chat, '\u6211\u60f3\u542c\u7ea2\u8c46')
                self.assertIn('\u7a0d\u7b49', self.spoken[0])
                self.assertNotIn('\u5206\u949f', self.spoken[0])

    def test_model_wait_message_is_preserved_without_fallback_in_both_modes(self):
        line = "I'll find a lovely piano performance for you; please give me a moment."
        for voice in (True, False):
            with self.subTest(voice=voice):
                self.spoken.clear()
                self.a.speak = self.spoken.append if voice else None
                chat = Mock()
                chat.send_user.return_value = SimpleNamespace(text=line, tool_calls=[call('search_youtube')])
                chat.send_tool_results.side_effect = [
                    SimpleNamespace(text='', tool_calls=[call('transcribe_url')]),
                    SimpleNamespace(text='Done.', tool_calls=[])]
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch.object(self.a, 'run_tool', return_value='{}'):
                    self.a.ask(chat, 'Please play Sunny Day.')
                self.assertEqual(output.getvalue().count('assistant>'), 1)
                self.assertEqual(self.spoken, [line] if voice else [])

    def test_model_delay_acknowledgment_prevents_extra_tool_wait_message(self):
        from concurrent.futures import TimeoutError
        reply = SimpleNamespace(text='', tool_calls=[call('search_youtube')])
        future = Mock(); future.result.side_effect = [TimeoutError(), reply]
        pool = Mock(); pool.submit.return_value = future
        chat = ScriptedChat('Done.')
        with patch('piano2midi.chat.ThreadPoolExecutor', return_value=pool), \
                patch.object(self.a, 'run_tool', return_value='{}'):
            self.a.ask(chat, 'Please play Sunny Day.')
        self.assertEqual(self.spoken, ['One moment, let me see.'])


if __name__ == '__main__':
    unittest.main()
