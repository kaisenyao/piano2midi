"""The one conversation behind Mac Voice and iPhone Voice; no paid APIs or audio devices."""
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

import numpy as np

from piano2midi import voice


class Out:
    """Records what she does, like the phone; a reply ends the turn when the test says so."""

    def __init__(self):
        self.events, self.spoken = [], []

    def status(self, state):
        self.events.append(('state', state))

    def speak(self, pieces):
        self.spoken.append([np.asarray(p) for p in pieces])
        self.events.append(('audio',))

    def turn_end(self, conversation, sleep):
        self.events.append(('turn_end', sleep))


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.out = Out()
        self.parts = SimpleNamespace(stt=Mock(return_value='play moonlight'), wake=Mock(), speech=Mock(return_value=False),
                                     speaker=Mock(), greeter=Mock())
        self.parts.wake.heard.return_value = None
        self.parts.speaker.stream.side_effect = lambda text: iter([np.zeros(240, dtype=np.int16)])
        self.parts.greeter.pick.return_value = ('诶，我在呢！', np.zeros(2400, dtype=np.int16))
        self.assistant = Mock(player=None, touched_playback=False)
        self.chat = Mock()
        self.c = voice.Conversation(self.assistant, self.chat, Mock(), self.parts, self.out, voice.PauseControl())
        self.pcm = np.full(voice.FRAME, 1200, dtype='<i2')
        quiet = patch('builtins.print')
        quiet.start()
        self.addCleanup(quiet.stop)

    def frames(self, n, speaking=None, name=None):
        if speaking is not None:
            self.parts.speech.return_value = speaking
        self.parts.wake.heard.return_value = name
        for _ in range(n):
            self.c.frame(self.pcm)

    def test_waiting_never_sends_ambient_audio_to_the_cloud(self):
        self.frames(30, speaking=True)
        self.parts.stt.assert_not_called()
        self.assertEqual(self.c.state, 'waiting')

    def test_name_and_request_in_one_breath_are_sent_together(self):
        self.frames(1)
        self.frames(1, speaking=True, name='小茗同学')
        self.assertEqual(self.c.state, 'listening')
        self.assertEqual(len(self.c.frames), 2, 'The pre-roll keeps the name')
        self.frames(voice.GO_ON_FRAMES, speaking=True)
        with patch.object(self.c, 'respond') as respond:
            self.frames(15, speaking=False)
            respond.assert_called()
        self.parts.greeter.pick.assert_not_called()

    def test_name_then_pause_gets_a_ready_made_greeting_without_recognition(self):
        self.frames(1, speaking=True, name='小茗同学')  # the name, still sounding
        self.frames(1, speaking=False)  # then a pause
        self.parts.greeter.pick.assert_called_once_with('小茗同学')
        self.assertEqual(len(self.out.spoken), 1)
        self.assertEqual(self.out.events[-1], ('turn_end', False))
        self.parts.stt.assert_not_called()
        self.c.resume()  # the greeting has been played: listen for the request without the name
        self.assertEqual(self.c.state, 'conversation')
        self.assertAlmostEqual(self.c.dialogue_until - self.c.control.now(), voice.START_TIMEOUT_S, delta=.1)

    def test_greeting_without_elevenlabs_is_said_by_the_mac_voice(self):
        self.parts.greeter.pick.return_value = None
        self.frames(1, name='Hey Jasmine')
        self.parts.speaker.stream.assert_called_once_with("Hi! I'm here.")
        self.assertEqual(self.out.events[-1], ('turn_end', False))

    def test_a_request_goes_to_the_shared_assistant_and_her_answer_is_spoken(self):
        self.c.frames = [np.ones(voice.FRAME, dtype=np.int16)]
        self.assistant.ask.return_value = 'Hello'
        self.c.respond()
        self.assistant.ask.assert_called_once_with(self.chat, 'play moonlight')
        self.parts.speaker.stream.assert_called_once_with('Hello')
        self.assertEqual(self.out.events[-1], ('turn_end', False))
        self.c.frame(self.pcm)
        self.assertEqual(self.c.state, 'speaking', 'Nothing is heard until her answer has been played')
        self.c.resume()
        self.frames(1, speaking=True)
        self.assertEqual(self.c.state, 'listening', 'After a reply, no name is needed for a while')

    def test_nothing_to_answer_stays_silent_and_waits_for_her_name(self):
        for heard in ('', '小明同学', '嗯。'):
            self.out.events.clear()
            self.c.frames = [np.zeros(voice.FRAME, dtype=np.int16)]
            self.parts.stt.return_value = heard
            self.c.respond()
            self.assistant.ask.assert_not_called()
            self.assertEqual(self.c.state, 'waiting')
            self.assertFalse(any(e[0] in {'audio', 'turn_end'} for e in self.out.events))

    def test_while_the_piano_plays_only_her_name_is_listened_for(self):
        self.c.frames = [np.ones(voice.FRAME, dtype=np.int16)]
        self.assistant.ask.return_value = '好啦，这就给你弹《传奇》。'
        self.assistant.player = Mock(state='playing', completed=None)
        self.c.respond()
        self.assertEqual(self.out.events[-1], ('turn_end', True), 'No name-free follow-up')
        self.c.sleep()
        self.frames(30, speaking=True)  # the piano, taken for speech
        self.assertEqual(self.c.state, 'waiting')
        self.assertEqual(self.parts.stt.call_count, 1)

    def test_followup_ends_after_its_window(self):
        self.c.resume()
        self.c.dialogue_until = self.c.control.now() - 1
        self.frames(1, speaking=True)
        self.assertEqual(self.c.state, 'waiting')
        self.parts.stt.assert_not_called()

    def test_goodbye_waits_for_her_name(self):
        self.c.frames = [np.ones(voice.FRAME, dtype=np.int16)]
        self.parts.stt.return_value = '再见'
        self.c.respond()
        self.parts.speaker.stream.assert_called_once_with('再见，需要我时再叫我。')
        self.assertEqual(self.out.events[-1], ('turn_end', True))
        self.assistant.ask.assert_not_called()

    def test_a_piece_that_ends_on_its_own_gets_one_closing_remark(self):
        self.assistant.player = Mock(state='stopped', completed=('传奇', time.perf_counter() - 2))
        self.frames(1)
        self.assertEqual(len(self.out.spoken), 1)
        self.assertIn(self.c.last_remark, voice.FINISH_LINES['zh'])
        self.assertEqual(self.out.events[-1], ('turn_end', False), 'She may be answered without her name')
        self.c.sleep()
        self.frames(5)
        self.assertEqual(len(self.out.spoken), 1, 'Once')

    def test_bad_pcm_is_rejected(self):
        with self.assertRaises(ValueError):
            self.c.frame(np.zeros(10, dtype=np.int16))

    def test_only_her_name_in_one_breath_gets_a_greeting(self):
        self.parts.stt.return_value = '小明同学'
        self.frames(1, speaking=True, name='小明同学')
        self.frames(voice.GO_ON_FRAMES, speaking=True)
        self.frames(10, speaking=False)
        self.parts.greeter.pick.assert_called_once_with('小明同学')
        self.assertEqual(self.out.events[-1], ('turn_end', False), 'Then she listens for the request')

    def test_a_recognition_failure_loses_the_request_not_the_conversation(self):
        self.c.frames = [np.zeros(voice.FRAME, dtype=np.int16)]
        self.parts.stt.side_effect = RuntimeError('offline')
        self.c.respond()
        self.chat.reset.assert_not_called()
        self.assertEqual(self.c.state, 'waiting')

    def test_a_model_failure_starts_the_chat_afresh_and_the_piano_plays_on(self):
        player = self.assistant.player = Mock(state='playing', completed=None, current=('p', '传奇'), position=3.0)
        self.c.begin()
        player.state = 'paused'
        self.c.frames = [np.zeros(voice.FRAME, dtype=np.int16)]
        self.assistant.ask.side_effect = RuntimeError('model down')
        self.c.respond()
        self.chat.reset.assert_called_once()
        player.start.assert_called_once()  # at once, not after the follow-up window

    def test_a_paused_piece_stays_paused_through_a_greeting(self):
        self.assistant.player = Mock(state='playing', completed=None)
        self.c.begin()
        self.assistant.player.pause.assert_called_once()
        self.assistant.player.state = 'paused'
        self.c.resume()
        self.c.begin()
        self.assertTrue(self.c.was_playing)

    def test_mac_speakers_drop_her_own_voice_then_listen_on(self):
        mic = Mock()
        speakers = voice.Speakers(mic)
        conversation = Mock()
        speakers.turn_end(conversation, False)
        mic.drain.assert_called_once()
        conversation.resume.assert_called_once()
        speakers.turn_end(conversation, True)
        conversation.sleep.assert_called_once()


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        self.speaker = voice.Speaker()
        self.speaker.cloud = True
        quiet = patch('builtins.print')
        quiet.start()
        self.addCleanup(quiet.stop)

    def respond(self, *chunks, error=None):
        resp = Mock()
        resp.__enter__ = Mock(return_value=resp)
        resp.__exit__ = Mock(return_value=False)
        reads = list(chunks) + ([error] if error else []) + [b'']
        def read(size):
            item = reads.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        resp.read.side_effect = read
        return resp

    def test_her_voice_comes_in_pieces_as_it_is_made(self):
        with patch('urllib.request.urlopen', return_value=self.respond(b'\x01\x00\x02', b'\x00\x03\x00')):
            pieces = [p.tolist() for p in self.speaker.stream('hello')]
        self.assertEqual(pieces, [[1], [2, 3]], 'An odd byte waits for its other half')

    def test_a_failure_before_any_audio_uses_the_mac_voice(self):
        with patch('urllib.request.urlopen', side_effect=OSError('offline')), \
                patch.object(voice, 'mac_voice', return_value=np.array([7], dtype=np.int16)) as mac:
            self.assertEqual([p.tolist() for p in self.speaker.stream('你好')], [[7]])
        mac.assert_called_once_with('你好')

    def test_a_refused_key_is_not_tried_again(self):
        refused = urllib.error.HTTPError('url', 401, 'no', {}, None)
        refused.read = lambda: b'invalid key'
        with patch('urllib.request.urlopen', side_effect=refused), \
                patch.object(voice, 'mac_voice', return_value=np.array([7], dtype=np.int16)):
            list(self.speaker.stream('hello'))
        self.assertFalse(self.speaker.cloud)

    def test_a_failure_halfway_does_not_start_the_sentence_again(self):
        with patch('urllib.request.urlopen', return_value=self.respond(b'\x01\x00', error=OSError('lost'))), \
                patch.object(voice, 'mac_voice') as mac:
            self.assertEqual([p.tolist() for p in self.speaker.stream('hello')], [[1]])
        mac.assert_not_called()

    def test_nothing_to_say_says_nothing(self):
        with patch('urllib.request.urlopen') as urlopen:
            self.assertEqual(list(self.speaker.stream('https://example.com')), [])
        urlopen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
