"""机器人历史不能示范内部元信息，模型输出格式检查不匹配正常正文。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.chat.replyer.maisaka_generator_base import (
    BaseMaisakaReplyGenerator, INTERNAL_REPLY_LABEL_PATTERN, MaisakaReplyContext, REPLYER_MAX_HOOK_RETRIES,
)
from src.common.data_models.llm_service_data_models import LLMResponseResult
from src.common.data_models.message_component_data_model import MessageSequence, ReplyComponent, TextComponent
from src.common.i18n import set_locale
from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType, get_item_text
from src.maisaka.context.messages import SessionBackedMessage
from src.maisaka.context.planner_messages import build_session_backed_text_message


def history(text: str, source: str = "guided_reply") -> SessionBackedMessage:
    return SessionBackedMessage(
        raw_message=MessageSequence([ReplyComponent("123"), TextComponent(text)]),
        visible_text=f"[机器人][引用消息]123\n[发言内容]{text}",
        timestamp=datetime.now(),
        source_kind=source,
    )


def test_quoted_bot_history_uses_actual_body_without_display_metadata() -> None:
    generator = object.__new__(BaseMaisakaReplyGenerator)
    for source in ("guided_reply", "auto_repeat", "priority_at_reply"):
        items = generator._build_history_messages([history("正常回复", source)], False)
        assert len(items) == 1
        assert get_item_text(items[0]) == "正常回复"
        assert items[0].role.value == "assistant"


def test_polluted_bot_history_is_not_an_assistant_format_example() -> None:
    generator = object.__new__(BaseMaisakaReplyGenerator)
    assert generator._build_history_messages([history("[发言内容]被污染正文")], False) == []


def test_synthetic_bot_history_also_excludes_planner_prefix() -> None:
    generator = object.__new__(BaseMaisakaReplyGenerator)
    message = build_session_backed_text_message(
        speaker_name="机器人", text="正常正文", timestamp=datetime.now(),
        message_id="m1", source_kind="guided_reply",
    )
    assert generator._extract_guided_bot_reply(message) == "正常正文"


@pytest.mark.asyncio
@pytest.mark.parametrize("eventually_clean", [True, False])
async def test_generated_internal_label_retries_and_never_returns_polluted_body(monkeypatch, eventually_clean) -> None:
    monkeypatch.setattr("src.services.tag_memory_recall.build_tag_memory_reference", AsyncMock(return_value=""))
    generator = object.__new__(BaseMaisakaReplyGenerator)
    generator._build_reply_context = AsyncMock(return_value=MaisakaReplyContext())
    generator._resolve_session_id = lambda stream: "test"
    generator._build_request_messages = Mock(return_value=[])
    generator._persist_reply_preview = Mock()
    hook = SimpleNamespace(invoke_hook=AsyncMock(return_value=SimpleNamespace(kwargs={})))
    generator._get_runtime_manager = lambda: hook

    def response(text):
        item = ContextItemBuilder().set_role(RoleType.Assistant).add_text_content(text).build()
        return LLMResponseResult(output_items=(item,), model_name="mock")

    responses = [response("[发言内容]错误正文")]
    if eventually_clean:
        responses.append(response("自然正文"))
    else:
        responses *= REPLYER_MAX_HOOK_RETRIES + 1
    client = SimpleNamespace(task_name="replyer", generate_response_with_context=AsyncMock(side_effect=responses))
    generator.express_model = client
    generator.request_type = "maisaka.replyer"
    success, result = await generator.generate_reply_with_context(stream_id="test", chat_history=[])
    assert success is eventually_clean
    assert result.completion.response_text == ("自然正文" if eventually_clean else "")
    assert client.generate_response_with_context.await_count == (2 if eventually_clean else REPLYER_MAX_HOOK_RETRIES + 1)
    if not eventually_clean:
        assert "内部上下文标签" in result.error_message


@pytest.mark.parametrize("text", [
    "[发言内容]你好", "【发言内容】你好", "[引用消息]123 [发言内容]你好",
    "[msg_id:123][机器人]你好", "22:01:01[msg_id:123]你好", "【标签记忆：当前群内部参考】内容",
    "[消息类型]表情包", "你好\n[参考消息]元信息",
])
def test_internal_output_headers_are_rejected(text: str) -> None:
    assert INTERNAL_REPLY_LABEL_PATTERN.search(text)


@pytest.mark.parametrize("text", [
    "你好", "[开心]今天不错", "代码是 list[int]", "你说的 [发言内容] 是什么意思？",
    "`[msg_id:123]` 是内部标记", '"[发言内容]"是什么意思？',
])
def test_normal_brackets_and_explicit_label_discussion_are_preserved(text: str) -> None:
    assert not INTERNAL_REPLY_LABEL_PATTERN.search(text)


@pytest.mark.parametrize("locale", ["zh-CN", "en-US", "ja-JP"])
def test_output_instructions_explain_metadata_in_each_locale(locale: str) -> None:
    try:
        set_locale(locale)
        assert "[msg_id:...]" in BaseMaisakaReplyGenerator._build_replyer_output_instruction()
    finally:
        set_locale("zh-CN")
