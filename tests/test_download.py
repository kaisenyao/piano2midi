from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from piano2midi import download


@pytest.fixture
def fake_ydl(monkeypatch):
    state = {'type': 'video', 'calls': 0, 'downloads': 0}
    class YoutubeDL:
        def __init__(self, options):
            self.options = options
            state['options'] = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download=False):
            assert download is False
            state['calls'] += 1
            return {'_type': state['type'], 'title': '花之舞', 'id': 'abc', 'ext': 'webm'}
        def process_ie_result(self, info, download=True):
            state['downloads'] += 1
            Path(self.prepare_filename(info)).write_bytes(b'audio')
            return info
        def prepare_filename(self, info):
            return self.options['outtmpl'].replace('%(ext)s', info['ext'])
    monkeypatch.setitem(sys.modules, 'yt_dlp', SimpleNamespace(YoutubeDL=YoutubeDL))
    monkeypatch.setattr(download, 'ffmpeg_exe', lambda: '/bundled/ffmpeg')
    monkeypatch.setattr(download, 'valid_audio', lambda p: p.is_file() and p.read_bytes() == b'audio')
    return state


def test_original_format_cache_and_title(tmp_path, fake_ydl):
    url = 'https://example.com/video'
    result, title = download.download_audio(url, tmp_path)
    assert result.suffix == '.webm' and title == '花之舞'
    assert fake_ydl['options']['noplaylist'] is True
    assert fake_ydl['options']['fixup'] == 'never'
    assert not list(tmp_path.glob('.*-*'))
    assert download.download_audio(url, tmp_path) == (result, title)
    assert fake_ydl['calls'] == 1
    assert fake_ydl['downloads'] == 1


def test_bad_cache_and_part_file_not_reused(tmp_path, fake_ydl):
    url = 'https://example.com/video'
    key = download.task_id(url)
    (tmp_path / f'{key}.webm').write_bytes(b'broken')
    (tmp_path / f'{key}.part').write_bytes(b'audio')
    result, _ = download.download_audio(url, tmp_path)
    assert result.read_bytes() == b'audio'
    assert fake_ydl['downloads'] == 1


def test_playlist_rejected_before_download(tmp_path, fake_ydl):
    fake_ydl['type'] = 'playlist'
    with pytest.raises(ValueError, match='single video'):
        download.download_audio('https://example.com/playlist', tmp_path)
    assert fake_ydl['downloads'] == 0
    assert not list(tmp_path.glob('*.webm'))


@pytest.mark.parametrize('url', ['', 'file:///etc/passwd', 'javascript:alert(1)', 'https:///bad'])
def test_invalid_url(tmp_path, fake_ydl, url):
    with pytest.raises(ValueError, match='http'):
        download.download_audio(url, tmp_path)
    assert fake_ydl['calls'] == 0
