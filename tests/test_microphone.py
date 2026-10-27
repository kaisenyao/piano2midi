"""The desktop microphone recovers when macOS breaks its stream; no audio devices."""
import queue
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

from piano2midi import voice


def frame(value):
    return np.full((voice.FRAME, 1), value, dtype=np.int16)


class FakeStream:
    opened = []
    on_start = None  # a frame a newly opened stream delivers at once

    def __init__(self, callback, **settings):
        self.callback, self.closed = callback, False
        FakeStream.opened.append(self)

    def start(self):
        if FakeStream.on_start is not None:
            self.callback(FakeStream.on_start, voice.FRAME, None, None)

    def close(self):
        self.closed = True


class MicrophoneTests(unittest.TestCase):
    def setUp(self):
        FakeStream.opened, FakeStream.on_start = [], None
        devices = patch.dict(sys.modules, {'sounddevice': types.SimpleNamespace(InputStream=FakeStream)})
        devices.start()
        self.addCleanup(devices.stop)
        self.mic = voice.Microphone()
        self.mic.frames.get = lambda *_, **__: queue.Queue.get(self.mic.frames, block=False)  # no waiting
        quiet = patch('builtins.print')
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_frames_arrive_in_order_and_drain_drops_the_backlog(self):
        for value in (1, 2, 3):
            FakeStream.opened[-1].callback(frame(value), voice.FRAME, None, None)
        self.assertEqual(self.mic.read()[0], 1)
        self.mic.drain()
        FakeStream.opened[-1].callback(frame(4), voice.FRAME, None, None)
        self.assertEqual(self.mic.read()[0], 4)

    def test_a_stream_that_stops_is_opened_again(self):
        FakeStream.on_start = frame(7)
        self.assertEqual(self.mic.read()[0], 7)
        self.assertEqual(len(FakeStream.opened), 2)
        self.assertTrue(FakeStream.opened[0].closed)

    def test_a_microphone_that_stays_silent_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'no audio'):
            self.mic.read()


if __name__ == '__main__':
    unittest.main()
