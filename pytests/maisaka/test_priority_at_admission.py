"""XM-001：真实入站、Runtime 注册与独立队列之间的所有权回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio

import pytest

from src.chat.heart_flow.heartflow_message_processor import HeartFCMessageReceiver
from src.maisaka.runtime import MaisakaHeartFlowChatting
from src.services.priority_at_reply_service import PriorityAtReplyService


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["global", "session", "closing"])
async def test_receiver_rejected_at_keeps_normal_processing(monkeypatch, rejection):
    from src.chat.heart_flow import heartflow_message_processor as receiver
    from src.chat.utils import utils
    from src.config.config import global_config
    from src.services import auto_repeat_service, bot_account_service, group_silence_emoji_service
    from src.services import memory_flow_service, priority_at_reply_service, tag_extraction_service

    service = PriorityAtReplyService(
        max_concurrent=1, max_per_session=1, max_pending=2, max_pending_per_session=1,
    )
    monkeypatch.setattr(priority_at_reply_service, "priority_at_reply_service", service)
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", True)
    monkeypatch.setattr(utils, "is_mentioned_bot_in_message", lambda message: (True, True, 0.0))
    monkeypatch.setattr(bot_account_service, "is_bot_self", lambda *args: False)
    monkeypatch.setattr(receiver.MessageUtils, "store_message_to_db_async", AsyncMock())
    monkeypatch.setattr(receiver.Person, "register_person", Mock())
    monkeypatch.setattr(group_silence_emoji_service.group_silence_emoji_service, "record_activity", Mock())
    monkeypatch.setattr(tag_extraction_service.tag_extraction_service, "notify_message_saved", Mock())
    monkeypatch.setattr(memory_flow_service.memory_automation_service, "on_incoming_message", AsyncMock())
    monkeypatch.setattr(auto_repeat_service.auto_repeat_service, "on_incoming_message", AsyncMock())
    errors = Mock()
    monkeypatch.setattr(receiver.logger, "error", errors)
    started = []

    async def reply(message, runtime, history):
        started.append((message.message_id, history))
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_reply", reply)

    def runtime(group):
        chat = SimpleNamespace(
            session_id=group, _chat_history=[], message_cache=[], _running=True,
            _agent_state="wait", _STATE_RUNNING="running",
            _reasoning_engine=SimpleNamespace(_build_history_message=AsyncMock(
                side_effect=lambda message, **kwargs: message.message_id,
            )),
        )
        for method in (
            "_emit_monitor_message_ingested", "_ensure_background_tasks_running",
            "_record_external_message_interval", "_update_message_trigger_state",
            "_prune_processed_message_cache", "_request_planner_interrupt_for_message",
            "_schedule_message_turn",
        ):
            setattr(chat, method, Mock())
        chat._is_reply_effect_tracking_enabled = lambda: False
        chat._should_continue_after_focus_gate = lambda message: True
        chat.register_message = lambda message: MaisakaHeartFlowChatting.register_message(chat, message)
        return chat

    chats = {group: runtime(group) for group in ("a", "b")}
    monkeypatch.setattr(receiver.heartflow_manager, "get_or_create_heartflow_chat", AsyncMock(
        side_effect=lambda group: chats[group],
    ))

    def message(index, group="a"):
        return SimpleNamespace(
            session_id=group, message_id=str(index), platform="qq", is_notify=False, is_command=False,
            processed_plain_text="@bot test", message_info=SimpleNamespace(
                additional_config={}, group_info=None,
                user_info=SimpleNamespace(user_id="user", user_nickname="user"),
            ),
        )

    incoming = HeartFCMessageReceiver()
    try:
        if rejection == "closing":
            await service.shutdown()
        else:
            # 一条运行、一条同群等待；global 分支再填入另一群等待以占满总队列。
            await incoming.process_message(message("running"))
            await incoming.process_message(message("pending"))
            if rejection == "global":
                await incoming.process_message(message("other", "b"))
        target = message("rejected", "b" if rejection == "global" else "a")
        await incoming.process_message(target)
        chat = chats[target.session_id]
        assert target.message_info.additional_config.get("priority_at_handled") is not True
        assert chat.message_cache == [target]
        chat._schedule_message_turn.assert_called_once()
        assert not service.owns(target)
        assert "rejected" not in chat._chat_history
        errors.assert_not_called()
        if rejection != "closing":
            # 已接纳消息重投不会被退回 Planner，也不会重复写入历史或启动回复。
            await incoming.process_message(message("running"))
            assert chats["a"]._chat_history.count("running") == 1
            assert len(service._tasks) == 1
            assert len(chats["a"].message_cache) == (1 if rejection == "session" else 0)
            await asyncio.sleep(0)
            assert started == [("running", ["running"])]
    finally:
        await service.shutdown()

@pytest.mark.asyncio
async def test_concurrent_duplicate_during_history_build_has_one_owner(monkeypatch):
    from src.services import priority_at_reply_service

    service = PriorityAtReplyService()
    monkeypatch.setattr(priority_at_reply_service, "priority_at_reply_service", service)
    building = 0
    both_building = asyncio.Event()

    async def build(message, **kwargs):
        nonlocal building
        building += 1
        if building == 2:
            both_building.set()
        await both_building.wait()
        return message.message_id

    async def reply(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_reply", reply)
    runtime = SimpleNamespace(
        session_id="a",
        _chat_history=[], _reasoning_engine=SimpleNamespace(_build_history_message=build),
        _emit_monitor_message_ingested=Mock(),
    )
    messages = [SimpleNamespace(
        session_id="a", message_id="same", message_info=SimpleNamespace(
            additional_config={"priority_at_requested": True},
        ),
    ) for _ in range(2)]
    try:
        await asyncio.wait_for(asyncio.gather(*(
            MaisakaHeartFlowChatting.register_message(runtime, message) for message in messages
        )), timeout=3)
        assert runtime._chat_history == ["same"]
        assert len(service._tasks) == 1
        runtime._emit_monitor_message_ingested.assert_called_once()
        assert all(message.message_info.additional_config["priority_at_handled"] for message in messages)
    finally:
        await service.shutdown()
