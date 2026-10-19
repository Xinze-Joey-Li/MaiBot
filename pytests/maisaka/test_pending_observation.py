from datetime import datetime
from types import SimpleNamespace

import pytest

from src.maisaka.turn_trigger.pending_observation import PendingObservation
from src.maisaka.turn_trigger.reply_likelihood import ReplyLikelihoodScore
from src.maisaka.turn_trigger import gates


def check(observer, now=100, **overrides):
    values = dict(message_ids=("m1",), now=now, newest_at=100,
                  last_reply_at=0, gap=0.02, eligible=True)
    values.update(overrides)
    return observer.evaluate(**values)


def test_one_opportunity_without_new_messages():
    observer = PendingObservation()
    assert check(observer) == (False, 10)
    assert check(observer, 105) == (False, None)
    assert check(observer, 110) == (True, None)
    assert check(observer, 111) == (False, None)


@pytest.mark.parametrize("overrides", [
    {"gap": 0.031}, {"newest_at": 30}, {"last_reply_at": 90}, {"eligible": False},
])
def test_ineligible_messages_never_schedule(overrides):
    assert check(PendingObservation(), **overrides) == (False, None)


@pytest.mark.parametrize("overrides", [
    {"message_ids": ("m1", "m2")}, {"last_reply_at": 105},
    {"gap": 0.05}, {"eligible": False},
])
def test_changed_context_cancels_and_does_not_retry(overrides):
    observer = PendingObservation()
    assert check(observer) == (False, 10)
    assert check(observer, 110, **overrides) == (False, None)
    assert check(observer, 111) == (False, None)


def test_late_timer_cannot_revive_topic():
    observer = PendingObservation()
    check(observer)
    assert check(observer, 116) == (False, None)
    assert check(observer, 117) == (False, None)


def test_dynamic_gate_delays_then_allows_once_without_inflating_demand(monkeypatch):
    monkeypatch.setattr(gates, "is_bot_self", lambda *args: False)
    monkeypatch.setattr(gates, "score_reply_likelihood", lambda *args: ReplyLikelihoodScore(probability=0.32, logit=-0.7537718, contributions={}))
    monkeypatch.setattr(gates, "estimate_reply_probability", lambda *args: 0.32)
    monkeypatch.setattr(gates.time, "time", lambda: 1800000000)
    message = SimpleNamespace(message_id="m1", platform="qq", timestamp=datetime.fromtimestamp(1800000000),
                              processed_plain_text="你好", is_at=False, is_mentioned=False,
                              message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="user")))
    gate = gates.DynamicReplyTurnGate(SimpleNamespace(session_id="test-session", chat_stream=SimpleNamespace(is_group_session=True), _chat_history=[]))
    first = gate.evaluate(pending_messages=[message], frequency=0.5, allow_observation=True)
    assert first.decision == "delay"
    assert first.delay_seconds == 10
    monkeypatch.setattr(gates.time, "time", lambda: 1800000010)
    second = gate.evaluate(pending_messages=[message], frequency=0.5, allow_observation=True)
    assert second.should_trigger
    assert len(gate._gate._proactive_scores) == 1
    assert not gate.evaluate(pending_messages=[message], frequency=0.5, allow_observation=True).should_trigger
