"""纯 tag 选择、同次请求、双层概率与群冷却豁免。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio

import pytest

from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator, MaisakaReplyContext
from src.common.data_models.llm_service_data_models import LLMResponseResult
from src.common.data_models.message_component_data_model import EmojiComponent, MessageSequence, TextComponent
from src.common.prompt_i18n import load_prompt
from src.emoji_system import reply_attachment
from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
from src.services import group_emoji_gate as gate_module, send_service


@pytest.mark.parametrize("locale", ["zh-CN", "en-US", "ja-JP"])
def test_tag_prompt_has_selection_protocol_in_each_locale(locale):
    prompt = load_prompt("reply_emoji_tag", locale=locale, tags='["开心"]')
    assert "[[emoji_tag:" in prompt and '"开心"' in prompt


def test_library_tags_and_unknown_selection(monkeypatch):
    emoji = SimpleNamespace(description="开心,赞同，开心")
    monkeypatch.setattr(reply_attachment.emoji_manager, "emojis", [emoji])
    tags = reply_attachment.get_tag_candidates()
    assert tags == {"开心": [emoji], "赞同": [emoji]}
    assert reply_attachment.extract_tag("好呀\n[[emoji_tag:开心]]", tags) == ("好呀", "开心")
    assert reply_attachment.extract_tag("好呀\n[[emoji_tag:未知]]", tags) == ("好呀", "")
    assert reply_attachment.extract_tag("好呀", tags) == ("好呀", "")


@pytest.mark.asyncio
async def test_replyer_selects_tag_in_existing_single_request(monkeypatch):
    monkeypatch.setattr("src.services.tag_memory_recall.build_tag_memory_reference", AsyncMock(return_value=""))
    monkeypatch.setattr(reply_attachment.emoji_manager, "emojis", [SimpleNamespace(description="开心")])
    generator = object.__new__(BaseMaisakaReplyGenerator)
    generator._build_reply_context = AsyncMock(return_value=MaisakaReplyContext())
    generator._resolve_session_id = lambda stream: "test"
    generator._build_request_messages = Mock(return_value=[])
    generator._persist_reply_preview = Mock()
    generator._get_runtime_manager = lambda: SimpleNamespace(
        invoke_hook=AsyncMock(return_value=SimpleNamespace(kwargs={}))
    )
    item = ContextItemBuilder().set_role(RoleType.Assistant).add_text_content("好呀\n[[emoji_tag:开心]]").build()
    client = SimpleNamespace(
        task_name="replyer",
        generate_response_with_context=AsyncMock(return_value=LLMResponseResult(output_items=(item,), model_name="mock")),
    )
    generator.express_model = client
    generator.request_type = "maisaka.replyer"
    success, result = await generator.generate_reply_with_context(stream_id="test", chat_history=[], log_reply=False)
    assert success and result.completion.response_text == "好呀"
    assert result.selected_emoji_tag == "开心"
    client.generate_response_with_context.assert_awaited_once()
    requirements = generator._build_request_messages.call_args.kwargs["reply_requirements"]
    assert '"开心"' in requirements and "[[emoji_tag:" in requirements


@pytest.mark.asyncio
@pytest.mark.parametrize("probability,roll,tag,sends", [
    (0.30, 0.29, "开心", True), (0.30, 0.30, "开心", False),
    (0.50, 0.49, "开心", True), (0.50, 0.51, "开心", False),
    (0.40, 0.01, "", False), (0.40, 0.01, "未知", False),
])
async def test_two_independent_random_draws_and_emoji_only_bubble(monkeypatch, tmp_path, probability, roll, tag, sends):
    path = tmp_path / "emoji.png"
    path.write_bytes(b"image")
    monkeypatch.setattr("src.common.utils.image_path.PROJECT_ROOT", tmp_path)
    emoji = SimpleNamespace(description="开心", file_hash="hash", full_path=path)
    monkeypatch.setattr(reply_attachment.emoji_manager, "emojis", [emoji])
    uniform = Mock(return_value=probability)
    random = Mock(return_value=roll)
    monkeypatch.setattr(reply_attachment.random, "uniform", uniform)
    monkeypatch.setattr(reply_attachment.random, "random", random)
    sender = AsyncMock(return_value=SimpleNamespace(message_id="sent"))
    monkeypatch.setattr(send_service, "_send_to_target_with_message", sender)
    await reply_attachment.send_after_text("group", tag)
    uniform.assert_called_once_with(0.30, 0.50)
    random.assert_called_once_with()
    assert sender.await_count == int(sends)
    if sends:
        args = sender.call_args.kwargs
        assert args["set_reply"] is False
        assert args["maisaka_source_kind"] == "reply_emoji_attachment"
        components = args["message_sequence"].components
        assert len(components) == 1 and isinstance(components[0], EmojiComponent)


def test_pending_failure_and_exact_cooldown_boundary(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(gate_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    gate = gate_module.GroupEmojiGate()
    key = ("qq", "group")
    assert gate.acquire(key)
    assert not gate.acquire(key)
    assert gate.acquire(("qq", "other"))
    gate.finish(key, False)
    assert gate.acquire(key)
    gate.finish(key, True)
    now[0] = 219.99
    assert not gate.acquire(key)
    now[0] = 220.0
    assert gate.acquire(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_send_failure", [False, True])
async def test_platform_gate_exempts_silence_and_standalone_at_without_reset(monkeypatch, after_send_failure):
    now = [100.0]
    monkeypatch.setattr(gate_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(gate_module, "group_emoji_gate", gate_module.GroupEmojiGate())
    delivered = SimpleNamespace(has_success=True, sent_receipts=[], failed_receipts=[])
    manager = SimpleNamespace(
        ensure_send_pipeline_ready=AsyncMock(), build_route_key_from_message=lambda message: object(),
        send_message=AsyncMock(return_value=delivered),
    )
    monkeypatch.setattr(send_service, "get_platform_io_manager", lambda: manager)

    async def hook(name, message, **kwargs):
        if after_send_failure and name == "send_service.after_send":
            raise RuntimeError("post-delivery hook failed")
        return SimpleNamespace(aborted=False, kwargs={}), message

    monkeypatch.setattr(send_service, "_invoke_send_hook", hook)
    for name in ("_prepare_message_for_platform_io", "_apply_successful_delivery_receipt",
                 "_dispatch_adapter_callbacks", "_notify_memory_automation_on_message_sent"):
        monkeypatch.setattr(send_service, name, AsyncMock())
    monkeypatch.setattr(send_service, "_record_sent_emoji_usage", lambda message: None)
    monkeypatch.setattr("src.services.group_silence_emoji_service.group_silence_emoji_service.record_activity", lambda message: None)

    async def send(flag="", text=False):
        message = SimpleNamespace(
            message_id="id", platform="qq",
            message_info=SimpleNamespace(group_info=SimpleNamespace(group_id="group"), additional_config={flag: True}),
            raw_message=MessageSequence([
                TextComponent("text") if text else EmojiComponent(binary_hash="hash", binary_data=b"image"),
            ]),
        )
        return await send_service._send_via_platform_io(
            message, typing=False, set_reply=False, reply_message_id=None, storage_message=False, show_log=False,
        )

    assert await send() is not None
    assert await send() is None
    now[0] = 150.0
    assert await send("silence_emoji") is not None
    assert await send("priority_at_response") is not None
    assert await send(text=True) is not None
    assert await send() is None
    now[0] = 220.0
    assert await send() is not None  # 豁免表情没有把冷却延长到 270。


@pytest.mark.asyncio
async def test_concurrent_platform_sends_allow_only_one(monkeypatch):
    gate = gate_module.GroupEmojiGate()
    key = ("qq", "group")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def attempt():
        if not gate.acquire(key):
            return False
        entered.set()
        await release.wait()
        gate.finish(key, True)
        return True

    first = asyncio.create_task(attempt())
    await entered.wait()
    assert not await attempt()
    release.set()
    assert await first
