from types import SimpleNamespace

import pytest

from src.emoji_system import local_selection as selection


def score(**overrides):
    values = dict(tag="开心", emotions=["开心"], description="游戏开心",
                  metadata={"keywords": ["游戏"], "useWhen": ["赢了"], "quality": 1},
                  context="游戏赢了", sent_at=[], now=10000)
    values.update(overrides)
    return selection.score_candidate(**values)


def test_score_weights_and_missing_metadata():
    assert score()["total"] == 100
    assert score(metadata={})["total"] == 55
    assert score(context="下雨了")["total"] == 60
    assert score(sent_at=[10000] * 4)["total"] == 80


def test_penalties_are_linear_and_expire():
    assert selection.penalties([10000] * 4, 10000) == (14, 6)
    assert selection.penalties([10000] * 4, 11800) == (7, 3)
    assert selection.penalties([10000] * 4, 13600) == (0, 0)
    assert selection.penalties([10000], 10000) == (14, 0)


def test_selection_threshold_singleton_and_group_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(selection, "HISTORY_PATH", tmp_path / "history.db")
    monkeypatch.setattr(selection, "METADATA_PATH", tmp_path / "metadata.json")
    monkeypatch.setattr(selection.time, "time", lambda: 10000)
    a = SimpleNamespace(file_hash="a", description="开心", emotion=["开心"])
    b = SimpleNamespace(file_hash="b", description="开心", emotion=["开心"])
    assert selection.select_candidate("g1", "开心", [a], "") is a
    assert selection.select_candidate("g1", "开心", [a, b], "游戏赢了") is None
    selection.METADATA_PATH.write_text(
        '{"a":{"keywords":["游戏"],"useWhen":["赢了"],"quality":1},'
        '"b":{"keywords":["游戏"],"useWhen":["赢了"],"quality":1}}', encoding="utf-8")
    selection.record_sent("g1", "a", 10000)
    assert selection.select_candidate("g1", "开心", [a, b], "游戏赢了") is b
    assert selection.select_candidate("g2", "开心", [a, b], "游戏赢了") is a
    assert selection.select_candidate("g1", "开心", [a, b], "下雨了") is None


def test_invalid_quality_is_not_silently_accepted():
    with pytest.raises(ValueError):
        score(metadata={"quality": 2})
