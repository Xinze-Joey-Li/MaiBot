"""表情附件保留历史，但不计作额外发言或更新时间。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.maisaka.turn_trigger.gates import DynamicReplyTurnGate
from src.services import send_service


def entry(now, seconds, source):
    return SimpleNamespace(timestamp=now - timedelta(seconds=seconds), source=source, count_in_context=True)


def test_single_text_and_attachment_have_same_logical_reply_statistics():
    now = datetime.now()
    text_history = [entry(now, 40, "user"), entry(now, 20, "guided_reply")]
    with_attachment = text_history + [entry(now, 2, "reply_emoji_attachment")]
    base = DynamicReplyTurnGate(SimpleNamespace(_chat_history=text_history))
    attached = DynamicReplyTurnGate(SimpleNamespace(_chat_history=with_attachment))
    assert base._summarize_recent_history(now.timestamp()) == attached._summarize_recent_history(now.timestamp())
    assert attached._summarize_recent_history(now.timestamp()) == (1, 2, 20)
    assert len(attached._runtime._chat_history) == 3


def test_multiple_independent_text_replies_still_count():
    now = datetime.now()
    history = [entry(now, 60, "user"), entry(now, 50, "guided_reply"), entry(now, 45, "reply_emoji_attachment"),
               entry(now, 15, "guided_reply"), entry(now, 1, "reply_emoji_attachment")]
    gate = DynamicReplyTurnGate(SimpleNamespace(_chat_history=history))
    assert gate._summarize_recent_history(now.timestamp()) == (2, 3, 15)


def test_attachment_without_text_never_sets_last_text_time():
    now = datetime.now()
    old = [entry(now, 700, "guided_reply"), entry(now, 1, "reply_emoji_attachment")]
    gate = DynamicReplyTurnGate(SimpleNamespace(_chat_history=old))
    assert gate._summarize_recent_history(now.timestamp()) == (0, 0, 700)


@pytest.mark.asyncio
async def test_attachment_source_is_persisted_before_delivery_and_synced_to_history(monkeypatch):
    message = SimpleNamespace(message_id="emoji-id", message_info=SimpleNamespace(additional_config={}))
    delivery = AsyncMock(return_value=message)
    synced = []
    monkeypatch.setattr(send_service, "_send_via_platform_io", delivery)
    monkeypatch.setattr(send_service, "_sync_sent_message_to_maisaka_history", lambda message, **kwargs: synced.append((message, kwargs)))
    result = await send_service.send_session_message_with_message(
        message, sync_to_maisaka_history=True, maisaka_source_kind="reply_emoji_attachment",
    )
    assert result is message
    assert delivery.call_args.args[0].message_info.additional_config["reply_emoji_attachment"] is True
    assert synced == [(message, {"source_kind": "reply_emoji_attachment"})]


def test_restored_attachment_source_is_preserved(monkeypatch):
    from src.maisaka import runtime

    monkeypatch.setattr(runtime, "is_bot_self", lambda *args: True)
    message = SimpleNamespace(platform="qq", message_info=SimpleNamespace(
        user_info=SimpleNamespace(user_id="bot"), additional_config={"reply_emoji_attachment": True}))
    assert runtime.MaisakaHeartFlowChatting._resolve_restored_message_source_kind(message) == "reply_emoji_attachment"
