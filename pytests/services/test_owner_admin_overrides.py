"""M2 覆盖存储、真实门控及实际发送入口回归；不调用 QQ 或模型。"""

from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import sqlite3
import threading
import time

import pytest
import pytest_asyncio

# 提前导入真实被测模块，避免其它用例的临时配置替身被首次模块导入缓存。
from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
from src.maisaka.chat_loop_service import MaisakaChatLoopService
from src.services.owner_admin import override_runtime, overrides
from src.services.owner_admin.overrides import OverrideManager
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent


@pytest_asyncio.fixture
async def manager(tmp_path, monkeypatch):
    instance = OverrideManager(tmp_path / "overrides.sqlite3")
    await instance.initialize(run_timer=False)
    monkeypatch.setattr(overrides, "override_manager", instance)
    monkeypatch.setattr(override_runtime, "override_manager", instance)
    direct = override_runtime._direct_messages.set(frozenset())
    presentation = override_runtime._presentation.set(None)
    try:
        yield instance
    finally:
        override_runtime._direct_messages.reset(direct)
        override_runtime._presentation.reset(presentation)
        await instance.shutdown()


async def set_value(manager, property, value, *, target="group-a", duration=3600, priority=100, cancel=False):
    return await manager.change(property=property, target_scope="bot" if property in {"style", "persona"} else "chat",
                                target_id=target, value=value, created_by="100000001", request_id="audit-request",
                                duration=duration, priority=priority, cancel=cancel)


@pytest.mark.asyncio
async def test_independent_properties_replacement_and_group_isolation(manager):
    first = await set_value(manager, "frequency", 0.6)
    await set_value(manager, "threshold", 0.1)
    await set_value(manager, "frequency", 0.7)
    snapshot = manager.snapshot("group-a")
    assert snapshot.frequency(0.4) == 0.7 and snapshot.threshold_delta == 0.1
    assert manager.snapshot("group-b").frequency(0.4) == 0.4
    with sqlite3.connect(manager.path) as connection:
        assert connection.execute("SELECT status FROM overrides WHERE override_id=?", (first.override_id,)).fetchone()[0] == "replaced"
        assert connection.execute("SELECT COUNT(*) FROM overrides WHERE status='active'").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_expiry_restart_and_new_webui_base_are_not_restored_to_old_values(manager, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(overrides, "time", SimpleNamespace(time=lambda: clock[0]))
    await set_value(manager, "frequency", 0.6, duration=60)
    await set_value(manager, "muted", True, duration=60)
    restarted = OverrideManager(manager.path)
    await restarted.initialize(run_timer=False)
    assert restarted.snapshot("group-a").frequency(0.3) == 0.6
    assert restarted.snapshot("group-a").muted
    clock[0] = 1061
    # 模拟 WebUI 把基础值由0.3改到0.8；没有回写旧值的恢复逻辑。
    assert restarted.snapshot("group-a").frequency(0.8) == 0.8
    assert not restarted.snapshot("group-a").muted
    after_expiry = OverrideManager(manager.path)
    await after_expiry.initialize(run_timer=False)
    with sqlite3.connect(manager.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM overrides WHERE status='expired'").fetchone()[0] == 2
    await restarted.shutdown()
    await after_expiry.shutdown()


@pytest.mark.asyncio
async def test_persistent_group_config_and_temporary_conflicts(manager):
    await set_value(manager, "frequency", 0.4, duration=None, priority=10)
    await set_value(manager, "threshold", 0.02, duration=None, priority=10)
    await set_value(manager, "mode", "quiet")
    assert manager.snapshot("group-a").frequency(0.8) == 0.2
    assert manager.snapshot("group-a").threshold_delta == pytest.approx(0.07)
    await set_value(manager, "frequency", 0.6)
    await set_value(manager, "threshold", 0.1)
    assert manager.snapshot("group-a").frequency(0.8) == 0.6
    assert manager.snapshot("group-a").threshold_delta == 0.1
    await set_value(manager, "frequency", None, cancel=True)
    await set_value(manager, "mode", None, cancel=True)
    assert manager.snapshot("group-a").frequency(0.8) == 0.4
    assert manager.snapshot("group-a").threshold_delta == 0.1
    assert manager.snapshot("group-a").frequency(0) == 0


@pytest.mark.asyncio
async def test_concurrent_changes_keep_distinct_fields_and_one_active_value(manager):
    await asyncio.gather(*[set_value(manager, "frequency", i / 20) for i in range(10)],
                         set_value(manager, "threshold", 0.2), set_value(manager, "muted", True))
    with sqlite3.connect(manager.path) as connection:
        rows = connection.execute("SELECT property,COUNT(*) FROM overrides WHERE status='active' GROUP BY property").fetchall()
    assert sorted(rows) == [("frequency", 1), ("muted", 1), ("threshold", 1)]
    assert manager.snapshot("group-a").muted and manager.snapshot("group-a").threshold_delta == 0.2


@pytest.mark.asyncio
async def test_frequency_and_threshold_use_different_real_runtime_mechanisms(manager, monkeypatch):
    from src.maisaka.runtime import MaisakaHeartFlowChatting
    from src.maisaka.turn_trigger.dynamic_gate import DynamicReplyGate
    from src.common.utils.utils_config import ChatConfigUtils

    runtime = SimpleNamespace(session_id="group-a", chat_stream=SimpleNamespace(is_group_session=True),
                              _talk_frequency_adjust=1.0, _is_focus_mode_active_for_current_chat=lambda: False,
                              _get_base_reply_frequency=lambda: 0.4)
    monkeypatch.setattr(ChatConfigUtils, "get_talk_value", lambda *args, **kwargs: 0.4)
    before = MaisakaHeartFlowChatting._get_effective_reply_frequency(runtime)
    gate = DynamicReplyGate()
    base = gate.evaluate(0.5, 0.5, 10)
    await set_value(manager, "threshold", 0.1)
    shifted = gate.evaluate(0.5, 0.5, 10, threshold_delta=manager.snapshot("group-a").threshold_delta)
    assert shifted.threshold == pytest.approx(min(1, base.threshold + 0.1))
    assert shifted.target_replies == base.target_replies and shifted.keep_ratio == base.keep_ratio
    assert MaisakaHeartFlowChatting._get_effective_reply_frequency(runtime) == before
    await set_value(manager, "frequency", 0.6)
    assert MaisakaHeartFlowChatting._get_effective_reply_frequency(runtime) == 0.6
    assert manager.snapshot("group-a").threshold_delta == 0.1
    runtime._talk_frequency_adjust = 0
    assert MaisakaHeartFlowChatting._get_effective_reply_frequency(runtime) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,frequency,delta", [("quiet", 0.2, 0.05), ("active", 0.5, -0.05),
                                                ("mention_only", 0.4, 0)])
async def test_modes_have_explicit_existing_configuration_effects(manager, mode, frequency, delta):
    await set_value(manager, "mode", mode)
    snapshot = manager.snapshot("group-a")
    assert snapshot.frequency(0.4) == frequency and snapshot.threshold_delta == delta
    assert snapshot.autonomous_allowed is (mode != "mention_only")


@pytest.mark.asyncio
async def test_live_mute_silences_normal_pending_but_preserves_direct_at(manager):
    from src.maisaka.runtime import MaisakaHeartFlowChatting

    pending = [SimpleNamespace(message_id="new", is_at=False)]
    runtime = SimpleNamespace(session_id="group-a", message_cache=pending, _last_processed_index=0,
                              _get_effective_reply_frequency=lambda: 0.5)
    await set_value(manager, "muted", True)
    assert MaisakaHeartFlowChatting._is_reply_frequency_silent(runtime)
    pending[0].is_at = True
    assert not MaisakaHeartFlowChatting._is_reply_frequency_silent(runtime)
    await set_value(manager, "muted", None, cancel=True)
    pending[0].is_at = False
    assert not MaisakaHeartFlowChatting._is_reply_frequency_silent(runtime)


@pytest.mark.asyncio
async def test_platform_entry_blocks_old_autonomous_round_and_ignores_forged_flags(manager, monkeypatch):
    from src.platform_io.manager import PlatformIOManager
    from src.platform_io.types import RouteKey

    platform = PlatformIOManager()
    drivers = Mock(return_value=[])
    monkeypatch.setattr(platform, "resolve_drivers", drivers)
    await set_value(manager, "muted", True)
    message = SimpleNamespace(session_id="group-a", message_id="outgoing",
                              raw_message=MessageSequence([TextComponent("test")]),
                              message_info=SimpleNamespace(additional_config={"priority_at_response": True}))
    await platform.send_message(message, RouteKey("qq", "bot"))
    drivers.assert_not_called()  # 连驱动选择都未进入，旧回合不能继续投递。
    message.session_id = "group-b"
    await platform.send_message(message, RouteKey("qq", "bot"))
    drivers.assert_called_once()


@pytest.mark.asyncio
async def test_mute_ack_waits_inflight_delivery_then_blocks_next_bubble(manager):
    entered = asyncio.Event()
    release = asyncio.Event()

    @override_runtime.managed_outbound
    async def send(self, message, route_key):
        entered.set()
        await release.wait()
        return "sent"

    message = SimpleNamespace(session_id="group-a", message_id="outgoing",
                              raw_message=MessageSequence([TextComponent("test")]),
                              message_info=SimpleNamespace(additional_config={}))
    sending = asyncio.create_task(send(None, message, None))
    await entered.wait()
    muting = asyncio.create_task(set_value(manager, "muted", True))
    await asyncio.sleep(0.02)
    assert not muting.done()
    release.set()
    assert await sending == "sent"
    await muting
    result = await send(None, message, None)
    assert result.receipts[0].error == "owner_autonomous_suppressed"


@pytest.mark.asyncio
async def test_real_at_reply_exemption_survives_normal_mute(manager):
    await set_value(manager, "muted", True)

    @override_runtime.priority_reply
    async def reply(self, incoming):
        @override_runtime.managed_outbound
        async def send(self, message, route_key):
            return "sent"
        return await send(None, incoming, None)

    message = SimpleNamespace(session_id="group-a", message_id="direct", is_at=True,
                              raw_message=MessageSequence([TextComponent("test")]))
    assert await reply(None, message) == "sent"
    message.is_at = False
    message.message_info = SimpleNamespace(additional_config={})
    denied = await reply(None, message)
    assert denied.receipts[0].error == "owner_autonomous_suppressed"


@pytest.mark.asyncio
async def test_historical_at_cannot_be_selected_by_model_to_bypass_mute(manager, monkeypatch):
    await set_value(manager, "muted", True)
    runtime = SimpleNamespace(session_id="group-a", message_cache=[SimpleNamespace(message_id="fresh", is_at=True)],
                              _last_processed_index=0, find_source_message_by_id=lambda message_id: None)
    override_runtime.begin_turn(runtime)
    called = Mock()

    @override_runtime.direct_reply(lambda value: (value, None))
    async def tool(context, invocation):
        called()
        return "allowed"

    context = SimpleNamespace(runtime=runtime, build_failure_result=lambda *args: "blocked")
    assert await tool(context, SimpleNamespace(arguments={"msg_id": "old-at"}, tool_name="reply")) == "blocked"
    called.assert_not_called()
    assert await tool(context, SimpleNamespace(arguments={"msg_id": "fresh"}, tool_name="reply")) == "allowed"


@pytest.mark.asyncio
async def test_planner_replyer_share_presets_and_preserve_core_identity(manager, monkeypatch):
    from src.chat.message_receive.chat_manager import chat_manager
    from src.config.config import global_config

    monkeypatch.setitem(chat_manager.sessions, "group-a", SimpleNamespace(platform="qq", account_id="bot"))
    monkeypatch.setattr(global_config.personality, "personality", "DeepSeek鲸鱼娘；保留主人关系和记忆。")
    await set_value(manager, "persona", "playful", target="bot")
    await set_value(manager, "style", "gentle", target="bot")
    runtime = SimpleNamespace(session_id="group-a", message_cache=[], _last_processed_index=0)
    override_runtime.begin_turn(runtime)
    planner = object.__new__(MaisakaChatLoopService)
    planner._session_id = "group-a"
    replyer = object.__new__(BaseMaisakaReplyGenerator)
    replyer.chat_stream = SimpleNamespace(session_id="group-a")
    shared = override_runtime.presentation_prompt("group-a")
    assert shared in planner.behavior_style_prompt
    assert shared in replyer._build_personality_prompt()
    assert replyer._select_reply_style() == shared and replyer._select_temporary_reply_style() == ""
    assert "保留主人关系和记忆" in replyer._build_personality_prompt()
    assert override_runtime.presentation("group-a").style == "gentle"  # 显式表达风格优先。
    await set_value(manager, "style", "calm", target="bot")
    assert override_runtime.presentation_prompt("group-a") == shared  # 已开始同轮保持一致。
    override_runtime.begin_turn(runtime)
    assert override_runtime.presentation("group-a").style == "calm"
    assert override_runtime.presentation_prompt("group-b") == ""  # 未知账号不能继承其他Bot预设。


@pytest.mark.parametrize("locale", ["zh-CN", "en-US", "ja-JP"])
@pytest.mark.parametrize("preset", ["gentle", "calm", "playful"])
def test_all_reviewed_presets_are_translated(locale, preset):
    from src.common.prompt_i18n import load_prompt

    value = load_prompt("owner_presentation_" + preset, locale=locale)
    assert "DeepSeek" in value and len(value) > 80


@pytest.mark.asyncio
async def test_one_expiry_coordinator_persists_expiration_and_notifies(manager, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(overrides, "time", SimpleNamespace(time=lambda: clock[0]))
    events = []
    manager.subscribe(events.append)
    await set_value(manager, "muted", True, duration=60)
    await manager.initialize()
    clock[0] += 61
    manager._changed.set()
    for _ in range(50):
        with sqlite3.connect(manager.path) as connection:
            expired = connection.execute("SELECT COUNT(*) FROM overrides WHERE status='expired'").fetchone()[0]
        if expired and len(events) >= 2:
            break
        await asyncio.sleep(0.01)
    assert expired == 1 and len(events) >= 2 and events[-1] == ("group-a",)
    assert not manager.snapshot("group-a").muted


@pytest.mark.asyncio
async def test_actual_turn_gate_receives_live_threshold_delta(manager, monkeypatch):
    from datetime import datetime
    from src.maisaka.turn_trigger import gates

    runtime = SimpleNamespace(session_id="group-a", chat_stream=SimpleNamespace(is_group_session=True), _chat_history=[])
    message = SimpleNamespace(message_id="one", platform="qq", is_at=False, is_mentioned=False,
                              timestamp=datetime.now(), processed_plain_text="普通发言",
                              message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="200000002")))
    monkeypatch.setattr(gates, "is_bot_self", lambda *args: False)
    gate = gates.DynamicReplyTurnGate(runtime)
    before = gate.evaluate(pending_messages=[message], frequency=0.5)
    entry = await set_value(manager, "threshold", 0.1)
    after = gate.evaluate(pending_messages=[message], frequency=0.5)
    assert after.replay_detail["threshold"] == pytest.approx(min(1, before.replay_detail["threshold"] + 0.1))
    assert entry.override_id in after.replay_detail["owner_overrides"]["ids"]


@pytest.mark.asyncio
async def test_mutation_cancellation_waits_transaction_and_does_not_diverge_memory(manager, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = manager._connect

    def blocking_connect():
        entered.set()
        release.wait(timeout=2)
        return original()

    monkeypatch.setattr(manager, "_connect", blocking_connect)
    task = asyncio.create_task(set_value(manager, "frequency", 0.6))
    while not entered.is_set():
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.01)
    assert not task.done()  # 阻塞的是线程，不是事件循环。
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.snapshot("group-a").frequency(0.4) == 0.6
    with sqlite3.connect(manager.path) as connection:
        assert connection.execute("SELECT value FROM overrides WHERE status='active'").fetchone()[0] == "0.6"


@pytest.mark.asyncio
async def test_failed_persistence_rolls_back_replacement_and_keeps_old_snapshot(manager, monkeypatch):
    first = await set_value(manager, "frequency", 0.4)
    original = manager._connect

    def fail_new_insert():
        connection = original()
        connection.set_authorizer(lambda action, name, *args:
                                  sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and name == "overrides"
                                  else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(manager, "_connect", fail_new_insert)
    with pytest.raises(sqlite3.DatabaseError):
        await set_value(manager, "frequency", 0.6)
    assert manager.snapshot("group-a").frequency(0.8) == 0.4
    with sqlite3.connect(manager.path) as connection:
        assert connection.execute("SELECT status FROM overrides WHERE override_id=?", (first.override_id,)).fetchone()[0] == "active"
        assert connection.execute("SELECT COUNT(*) FROM override_events").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_mute_and_mention_only_are_independent_and_unmute_does_not_cancel_mode(manager):
    await set_value(manager, "mode", "mention_only")
    await set_value(manager, "muted", True)
    await set_value(manager, "muted", None, cancel=True)
    assert not manager.snapshot("group-a").autonomous_allowed
    await set_value(manager, "mode", None, cancel=True)
    assert manager.snapshot("group-a").autonomous_allowed


@pytest.mark.asyncio
async def test_presentation_expiry_invalidates_old_pin_without_overwriting_identity(manager, monkeypatch):
    from src.chat.message_receive.chat_manager import chat_manager

    now = [time.time()]
    monkeypatch.setattr(overrides, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(override_runtime, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setitem(chat_manager.sessions, "group-a", SimpleNamespace(platform="qq", account_id="bot"))
    await set_value(manager, "style", "gentle", target="bot", duration=60)
    override_runtime.begin_turn(SimpleNamespace(session_id="group-a", message_cache=[], _last_processed_index=0))
    assert override_runtime.presentation_prompt("group-a")
    now[0] += 61
    assert override_runtime.presentation_prompt("group-a") == ""


@pytest.mark.asyncio
async def test_webui_config_replacement_is_read_live_after_expiry(manager, monkeypatch):
    from src.config.config import config_manager, global_config
    from src.maisaka.runtime import MaisakaHeartFlowChatting

    now = [time.time()]
    monkeypatch.setattr(overrides, "time", SimpleNamespace(time=lambda: now[0]))
    original = config_manager.global_config.model_copy(deep=True)
    original.chat.reply_timing.talk_value = 0.4
    original.chat.reply_timing.enable_talk_value_rules = False
    monkeypatch.setattr(config_manager, "global_config", original)
    runtime = SimpleNamespace(session_id="group-a", chat_stream=SimpleNamespace(is_group_session=True),
                              _talk_frequency_adjust=1.0, _is_focus_mode_active_for_current_chat=lambda: False,
                              _get_base_reply_frequency=lambda: global_config.chat.reply_timing.talk_value)
    assert MaisakaHeartFlowChatting._get_effective_reply_frequency(runtime) == 0.4
    await set_value(manager, "frequency", 0.6, duration=60)
    replacement = original.model_copy(deep=True)
    replacement.chat.reply_timing.talk_value = 0.8
    monkeypatch.setattr(config_manager, "global_config", replacement)  # 与WebUI热重载相同的对象替换路径。
    assert MaisakaHeartFlowChatting._get_effective_reply_frequency(runtime) == 0.6
    now[0] += 61
    assert MaisakaHeartFlowChatting._get_effective_reply_frequency(runtime) == 0.8


@pytest.mark.asyncio
async def test_active_mode_does_not_bypass_existing_focus_admission(manager, monkeypatch):
    from src.maisaka.turn_trigger import scheduler

    await set_value(manager, "mode", "active")
    monkeypatch.setattr(scheduler.focus_mode_manager, "can_decide", lambda *args, **kwargs: False)
    runtime = SimpleNamespace(session_id="group-a", log_prefix="[test]",
                              chat_stream=SimpleNamespace(is_group_session=True), _agent_state="stop",
                              _enqueue_message_turn=Mock())
    service = scheduler.MessageTurnScheduler(runtime)
    service.schedule_message_turn()
    runtime._enqueue_message_turn.assert_not_called()


@pytest.mark.asyncio
async def test_replacing_an_already_expired_entry_keeps_truthful_expiry_audit(manager, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(overrides, "time", SimpleNamespace(time=lambda: now[0]))
    first = await set_value(manager, "frequency", 0.4, duration=60)
    now[0] += 61
    await set_value(manager, "frequency", 0.6)
    with sqlite3.connect(manager.path) as connection:
        assert connection.execute("SELECT status FROM overrides WHERE override_id=?", (first.override_id,)).fetchone()[0] == "expired"
        actor = connection.execute("SELECT actor FROM override_events WHERE status='expired'").fetchone()[0]
    assert actor == "system:expiry"


@pytest.mark.asyncio
async def test_expiry_coordinator_failure_is_exposed_immediately(manager, monkeypatch, caplog):
    now = [time.time()]
    monkeypatch.setattr(overrides, "time", SimpleNamespace(time=lambda: now[0]))
    await set_value(manager, "muted", True, duration=60)

    def fail_storage():
        raise OSError("injected expiry persistence failure")

    monkeypatch.setattr(manager, "_expire_and_load", fail_storage)
    await manager.initialize()
    now[0] += 61
    manager._changed.set()
    for _ in range(100):
        if manager._expiry_task.done() and "覆盖到期协调器失败" in caplog.text:
            break
        await asyncio.sleep(0.01)
    assert "injected expiry persistence failure" in caplog.text
    assert not manager.snapshot("group-a").muted
