"""权重模式使用真实评分门控，不能执行旧消息数量阈值。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.maisaka.turn_trigger import gates, scheduler


@pytest.mark.parametrize(
    "text,is_mentioned,should_trigger",
    [("麦麦知道吗？", True, True), ("[表情包:开心]", False, False)],
)
def test_dynamic_scheduler_uses_weights_without_frequency_threshold(
    monkeypatch, text: str, is_mentioned: bool, should_trigger: bool
) -> None:
    monkeypatch.setattr(scheduler, "is_dynamic_reply_trigger_enabled", lambda: True)
    monkeypatch.setattr(scheduler.focus_mode_manager, "can_decide", lambda *args, **kwargs: True)
    monkeypatch.setattr(gates, "is_bot_self", lambda *args: False)
    message = SimpleNamespace(
        message_id="m1",
        platform="test",
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="user")),
        processed_plain_text=text,
        is_mentioned=is_mentioned,
        is_at=False,
        timestamp=datetime.now(),
    )
    runtime = SimpleNamespace(
        session_id="test-session",
        log_prefix="[test]",
        chat_stream=SimpleNamespace(is_group_session=True),
        _chat_history=[],
        _agent_state="stop",
        _STATE_WAIT="wait",
        _message_turn_scheduled=False,
        message_cache=[message],
        _last_processed_index=0,
        _idle_backoff=SimpleNamespace(should_delay=lambda count: False),
        _get_pending_message_count=lambda: 1,
        _get_effective_reply_frequency=lambda: 0.5,
        _is_reply_frequency_silent=lambda: False,
        _has_forced_turn_trigger=lambda: False,
        _get_message_trigger_threshold=Mock(side_effect=AssertionError("旧阈值不应被调用")),
        _enqueue_message_turn=Mock(),
    )
    service = scheduler.MessageTurnScheduler(runtime)
    service._frequency_threshold_gate.evaluate = Mock(side_effect=AssertionError("旧门控不应被调用"))
    service.schedule_message_turn()
    assert runtime._enqueue_message_turn.call_count == int(should_trigger)
    runtime._get_message_trigger_threshold.assert_not_called()
    service._frequency_threshold_gate.evaluate.assert_not_called()
    assert len(service._dynamic_reply_gate._gate._proactive_scores) == 1
