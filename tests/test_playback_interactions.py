"""Playback state and tool-boundary tests; no real API calls, devices, or keys."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import mido
from piano2midi import youtube
from piano2midi.chat import MAX_TOOL_ROUNDS, NEW_CONVERSATION_AFTER_S, Assistant, TOOLS
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
        self.resets = self.discards = 0

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

    def discard_turn(self):
        self.discards += 1


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.root_context = tempfile.TemporaryDirectory()
        self.addCleanup(self.root_context.cleanup)
        self.root = Path(self.root_context.name)
        self.lib = Library(self.root/'library.db')
        self.addCleanup(self.lib.db.close)
        self.records = []
        for title in ('贝加尔湖畔', '月光'):
            path = self.root/(title+'.mid')
            midi = mido.MidiFile(); track = mido.MidiTrack(); midi.tracks.append(track)
            track.append(mido.Message('note_on', note=60, velocity=64, time=0))
            track.append(mido.Message('note_off', note=60, time=480*240))
            midi.save(path)
            record = self.lib.add(path, title)
            metadata = ({'composer':'李健', 'aliases':'Li Jian 贝加尔湖畔', 'tags':'piano'} if title == '贝加尔湖畔'
                        else {'composer':'Claude Debussy 德彪西', 'aliases':'Clair de Lune 月光', 'tags':'piano'})
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
        self.a.request = '播放贝加尔湖畔'
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
        self.assertEqual(self.player.current[1], '贝加尔湖畔')

    def test_fixture_has_composer_and_aliases_for_realistic_new_song_search(self):
        results = self.lib.search('德彪西 月光 Clair de Lune Debussy piano')
        moonlight = next(r for r in results if r['title'] == '月光')
        self.assertIn('Debussy', moonlight['composer'])
        self.assertIn('Clair de Lune', moonlight['aliases'])

    def test_waiting_preserves_pending_request_and_position(self):
        self.prime(); self.connected = False
        self.a.control_playback('resume')
        pending = self.a.pending
        chat = ScriptedChat([call('control_playback', action='pause')], '好，准备好再叫我。')
        self.a.ask(chat, '先别继续，我还没连好')
        self.assertEqual(self.player.state, 'paused')
        self.assertEqual(self.player.position, 22)
        self.assertEqual(self.a.pending, pending)

    def test_connection_failure_forgets_only_that_turn(self):
        self.connected = False
        chat = ScriptedChat([call('search_library', query='月光')],
                            [call('play_recording', id=self.records[1]['id'])])
        self.a.ask(chat, '我想听月光')
        self.assertEqual(chat.discards, 1)
        self.assertEqual(chat.resets, 0, 'Earlier turns stay')
        self.assertEqual(self.a.pending.title, '月光')

    def test_endless_tool_calls_are_given_up_cleanly(self):
        chat = ScriptedChat(*[[call('search_library', query='月光')]] * (MAX_TOOL_ROUNDS + 1))
        self.a.ask(chat, '我想听月光')
        self.assertEqual(chat.discards, 1, 'No unanswered tool call is left for the next request')

    def test_a_long_quiet_spell_starts_a_new_conversation(self):
        now = [1000.0]
        self.a.clock = lambda: now[0]
        chat = ScriptedChat('好呀。', '在呢。', '欢迎回来。')
        self.a.ask(chat, '你好')
        now[0] += 60
        self.a.ask(chat, '还在吗')
        self.assertEqual(chat.resets, 0)
        now[0] += NEW_CONVERSATION_AFTER_S + 1
        self.a.ask(chat, '我回来了')
        self.assertEqual(chat.resets, 1)

    def test_tool_failures_are_shown_in_the_terminal(self):
        with patch('piano2midi.youtube.search', side_effect=youtube.YouTubeError('YouTube API error 403: quota')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = json.loads(self.a.run_tool('search_youtube', {'query': '月光'}))
        self.assertIn('quota', result['error'])
        self.assertIn('search_youtube failed: YouTube API error 403: quota', out.getvalue())

    def test_selected_new_song_requires_actual_search_result(self):
        chat = ScriptedChat([call('search_library', query='月光')],
                            [call('play_recording', id=self.records[1]['id'])])
        self.a.ask(chat, '我想听月光')
        self.assertEqual(self.player.current[1], '月光')
        self.assertIsNone(self.a.pending)

    def test_selection_permission_expires_after_request(self):
        self.a.run_tool('search_library', {'query': '月光'})
        chat = ScriptedChat([call('play_recording', id=self.records[1]['id'])])
        self.a.ask(chat, '聊聊音乐')
        self.assertEqual(chat.results[0]['error'], 'selection_required')
        self.assertIsNone(self.player.current)

    def test_transcription_can_offer_a_new_recording(self):
        with patch.object(self.a, 'transcribe_local', return_value=self.records[1]):
            self.a.run_tool('transcribe_local', {'path':'input.wav', 'title':'月光'})
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

    def test_model_can_correct_wrong_selection_tool_to_control(self):
        self.prime()
        chat = ScriptedChat([call('play_recording', id=999)], [call('control_playback', action='resume')])
        answer = self.a.ask(chat, '好像断开了，你可以看看能帮我继续播放吗？')
        self.player._thread.join()
        self.assertEqual(chat.results[0]['error'], 'selection_required')
        self.assertEqual(self.player._from, 22)
        self.assertEqual(answer, '')
        self.assertFalse(any('找不到' in line or '选中' in line for line in self.spoken))

    def test_model_can_correct_control_parameters(self):
        self.prime()
        chat = ScriptedChat([call('control_playback', action='resume', id=999)],
                            [call('control_playback', action='resume')])
        self.a.ask(chat, '你能帮我接着弹吗')
        self.assertEqual(chat.results[0]['error'], 'invalid_playback_arguments')
        self.assertEqual(self.player._from, 22)

    def test_long_request_model_path_matrix(self):
        cases = [
            ('呃，好像断开了，你可以再帮我重新播放吗？', 'restart', 0),
            ('小明同学好像又断开了，你可以看看能帮我继续播放吗？', 'resume', 22),
            ('我想继续听这首歌', 'resume', 22),
            ('我已经连好了，你帮我再检查再试一次好吗', 'retry', 22),
            ('刚才那首从开头再来一遍吧', 'restart', 0),
            ('Could you carry on with the piece we were hearing?', 'resume', 22),
        ]
        for text, action, offset in cases:
            with self.subTest(text=text):
                self.prime()
                chat = ScriptedChat([call('control_playback', action=action)])
                self.a.ask(chat, text)
                self.player._thread.join()
                self.assertEqual(self.player._from, offset)
                self.assertEqual(self.player.current[1], '贝加尔湖畔')

    def test_midpiece_disconnect_then_repeated_retry_preserves_position(self):
        self.prime(); self.connected = False
        for text in ('好像停了，能再帮我试一次吗', '我已经连接好了'):
            chat = ScriptedChat([call('control_playback', action='retry')])
            result = self.a.ask(chat, text)
            self.assertIn('设备和钢琴', result)
            self.assertEqual(self.a.pending.from_s, 22)
        self.connected = True
        self.a.ask(ScriptedChat([call('control_playback', action='retry')]), '现在好了，帮我继续')
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
        self.a.run_tool('search_library', {'query':'月光'})
        self.a.play_recording(self.records[1]['id'])
        self.assertEqual(self.player.current[1], '贝加尔湖畔')
        self.assertEqual(self.a.pending.title, '月光')
        self.connected = True; self.a.control_playback('retry')
        self.assertEqual(self.player.current[1], '月光')

    def test_cancel_clears_pending_and_stops(self):
        self.prime(); self.connected = False; self.a.control_playback('resume')
        self.a.control_playback('cancel')
        self.assertIsNone(self.a.pending)
        self.assertEqual(self.player.state, 'stopped')
        self.connected = True; self.a.control_playback('restart')
        self.assertEqual(self.player._from, 0)

    def test_no_current_control_requests_selection_without_claiming_missing_file(self):
        result = self.a.ask(ScriptedChat([call('control_playback', action='resume')]), '能继续吗')
        self.assertIn('曲名', result)
        self.assertNotIn('文件', result)

    def test_only_actual_file_loss_reports_unavailable(self):
        self.prime(); self.player.current[0].unlink()
        result = self.a.control_playback('resume')
        self.assertEqual(result['error'], 'recording_unavailable')

    def test_current_state_is_supplied_to_model(self):
        self.prime(); chat = ScriptedChat('这首很动听。')
        self.a.ask(chat, '这首叫什么')
        self.assertIn('贝加尔湖畔', chat.messages[0])
        self.assertIn('22', chat.messages[0])
        self.assertIn('paused', chat.messages[0])

    def test_negations_and_new_song_titles_remain_model_requests(self):
        for text in ('不要继续播放', '我想继续听月光', '你能看看能帮我继续吗'):
            self.assertIsNone(match_command(text))
        self.prime()
        self.a.ask(ScriptedChat('好，先不继续。'), '先别继续，听我说')
        self.assertEqual(self.player.state, 'paused')

    def test_quick_controls_share_the_same_dispatch(self):
        self.prime()
        self.a.ask(ScriptedChat(), '继续播放')
        self.player._thread.join()
        self.player._sent_to = 30
        self.a.ask(ScriptedChat(), '停止播放')
        self.assertEqual(self.player.position, 30)
        self.a.ask(ScriptedChat(), '从头弹')
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
        self.a.request = '现在好像断开了，你帮我看一下'
        speaker = Mock()
        finish_interaction(self.a, speaker, '停在22秒，等连接好了继续。', True)
        speaker.say.assert_called_once()
        self.assertIn('设备和钢琴', speaker.say.call_args.args[0])
        self.assertEqual(self.player.state, 'paused')
        self.assertEqual(self.player.position, 22)

    def test_auto_resume_checks_then_speaks_then_plays(self):
        self.prime(); self.a.touched_playback = False
        events = []
        speaker = Mock(); speaker.say.side_effect = lambda text: events.append(('reply', text))
        with patch.object(self.player, 'check_connection', side_effect=lambda: events.append('check')),              patch.object(self.player, 'play', side_effect=lambda *args: events.append('play')):
            finish_interaction(self.a, speaker, '是贝加尔湖畔。', True)
            self.player._thread.join()
        self.assertEqual(events, ['check', ('reply','是贝加尔湖畔。'), 'play'])
        self.assertEqual(self.player._from, 22)

    def test_explicit_pause_does_not_auto_resume(self):
        self.prime(); self.a.touched_playback = True
        speaker = Mock()
        with patch.object(self.player, 'start') as start:
            finish_interaction(self.a, speaker, '好，先暂停。', True)
            start.assert_not_called()
        speaker.say.assert_called_once_with('好，先暂停。')

    def test_short_pause_has_one_spoken_confirmation_and_keeps_position(self):
        self.prime()
        answer = self.a.ask(ScriptedChat(), "停止播放")
        self.assertTrue(answer)
        self.assertNotIn("播放", answer)
        speaker = Mock()
        finish_interaction(self.a, speaker, answer, True)
        speaker.say.assert_called_once_with(answer)
        self.assertEqual(self.player.state, 'paused')
        self.assertEqual(self.player.position, 22)

    def test_resume_and_restart_introductions_are_spoken_once(self):
        self.prime()
        self.a.ask(ScriptedChat(), "继续播放")
        self.player._thread.join()
        self.assertEqual(len(self.spoken), 1)
        self.assertIn("弹", self.spoken[0])
        self.assertEqual(self.player._from, 22)
        self.a.pause(); self.spoken.clear()
        answer = self.a.ask(ScriptedChat(), "从头弹")
        self.player._thread.join()
        self.assertEqual(answer, '')
        self.assertEqual(len(self.spoken), 1)
        self.assertTrue("从头" in self.spoken[0] or "开始" in self.spoken[0])
        self.assertEqual(self.player._from, 0)

    def test_model_pause_uses_one_fallback_only_if_final_reply_is_empty(self):
        for final_reply in ('', "好呀，我先停一下。"):
            with self.subTest(final_reply=final_reply):
                self.prime()
                chat = ScriptedChat([call('control_playback', action='pause')], final_reply)
                answer = self.a.ask(chat, "能先暂停一下这首吗")
                self.assertTrue(answer)
                if final_reply: self.assertEqual(answer, final_reply)
                self.assertEqual(self.spoken, [])
                speaker = Mock()
                finish_interaction(self.a, speaker, answer, True)
                speaker.say.assert_called_once()

    def test_failed_restart_does_not_speak_a_success_confirmation(self):
        self.prime(); self.connected = False
        answer = self.a.ask(ScriptedChat(), "从头弹")
        self.assertIn("设备和钢琴", answer)
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.a.pending.from_s, 0)
        self.assertEqual(self.player.state, 'paused')

    def test_name_only_request_is_local_and_preserves_playback(self):
        self.prime()
        for text in ("小茗同学", "小明同学", "Hey Jasmine"):
            with self.subTest(text=text):
                chat = Mock()
                answer = self.a.ask(chat, text)
                self.assertTrue(answer)
                self.assertNotIn("看看", answer)
                chat.send_user.assert_not_called()
                self.assertEqual(self.player.state, 'paused')
                self.assertEqual(self.player.position, 22)

    def test_mac_voice_runs_the_shared_conversation_on_its_own_microphone(self):
        import numpy as np
        import piano2midi.voice as voice
        mic = Mock(); mic.read.side_effect = [np.zeros(voice.FRAME, dtype=np.int16), KeyboardInterrupt()]
        parts = Mock(); parts.wake.heard.return_value = None; parts.wake.words = ['小茗同学']
        parts.speech.return_value = False
        assistant = Mock(player=None)
        with patch('piano2midi.library.Library'), patch('piano2midi.chat.index_midi_folder'), \
                patch('piano2midi.chat.Assistant', return_value=assistant), \
                patch.object(voice, 'voice_chat', return_value=(Mock(), 'test')), \
                patch.multiple(voice, VoiceParts=Mock(return_value=parts), Microphone=Mock(return_value=mic)):
            self.assertEqual(voice.main([]), 0)
        parts.speech.assert_called_once()  # the frame reached the conversation
        assistant.stop.assert_called_once()
        assistant.close.assert_called_once()

    def test_wait_message_only_once_during_model_delay(self):
        from concurrent.futures import TimeoutError
        reply = SimpleNamespace(text='想听哪首？', tool_calls=[])
        future = Mock(); future.result.side_effect=[TimeoutError(), reply]
        pool = Mock(); pool.submit.return_value=future
        with patch('piano2midi.chat.Worker', return_value=pool):
            self.a.ask(Mock(), '想听歌')
        self.assertEqual(self.spoken, ['好呀，我看看。'])

    def test_wait_message_counts_all_model_steps_together(self):
        from concurrent.futures import TimeoutError
        now, timeouts = [100.0], []
        steps = iter([SimpleNamespace(text='', tool_calls=[call('search_library', query='月光')]),
                      TimeoutError(), SimpleNamespace(text='找到啦。', tool_calls=[])])
        def result(timeout=None):
            timeouts.append(timeout)
            now[0] += 2  # each step takes two seconds: neither alone reaches three
            step = next(steps)
            if isinstance(step, Exception):
                raise step
            return step
        future = Mock(); future.result.side_effect = result
        pool = Mock(); pool.submit.return_value = future
        with patch('piano2midi.chat.Worker', return_value=pool), \
                patch('piano2midi.chat.time.monotonic', side_effect=lambda: now[0]):
            self.a.ask(Mock(), '想听月光')
        self.assertEqual(timeouts[:2], [3.0, 1.0])
        self.assertEqual(self.spoken, ['好呀，我看看。'])

    def test_silent_model_announces_before_long_tools_only_once(self):
        for first_tool in ('search_youtube', 'transcribe_url', 'transcribe_local'):
            with self.subTest(first_tool=first_tool):
                self.spoken.clear()
                chat = ScriptedChat([call(first_tool)], [call('transcribe_url')], 'Done.')
                def run_tool(name, arguments):
                    self.assertEqual(len(self.spoken), 1)  # feedback precedes the blocking work
                    return '{}'
                with patch.object(self.a, 'run_tool', side_effect=run_tool):
                    self.a.ask(chat, '我想听红豆')
                self.assertIn('稍等', self.spoken[0])
                self.assertNotIn('分钟', self.spoken[0])

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
        with patch('piano2midi.chat.Worker', return_value=pool), \
                patch.object(self.a, 'run_tool', return_value='{}'):
            self.a.ask(chat, 'Please play Sunny Day.')
        self.assertEqual(self.spoken, ['One moment, let me see.'])



class ReconnectTests(unittest.TestCase):
    """The piano disconnects and comes back while chat, voice or iPhone Voice runs."""

    def setUp(self):
        self.outputs, self.opened = ['Network piano2midi'], []
        def open_output(name):
            self.opened.append(Mock())
            return self.opened[-1]
        for patcher in (patch('piano2midi.play.output_names', side_effect=lambda: list(self.outputs)),
                        patch('piano2midi.play.default_lead', return_value=0.0),
                        patch('mido.open_output', side_effect=open_output),
                        patch.object(Player, 'play')):  # nothing is sent
            patcher.start()
            self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)/'月光.mid'
        self.path.touch()

    def test_continue_says_the_piano_is_gone_then_plays_on_once_it_is_back(self):
        a = Assistant(Mock())
        a.request, a.player = '继续', Player()
        self.addCleanup(a.player.close)
        with contextlib.redirect_stdout(io.StringIO()):
            a.player.start([(self.path, '月光')], 22)
            a.pause()
            self.outputs = []
            result = a.play()
            self.assertIn('还没连上琴', a.playback_reply(result))
            self.assertEqual(a.pending.from_s, 22)
            self.outputs = ['Network piano2midi']
            result = a.play()
        self.assertEqual((result['status'], result['from']), ('playing', '0:22'))
        self.assertEqual(len(self.opened), 2)  # opened afresh: the piano that came back is a new output
        self.opened[0].close.assert_called_once()
        self.assertIs(a.player.port, self.opened[1])

    def test_a_missing_piano_is_explained_in_one_line(self):
        a = Assistant(Mock())
        a.request, a.player = '继续', Player()
        self.addCleanup(a.player.close)
        a.player.current, a.player.state, a.player._offset = (self.path, '月光'), 'paused', 22
        self.outputs = []
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            result = json.loads(a.run_tool('control_playback', {'action': 'resume'}))
        self.assertEqual(result['error'], 'playback_connection_unavailable')
        self.assertEqual(shown.getvalue().splitlines(), [
            '  (cannot play: no MIDI output matching "piano2midi" (available: none). '
            'Check that the network MIDI session is on and the iPad is connected.)'])

    def test_the_piano_going_away_mid_piece_pauses_where_the_sound_stopped(self):
        midi = mido.MidiFile(); track = mido.MidiTrack(); midi.tracks.append(track)
        step = round(mido.second2tick(0.02, midi.ticks_per_beat, 500000))
        for _ in range(100):  # a short note every 40 ms, four seconds in all
            track.append(mido.Message('note_on', note=60, velocity=64, time=step))
            track.append(mido.Message('note_off', note=60, time=step))
        midi.save(self.path)
        player = Player()
        self.addCleanup(player.close)
        player.check_connection()
        threading.Timer(0.3, self.outputs.clear).start()
        with patch('piano2midi.play.LOOK_EVERY_S', 0.1):
            started = time.perf_counter()
            self.assertFalse(player._play_file(self.path))
        self.assertLess(time.perf_counter() - started, 1.0, 'It stops soon after')
        self.assertEqual(player.state, 'paused')
        self.assertIn('no MIDI output', player.lost)
        self.assertGreater(player.position, 0.1)
        self.assertLess(player.position, 0.45)
        self.outputs.append('Network piano2midi')
        player.check_connection()  # the next try starts afresh
        self.assertIsNone(player.lost)

    def test_the_ipad_leaving_the_session_is_the_piano_going_away(self):
        # macOS keeps the session's output while the iPad is away; only its Participants show it has gone.
        joined = [1]
        midi = mido.MidiFile(); track = mido.MidiTrack(); midi.tracks.append(track)
        step = round(mido.second2tick(0.02, midi.ticks_per_beat, 500000))
        for _ in range(100):
            track.append(mido.Message('note_on', note=60, velocity=64, time=step))
            track.append(mido.Message('note_off', note=60, time=step))
        midi.save(self.path)
        with patch('piano2midi.play.session_peers', side_effect=lambda name: joined[0]), \
             patch('piano2midi.play.LOOK_EVERY_S', 0.1):
            player = Player()
            self.addCleanup(player.close)
            player.check_connection()
            threading.Timer(0.3, joined.__setitem__, (0, 0)).start()
            self.assertFalse(player._play_file(self.path))
            self.assertEqual(player.state, 'paused')
            self.assertIn('the network MIDI session "Network piano2midi" has no participants', player.lost)
            with self.assertRaises(LookupError):
                player.check_connection()
            joined[0] = 1
            player.check_connection()
        self.assertEqual(self.outputs, ['Network piano2midi'], 'The output itself never went away')

    def test_ports_say_who_has_joined_a_network_session(self):
        from piano2midi.play import main
        self.outputs[:] = ['Network piano2midi', 'IAC Driver Bus 1']
        shown = io.StringIO()
        with patch('piano2midi.play.session_peers', side_effect=lambda name: 1 if name.startswith('Network') else None), \
             contextlib.redirect_stdout(shown):
            self.assertEqual(main(['--ports']), 0)
        self.assertEqual(shown.getvalue().splitlines(),
                         ['Network piano2midi  (network session, participants: 1)', 'IAC Driver Bus 1'])

    def test_she_says_the_piece_not_the_file_name(self):
        from piano2midi.chat import spoken_title
        for title, said in [('周杰伦 - 搁浅 (MappleZS)', '搁浅'), ('Chopin - Nocturne Op. 9 No. 2 (Lang Lang)', 'Nocturne Op. 9 No. 2'),
                            ('贝加尔湖畔（赵海洋）', '贝加尔湖畔'), ('Handel-Halvorsen - Passacaglia (Kassia)', 'Passacaglia'),
                            ('Greensleeves', 'Greensleeves'), ('(MappleZS)', '(MappleZS)')]:
            self.assertEqual(spoken_title(title), said)

    def test_notes_on_their_way_keep_their_output(self):
        player = Player()
        self.addCleanup(player.close)
        player.start([(self.path, '月光')])
        player.pause()
        player._last_when = time.perf_counter() + 5  # the last notes sent have yet to sound
        player.start([(self.path, '月光')], 3)
        self.assertEqual(len(self.opened), 1)
        player.pause()
        self.outputs = []
        with self.assertRaises(LookupError):
            player.start([(self.path, '月光')], 3)


class WorkerTests(unittest.TestCase):
    def test_calls_run_in_order_in_one_thread_that_never_holds_up_quitting(self):
        from piano2midi.chat import Worker
        worker, seen = Worker(), []
        def job(n):
            seen.append((n, threading.current_thread()))
            if n == 2:
                raise ValueError('bad')
            return n
        futures = [worker.submit(job, n) for n in range(4)]
        self.assertEqual(futures[0].result(2), 0)
        with self.assertRaises(ValueError):
            futures[2].result(2)
        self.assertEqual(futures[3].result(2), 3)
        self.assertEqual([n for n, _ in seen], [0, 1, 2, 3])
        self.assertEqual(len({thread for _, thread in seen}), 1)
        self.assertTrue(seen[0][1].daemon)
        worker.shutdown()
        seen[0][1].join(2)
        self.assertFalse(seen[0][1].is_alive())

if __name__ == '__main__':
    unittest.main()
