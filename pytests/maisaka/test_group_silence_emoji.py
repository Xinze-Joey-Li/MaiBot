"""独立静默表情的白天周期、活动重置与无引用发送。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.services.group_silence_emoji_service import GroupSilenceEmojiService, SILENCE_SECONDS


@pytest.fixture
def setup_service(monkeypatch):
    from src.chat.message_receive.chat_manager import chat_manager
    from src.emoji_system.emoji_manager import emoji_manager
    from src.services import send_service

    sessions = {
        "group": SimpleNamespace(session_id="group", platform="qq", is_group_session=True),
        "private": SimpleNamespace(session_id="private", platform="qq", is_group_session=False),
    }
    monkeypatch.setattr(chat_manager, "sessions", sessions)
    monkeypatch.setattr(emoji_manager, "emojis", [SimpleNamespace(full_path="a", file_hash="first")])
    monkeypatch.setattr(
        "src.common.utils.image_path.resolve_stored_image_path",
        lambda path: SimpleNamespace(read_bytes=lambda: b"emoji"),
    )
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(send_service, "send_group_silence_emoji", sender)
    return GroupSilenceEmojiService(), sender


@pytest.mark.asyncio
async def test_daytime_waits_45_minutes_and_repeats_without_quote(setup_service):
    service, sender = setup_service
    now = datetime(2026, 10, 14, 9)
    await service.check_once(now)
    await service.check_once(now + timedelta(seconds=SILENCE_SECONDS - 1))
    sender.assert_not_awaited()
    await service.check_once(now + timedelta(seconds=SILENCE_SECONDS))
    assert sender.await_count == 1
    assert sender.await_args.args[1] == "group"
    await service.check_once(now + timedelta(seconds=SILENCE_SECONDS + 30))
    assert sender.await_count == 1
    await service.check_once(now + timedelta(seconds=SILENCE_SECONDS * 2))
    assert sender.await_count == 2
    assert "private" not in service._last_activity


@pytest.mark.asyncio
async def test_quiet_hours_reset_to_eight_and_do_not_catch_up(setup_service):
    service, sender = setup_service
    service._last_activity["group"] = datetime(2026, 10, 13, 20).timestamp()
    await service.check_once(datetime(2026, 10, 14, 7, 59))
    await service.check_once(datetime(2026, 10, 14, 8))
    await service.check_once(datetime(2026, 10, 14, 8, 44, 59))
    sender.assert_not_awaited()
    await service.check_once(datetime(2026, 10, 14, 8, 45))
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_activity_resets_group_and_failed_send_does_not_spin(setup_service, monkeypatch):
    service, sender = setup_service
    now = datetime(2026, 10, 14, 10)
    monkeypatch.setattr("src.services.group_silence_emoji_service.time.time", lambda: now.timestamp())
    message = SimpleNamespace(
        platform="qq", is_notify=False, session_id="group",
        message_info=SimpleNamespace(group_info=object()),
    )
    service.record_activity(message)
    await service.check_once(now + timedelta(minutes=44))
    sender.assert_not_awaited()
    sender.return_value = False
    await service.check_once(now + timedelta(minutes=45))
    await service.check_once(now + timedelta(minutes=46))
    assert sender.await_count == 1


@pytest.mark.asyncio
async def test_priority_emoji_sender_has_no_reply_anchor(monkeypatch):
    from src.services import send_service

    message = SimpleNamespace(message_info=SimpleNamespace(additional_config={}), message_id="")
    builder = Mock(return_value=message)
    sender = AsyncMock(return_value=message)
    monkeypatch.setattr(send_service, "_build_outbound_session_message", builder)
    monkeypatch.setattr(send_service, "send_session_message_with_message", sender)
    target = SimpleNamespace(session_id="group", message_id="original")
    assert await send_service.send_priority_at_reply(object(), target, set_reply=False)
    assert builder.call_args.kwargs["reply_message"] is None
    assert sender.await_args.kwargs["set_reply"] is False
    assert sender.await_args.kwargs["reply_message_id"] is None
    assert sender.await_args.kwargs["reply_message"] is None


@pytest.mark.asyncio
async def test_activity_during_file_read_cancels_stale_send(setup_service, monkeypatch):
    service, sender = setup_service
    now = datetime(2026, 10, 14, 10)
    service._last_activity["group"] = now.timestamp() - SILENCE_SECONDS

    def read():
        service._last_activity["group"] = now.timestamp() + 1
        return b"emoji"

    monkeypatch.setattr(
        "src.common.utils.image_path.resolve_stored_image_path",
        lambda path: SimpleNamespace(read_bytes=read),
    )
    await service.check_once(now)
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_read_crossing_midnight_does_not_send(setup_service, monkeypatch):
    service, sender = setup_service
    now = datetime(2026, 10, 14, 23, 59, 59)
    service._last_activity["group"] = now.timestamp() - SILENCE_SECONDS
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(
        "src.services.group_silence_emoji_service.time",
        SimpleNamespace(monotonic=lambda: next(clock)),
    )
    await service.check_once(now)
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_silence_emoji_does_not_write_back_memory(monkeypatch):
    from src.services.memory_flow_service import memory_automation_service
    from src.services import send_service

    writeback = AsyncMock()
    monkeypatch.setattr(memory_automation_service, "on_message_sent", writeback)
    message = SimpleNamespace(message_info=SimpleNamespace(additional_config={"silence_emoji": True}))
    await send_service._notify_memory_automation_on_message_sent(message)
    writeback.assert_not_awaited()


def test_restored_silence_emoji_retains_its_source(monkeypatch):
    from src.maisaka import runtime

    monkeypatch.setattr(runtime, "is_bot_self", lambda *args: True)
    message = SimpleNamespace(
        platform="qq", message_info=SimpleNamespace(
            user_info=SimpleNamespace(user_id="bot"), additional_config={"silence_emoji": True}
        ),
    )
    assert runtime.MaisakaHeartFlowChatting._resolve_restored_message_source_kind(message) == "silence_emoji"
