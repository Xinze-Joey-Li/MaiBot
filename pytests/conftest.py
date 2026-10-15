"""Keep observational tests out of the real replay database."""
import pytest


@pytest.fixture(autouse=True)
def isolate_decision_replay(tmp_path, monkeypatch):
    from src.decision_replay import recorder
    from src.decision_replay.store import ReplayStore

    isolated = ReplayStore(tmp_path / 'decision-replay.db')
    monkeypatch.setattr(recorder, 'store', isolated)
    monkeypatch.setattr(recorder, 'enabled', lambda: False)
    yield
    isolated.flush()
