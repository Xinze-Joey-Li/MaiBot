"""正式兼容默认、显式模式、旧配置升级及实际动态调用链。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import tomlkit

from src.config import config as config_module
from src.config.config import Config
from src.config.official_configs import ChatReplyTimingConfig
from src.maisaka import mode_policy
from src.maisaka.turn_trigger import gates, scheduler


def test_compatible_default_and_new_template_use_frequency(tmp_path):
    assert ChatReplyTimingConfig().reply_trigger_mode == "frequency"
    path = tmp_path / "bot_config.toml"
    config_module.generate_new_config_file(Config, path, config_module.CONFIG_VERSION)
    data = tomlkit.parse(path.read_text(encoding="utf-8"))
    assert data["chat"]["reply_timing"]["reply_trigger_mode"] == "frequency"


@pytest.mark.parametrize("mode", [None, "frequency", "dynamic"])
def test_upgrade_preserves_explicit_modes_and_missing_field_compatibility(tmp_path, monkeypatch, mode):
    path = tmp_path / "bot_config.toml"
    config_module.generate_new_config_file(Config, path, "8.14.70")
    data = tomlkit.parse(path.read_text(encoding="utf-8"))
    if mode is None:
        del data["chat"]["reply_timing"]["reply_trigger_mode"]
    else:
        data["chat"]["reply_timing"]["reply_trigger_mode"] = mode
    path.write_text(tomlkit.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config_module, "should_apply_legacy_migration", lambda name: False)
    loaded, upgraded = config_module.load_config_from_file(Config, path, config_module.CONFIG_VERSION)
    assert upgraded
    assert loaded.chat.reply_timing.reply_trigger_mode == (mode if mode is not None else "frequency")
    written = tomlkit.parse(path.read_text(encoding="utf-8"))
    assert written["chat"]["reply_timing"]["reply_trigger_mode"] == loaded.chat.reply_timing.reply_trigger_mode


@pytest.mark.parametrize("mode", ["frequency", "dynamic"])
def test_actual_mode_logs_and_scheduler_use_real_policy(monkeypatch, mode):
    config = ChatReplyTimingConfig(reply_trigger_mode=mode)
    monkeypatch.setattr(mode_policy, "global_config", SimpleNamespace(chat=SimpleNamespace(reply_timing=config)))
    logger = Mock()
    monkeypatch.setattr(mode_policy, "logger", logger)
    mode_policy.log_reply_trigger_mode("测试会话")
    logged = logger.info.call_args.args[0]
    assert f"mode={mode}" in logged
    assert f"权重评分={'启用' if mode == 'dynamic' else '停用'}" in logged
    assert mode_policy.is_dynamic_reply_trigger_enabled() == (mode == "dynamic")
    monkeypatch.setattr(scheduler.focus_mode_manager, "can_decide", lambda *args, **kwargs: True)
    monkeypatch.setattr(gates, "is_bot_self", lambda *args: False)
    message = SimpleNamespace(message_id="m1", platform="test", processed_plain_text="麦麦知道吗？",
                              message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="user")),
                              is_mentioned=True, is_at=False, timestamp=datetime.now())
    runtime = SimpleNamespace(
        session_id="group", log_prefix="test", chat_stream=SimpleNamespace(is_group_session=True), _chat_history=[],
        _agent_state="stop", _STATE_WAIT="wait", _message_turn_scheduled=False,
        message_cache=[message], _last_processed_index=0, _idle_backoff=SimpleNamespace(should_delay=lambda count: False),
        _get_pending_message_count=lambda: 1, _get_effective_reply_frequency=lambda: 0.5,
        _is_reply_frequency_silent=lambda: False, _has_forced_turn_trigger=lambda: False,
        _get_message_trigger_threshold=Mock(return_value=1), _enqueue_message_turn=Mock(),
    )
    scored = Mock(wraps=gates.score_reply_likelihood)
    monkeypatch.setattr(gates, "score_reply_likelihood", scored)
    service = scheduler.MessageTurnScheduler(runtime)
    dynamic_gate = Mock(wraps=service._dynamic_reply_gate._gate.evaluate)
    monkeypatch.setattr(service._dynamic_reply_gate._gate, "evaluate", dynamic_gate)
    service.schedule_message_turn()
    if mode == "dynamic":
        scored.assert_called_once()
        dynamic_gate.assert_called_once()
        runtime._get_message_trigger_threshold.assert_not_called()
    else:
        scored.assert_not_called()
        dynamic_gate.assert_not_called()
        runtime._get_message_trigger_threshold.assert_called_once()
