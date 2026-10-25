import csv
from pathlib import Path
from types import SimpleNamespace
import sys

import pretty_midi
import pytest

from piano2midi import batch
from piano2midi.cli import main
from piano2midi.pipeline import Options, convert_file


def midi(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    m = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(0)
    inst.notes.append(pretty_midi.Note(80, 60, 0, 1))
    m.instruments.append(inst)
    m.write(str(path))


def tasks(path, rows, fields=None):
    fields = fields or batch.COLUMNS
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def env(tmp_path, monkeypatch):
    calls = {'download': [], 'convert': [], 'engine': []}
    def download(url, cache):
        calls['download'].append(url)
        if url == 'bad':
            raise RuntimeError('test download failure')
        p = cache / f'{batch.task_id(url)}.webm'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'valid audio')
        return p, 'Same title'
    def convert(engine, audio, out, options, overwrite=False, output_name=None):
        calls['convert'].append((audio, output_name))
        midi(Path(out) / f'{output_name}.mid')
    def engine(**kwargs):
        calls['engine'].append(kwargs)
        return object()
    monkeypatch.setattr(batch, 'download_audio', download)
    monkeypatch.setattr(batch, 'valid_audio', lambda p: p.is_file() and p.read_bytes() == b'valid audio')
    monkeypatch.setattr(batch, 'convert_file', convert)
    monkeypatch.setitem(sys.modules, 'piano2midi.engine', SimpleNamespace(TranskunEngine=engine))
    path = tmp_path / 'lists' / 'tasks.csv'
    path.parent.mkdir()
    out = tmp_path / 'output'
    return path, out, calls


def run(env, **kwargs):
    path, out, _ = env
    return batch.run_csv(path, out, Options(), **kwargs)


def rows(env):
    return batch.read_tasks(env[0])[1]


def test_url_only_bom_completed_skipped_no_model(env):
    path, out, calls = env
    tasks(path, [{'url': 'https://example.com/a'}], ['url'])
    assert run(env) == 0
    assert rows(env)[0]['name'] == 'Same title'
    assert (out / 'Same title.mid').exists()
    assert len(calls['engine']) == 1
    for value in calls.values():
        value.clear()
    assert run(env) == 0
    assert not any(calls.values())


def test_names_duplicate_urls_and_collisions(env):
    tasks(env[0], [{'url': 'https://example.com/a', 'name': '花, "舞"'},
                   {'url': 'https://example.com/a'},
                   {'url': 'https://example.com/b', 'name': '花, "舞"'}])
    assert run(env) == 0
    result = rows(env)
    assert result[0]['midi_path'] == result[1]['midi_path']
    assert result[0]['midi_path'] != result[2]['midi_path']
    assert len(env[2]['download']) == len(env[2]['convert']) == 2
    assert len(env[2]['engine']) == 1
    assert result[0]['name'] == '花, "舞"'
    assert '..' in result[0]['midi_path']  # Relative to lists/tasks.csv, not cwd.


@pytest.mark.parametrize('status', ['failed', 'processing', 'done'])
def test_resume_missing_midi_reuses_audio(env, status):
    audio = env[1] / 'audio' / 'cache.opus'
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b'valid audio')
    tasks(env[0], [{'url': 'https://example.com/a', 'name': 'Song', 'status': status,
                    'audio_path': str(audio), 'midi_path': str(env[1] / 'Song.mid')}])
    assert run(env) == 0
    assert not env[2]['download']
    assert len(env[2]['convert']) == 1
    assert rows(env)[0]['status'] == 'done'


def test_corrupt_audio_redownload_and_corrupt_midi_rebuilt(env):
    audio = env[1] / 'bad.opus'
    audio.parent.mkdir()
    audio.write_bytes(b'corrupt')
    target = env[1] / 'Song.mid'
    target.write_bytes(b'broken midi')
    tasks(env[0], [{'url': 'https://example.com/a', 'audio_path': str(audio),
                    'midi_path': str(target), 'status': 'done'}])
    assert run(env) == 0
    assert len(env[2]['download']) == 1
    assert batch.valid_midi(target)
    assert not list(env[1].glob('*(2)*'))


def test_failure_continues_and_retry_uses_audio(env, monkeypatch):
    original = batch.convert_file
    def fail(*args, **kwargs):
        raise RuntimeError('transcription failed')
    tasks(env[0], [{'url': 'bad'}, {'url': 'https://example.com/a'}])
    monkeypatch.setattr(batch, 'convert_file', fail)
    assert run(env) == 1
    assert all(row['status'] == 'failed' for row in rows(env))
    assert rows(env)[1]['audio_path']
    monkeypatch.setattr(batch, 'convert_file', original)
    env[2]['download'].clear()
    assert run(env) == 1
    assert env[2]['download'] == ['bad']
    assert rows(env)[1]['status'] == 'done'


def test_overwrite_retranscribes_without_download(env):
    tasks(env[0], [{'url': 'https://example.com/a'}])
    assert run(env) == 0
    env[2]['download'].clear()
    assert run(env, overwrite=True) == 0
    assert not env[2]['download']
    assert len(env[2]['convert']) == 2


def test_interrupt_then_resume(env, monkeypatch):
    tasks(env[0], [{'url': 'https://example.com/a'}])
    original = batch.convert_file
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(batch, 'convert_file', interrupt)
    assert run(env) == 130
    assert rows(env)[0]['status'] == 'processing'
    assert rows(env)[0]['audio_path'] and rows(env)[0]['midi_path']
    monkeypatch.setattr(batch, 'convert_file', original)
    env[2]['download'].clear()
    assert run(env) == 0
    assert not env[2]['download']


def test_save_failure_stops_before_download(env, monkeypatch):
    tasks(env[0], [{'url': 'https://example.com/a'}])
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(batch, 'save_tasks', fail)
    assert run(env) == 2
    assert not any(env[2].values())


def test_bad_headers_and_blank_rows(tmp_path):
    p = tmp_path / 'tasks.csv'
    p.write_text('url,url\na,b\n')
    with pytest.raises(ValueError):
        batch.read_tasks(p)
    p.write_text('name\na\n')
    with pytest.raises(ValueError):
        batch.read_tasks(p)
    p.write_text('url\n\nhttps://example.com/a\n')
    assert len(batch.read_tasks(p)[1]) == 1


def test_cli_validation_and_original_mode(monkeypatch, tmp_path):
    for args in [[], ['a.mp3', '--csv', 'tasks.csv']]:
        with pytest.raises(SystemExit) as e:
            main(args)
        assert e.value.code == 2
    midi(tmp_path / 'a.mid')
    # Existing MIDI skips before checking audio decoding or loading the engine.
    audio = tmp_path / 'a.mp3'
    audio.touch()
    assert main([str(audio), '-o', str(tmp_path)]) == 0


def test_real_pipeline_custom_name_and_atomic_failure(tmp_path, monkeypatch):
    from piano2midi import pipeline
    raw_path = tmp_path / 'source.mid'
    midi(raw_path)
    raw = pretty_midi.PrettyMIDI(str(raw_path))
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *a: (raw, raw, {'output': {}}))
    out = tmp_path / 'output'
    info = convert_file(None, 'hash.webm', out, Options(), overwrite=True, output_name='花之舞')
    assert Path(info['files']['midi']) == out / '花之舞.mid'
    assert batch.valid_midi(out / '花之舞.mid')
    assert (out / 'extras' / '花之舞.raw.mid').exists()
    before = (out / '花之舞.mid').read_bytes()
    def broken_write(*args):
        raise OSError('write failed')
    monkeypatch.setattr(raw, 'write', broken_write)
    with pytest.raises(OSError):
        convert_file(None, 'hash.webm', out, Options(), overwrite=True, output_name='花之舞')
    assert (out / '花之舞.mid').read_bytes() == before
    assert not list(out.glob('.transcribe-*'))


def test_duplicate_failed_url_attempted_once(env):
    tasks(env[0], [{'url': 'bad'}, {'url': 'bad'}, {'url': 'https://example.com/a'}])
    assert run(env) == 1
    assert env[2]['download'] == ['bad', 'https://example.com/a']
    assert [row['status'] for row in rows(env)] == ['failed', 'failed', 'done']


def test_existing_unrelated_output_not_overwritten(env):
    target = env[1] / 'Same title.mid'
    midi(target)
    original = target.read_bytes()
    tasks(env[0], [{'url': 'https://example.com/a'}])
    assert run(env) == 0
    assert target.read_bytes() == original
    assert rows(env)[0]['midi_path'].endswith(f"Same title-{batch.task_id('https://example.com/a')}.mid")
