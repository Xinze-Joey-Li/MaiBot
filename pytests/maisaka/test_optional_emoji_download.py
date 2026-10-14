from contextlib import contextmanager
from io import BytesIO

import hashlib

import pytest

from scripts import manage_qq_agent_emojis as manager


def test_download_validates_and_reuses(tmp_path, monkeypatch):
    raw = b'emoji'
    calls = []

    @contextmanager
    def request(url, timeout):
        calls.append(url)
        yield BytesIO(raw)

    monkeypatch.setattr(manager, 'urlopen', request)
    target = tmp_path / 'image.gif'
    digest = hashlib.sha256(raw).hexdigest()
    assert manager.download_file('https://example.test/image', target, digest, len(raw))
    assert not manager.download_file('https://example.test/image', target, digest, len(raw))
    assert len(calls) == 1
    with pytest.raises(ValueError):
        manager.download_file('https://example.test/image', target, '0' * 64, len(raw))
    assert target.read_bytes() == raw
    assert not list(tmp_path.glob('*.download'))


def test_no_flags_never_downloads(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manager, 'download_bundle', lambda _: pytest.fail('Unexpected download'))
    monkeypatch.setattr('sys.argv', ['manage_qq_agent_emojis.py'])
    manager.main()
    assert 'no files downloaded' in capsys.readouterr().out
