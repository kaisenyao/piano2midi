"""A library keeps its MIDI files and playlist next to its index, and writes the playlist once per batch."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mido

from piano2midi.chat import index_midi_folder
from piano2midi.library import Library


def make_midi(path: Path) -> Path:
    midi = mido.MidiFile()
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.Message('note_on', note=60, velocity=64, time=0))
    track.append(mido.Message('note_off', note=60, time=480))
    path.parent.mkdir(parents=True, exist_ok=True)
    midi.save(path)
    return path


class LibraryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.lib = Library(self.root / 'library.db')
        self.addCleanup(self.lib.db.close)
        self.playlist = self.root / 'MIDI Files' / 'playlist.m3u'

    def test_import_copies_next_to_the_index_and_writes_the_playlist_once(self):
        for name in ('Nocturne', 'Waltz', 'Etude'):
            make_midi(self.root / 'elsewhere' / f'{name}.mid')
        with patch.object(self.lib, 'write_playlist', wraps=self.lib.write_playlist) as write:
            added = self.lib.import_folder(self.root / 'elsewhere')
        self.assertEqual(len(added), 3)
        write.assert_called_once()
        self.assertTrue((self.root / 'MIDI Files' / 'Waltz.mid').is_file())
        self.assertEqual(self.playlist.read_text().count('#EXTINF'), 3)

    def test_startup_picks_up_files_put_in_by_hand_and_drops_deleted_ones(self):
        kept = make_midi(self.root / 'MIDI Files' / 'Kept.mid')
        gone = make_midi(self.root / 'MIDI Files' / 'Gone.mid')
        with patch.object(self.lib, 'write_playlist', wraps=self.lib.write_playlist) as write:
            self.assertEqual(index_midi_folder(self.lib), 2)
        write.assert_called_once()
        gone.unlink()
        self.assertEqual(index_midi_folder(self.lib), 0)
        self.assertIn(kept.name, self.playlist.read_text())
        self.assertNotIn(gone.name, self.playlist.read_text())


if __name__ == '__main__':
    unittest.main()
