"""Privacy at the real Replyer boundary; synthetic LLM and chat only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import logging

import pytest

from src.chat.replyer import maisaka_generator_base as module
from src.common.data_models.llm_service_data_models import LLMResponseResult
from src.core.tooling import ToolInvocation
from src.decision_observation import reply_flow
from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
from test_reply_flow import (
    enable, env as _env, generator, ordinary, pipeline, reply_context as _reply_context,
    reply_tool, rows, tracker_for,
)

env = _env
reply_context = _reply_context
PRIVATE = "SYNTHETIC_PRIVATE AliceExample tel=13800001234 mail=alice@example.invalid"


def assert_private(caplog):
    text = caplog.text + repr([(r.msg, r.args, r.exc_info) for r in caplog.records])
    for marker in ["SYNTHETIC_PRIVATE", "AliceExample", "13800001234", "alice@example.invalid"]:
        assert marker not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("log_reply", [True, False])
@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.CRITICAL])
@pytest.mark.parametrize("kind", ["success", "empty", "exception", "internal_retry", "hook_retry", "hook_rewrite", "hook_error", "context_error", "prompt_error"])
async def test_reply_privacy_and_result_contract(env, monkeypatch, caplog, enabled, log_reply, level, kind):
    if enabled:
        enable(env)
    caplog.set_level(level)
    monkeypatch.setattr(module, "wait_for_simulated_typing", AsyncMock(return_value=0))
    output = "" if kind == "empty" else PRIVATE
    outputs = ("[发言内容]" + PRIVATE, PRIVATE) if kind == "internal_retry" else (output,)
    gen, calls = generator(monkeypatch, outputs=outputs, failure=RuntimeError(PRIVATE) if kind == "exception" else None)
    if kind == "context_error":
        gen._build_reply_context.side_effect = ValueError(PRIVATE)
    if kind == "prompt_error":
        gen._build_request_messages.side_effect = ValueError(PRIVATE)
    seen = []

    async def hook(name, **kwargs):
        seen.append((name, kwargs))
        if name.endswith("after_response"):
            if kind == "hook_error":
                raise ValueError(PRIVATE)
            if kind == "hook_retry":
                return SimpleNamespace(kwargs={"retry": True, "retry_reason": PRIVATE, "matched_regex": PRIVATE,
                                               "matched_regex_pattern": PRIVATE, "matched_regex_description": PRIVATE})
            if kind == "hook_rewrite":
                return SimpleNamespace(kwargs={"response": PRIVATE + " rewritten"})
        return SimpleNamespace(kwargs={})

    gen._get_runtime_manager = lambda: SimpleNamespace(invoke_hook=hook)
    tracker = tracker_for(env)
    with reply_flow.planner_scope(tracker, env.chats["group-a"], SimpleNamespace(call_id="call", name="reply")):
        success, result = await gen.generate_reply_with_context(stream_id="group-a", chat_history=[], log_reply=log_reply)
    expected_success = kind not in {"empty", "exception", "context_error", "prompt_error"}
    assert success == expected_success
    expected_count = 0 if kind in {"context_error", "prompt_error"} else (2 if kind == "internal_retry" else module.REPLYER_MAX_HOOK_RETRIES + 1 if kind == "hook_retry" else 1)
    assert len(calls) == expected_count
    if success:
        expected = PRIVATE + " rewritten" if kind == "hook_rewrite" else PRIVATE
        assert result.completion.response_text == expected
        assert result.text_fragments == [expected]
        assert result.output_items  # Content contract remains populated.
    if kind in {"exception", "context_error", "prompt_error"}:
        assert PRIVATE in result.error_message  # Keep return/Replay contract, sanitize only logs.
    if kind == "hook_retry":
        assert PRIVATE in result.metrics.extra["replyer_retry_events"][0]["retry_reason"]
    if level <= logging.INFO and log_reply and success:
        assert "response_length=" in caplog.text
    if not log_reply:
        assert "生成成功" not in caplog.text and "Replyer缓存" not in caplog.text
    assert_private(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("failure", [False, True])
async def test_actual_tool_split_send_and_failure_privacy(env, reply_context, monkeypatch, caplog, enabled, failure):
    caplog.set_level(logging.DEBUG)
    if enabled:
        enable(env)
    monkeypatch.setattr(module, "wait_for_simulated_typing", AsyncMock(return_value=0))
    gen, calls = generator(monkeypatch, outputs=(PRIVATE,), failure=RuntimeError(PRIVATE) if failure else None)
    rt = ordinary(env, reply_context, monkeypatch, gen, segments=2)
    manager, dispatches, sends = pipeline(monkeypatch)
    monkeypatch.setattr("src.emoji_system.reply_attachment.send_after_text", AsyncMock())
    invocation = ToolInvocation("reply", call_id="privacy-call", arguments={"msg_id": "msg-1"})
    with reply_flow.planner_scope(tracker_for(env), rt, invocation):
        result = await reply_tool.handle_tool(reply_context, invocation)
    assert result.success is not failure
    assert len(calls) == 1
    assert len(dispatches) == len(sends) == (0 if failure else 2)
    if enabled:
        assert rows(env, "generation")[-1]["status"] == ("generation_failed" if failure else "generation_completed")
    else:
        assert not rows(env)
    assert_private(caplog)


@pytest.mark.asyncio
async def test_priority_failure_final_callback_does_not_log_returned_error(env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    enable(env)
    gen, calls = generator(monkeypatch, failure=RuntimeError(PRIVATE))
    monkeypatch.setattr("src.chat.replyer.maisaka_generator.MaisakaReplyGenerator", lambda **kwargs: gen)
    await env.receiver.process_message(env.message("privacy-failure"))
    await env.drain()
    assert len(calls) == 1
    assert "error_type=RuntimeError" in caplog.text
    assert_private(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("show_thinking", [True, False])
async def test_reasoning_continuation_and_context_request_remain_private(env, monkeypatch, caplog, show_thinking):
    from src.config.config import global_config

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(global_config.debug, "show_maisaka_thinking", show_thinking)
    monkeypatch.setattr(module, "wait_for_simulated_typing", AsyncMock(return_value=0))
    gen, _ = generator(monkeypatch)
    gen._is_reasoning_only_response = lambda response: True
    # Execute the real request factory and real continuation call; only the provider is mocked.
    requests = []
    hooks = []
    def visible(text):
        item = ContextItemBuilder().set_role(RoleType.Assistant).add_text_content(text).build()
        return LLMResponseResult(output_items=(item,), model_name="mock")
    first = visible(PRIVATE)
    gen._build_response_continuation_items = lambda result: list(result.output_items)
    async def hook(name, **kwargs):
        hooks.append((name, kwargs))
        return SimpleNamespace(kwargs={})
    gen._get_runtime_manager = lambda: SimpleNamespace(invoke_hook=hook)
    async def model(*, context_factory, options):
        messages = await context_factory(SimpleNamespace(api_provider=SimpleNamespace(client_type="openai_responses")))
        requests.append((messages, options))
        return first if len(requests) == 1 else visible(PRIVATE + " continued")
    gen.express_model.generate_response_with_context = AsyncMock(side_effect=model)
    success, result = await gen.generate_reply_with_context(stream_id="group-a", chat_history=[])
    assert success and result.text_fragments == [PRIVATE + " continued"]
    assert len(requests) == 2 and requests[0][0] == []
    assert requests[1][0] == list(first.output_items)
    assert [name for name, _ in hooks] == ["maisaka.replyer.before_request", "maisaka.replyer.before_model_request", "maisaka.replyer.after_response"]
    assert result.metrics.extra["replyer_reasoning_continuation_count"] == 1
    assert_private(caplog)
