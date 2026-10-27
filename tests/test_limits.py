"""Where the library lives, and the 60-minute limit for recordings; no network."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

from piano2midi import audio, download, library, youtube


class LimitTests(unittest.TestCase):
    def test_the_library_is_in_the_project_wherever_a_command_runs(self):
        self.assertEqual(library.OUTPUT_DIR, Path(__file__).resolve().parents[1] / 'output')

    def test_decoding_refuses_a_recording_over_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'three seconds.wav'
            with wave.open(str(path), 'wb') as clip:
                clip.setnchannels(1)
                clip.setsampwidth(2)
                clip.setframerate(8000)
                clip.writeframes(b'\x10\x00' * 8000 * 3)
            with patch.object(audio, 'MAX_AUDIO_S', 2), self.assertRaisesRegex(RuntimeError, 'longer than'):
                audio.load_audio(path, 8000)
            with patch.object(audio, 'MAX_AUDIO_S', 5):
                self.assertEqual(audio.load_audio(path, 8000).shape, (24000, 2))

    def test_a_long_video_is_refused_before_downloading(self):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = {'_type': 'video', 'duration': audio.MAX_AUDIO_S + 1}
        with tempfile.TemporaryDirectory() as tmp, patch('yt_dlp.YoutubeDL', return_value=ydl):
            with self.assertRaisesRegex(ValueError, 'longer than 60 minutes'):
                download.download_audio('https://www.youtube.com/watch?v=abcdefghijk', Path(tmp))
        ydl.process_ie_result.assert_not_called()

    def test_search_never_offers_a_video_over_the_limit(self):
        pages = [{'items': [{'id': {'videoId': 'short'}}, {'id': {'videoId': 'long'}}]},
                 {'items': [{'id': 'short', 'snippet': {'title': 'Nocturne'}, 'contentDetails': {'duration': 'PT5M'}},
                            {'id': 'long', 'snippet': {'title': 'All Nocturnes'},
                             'contentDetails': {'duration': 'PT2H'}}]}]
        with patch.object(youtube, '_get', side_effect=pages):
            self.assertEqual([r['title'] for r in youtube.search('nocturne', key='test')], ['Nocturne'])


if __name__ == '__main__':
    unittest.main()
