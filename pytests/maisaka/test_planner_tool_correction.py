"""Planner 明确工具意图遗漏调用时的有限纠错，全部使用 Mock 响应。"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, Mock

import asyncio
import pytest

from src.chat.heart_flow.heartFC_utils import CycleDetail
from src.common.data_models.llm_service_data_models import LLMResponseResult
from src.llm_models.payload_content.context_item import get_item_text
from src.llm_models.payload_content.tool_option import ToolCall
from src.maisaka.chat_loop_service import ChatResponse, MaisakaChatLoopService
from src.maisaka.context.messages import ReferenceMessage
from src.maisaka.reasoning_engine import CycleRuntimeState, MaisakaReasoningEngine
from src.maisaka.focus import focus_mode_manager


def _response(content: str, calls: Optional[List[ToolCall]] = None, reasoning: str = "") -> ChatResponse:
    result = LLMResponseResult.from_portable_output(response=content, tool_calls=calls, reasoning=reasoning)
    return ChatResponse(
        output_items=result.output_items,
        request_messages=[],
        selected_history_count=1,
        tool_count=1,
        prompt_tokens=0,
        built_message_count=1,
        completion_tokens=0,
        total_tokens=0,
    )


def _engine(group: bool = False) -> MaisakaReasoningEngine:
    runtime = SimpleNamespace(
        _chat_history=[
            ReferenceMessage(
                content='<message msg_id="real-target">考考你，1+1等于几</message>',
                timestamp=datetime.now(),
                remaining_uses_value=None,
            )
        ],
        chat_stream=SimpleNamespace(is_group_session=group),
        log_prefix="[test]",
        is_action_tool_currently_available=lambda name: name == "reply",
        _end_planner_continuation=Mock(),
        _reset_consecutive_wait_count=Mock(),
        _enter_stop_state=Mock(),
    )
    engine = MaisakaReasoningEngine(runtime)
    engine._emit_planner_progress = AsyncMock()
    engine._handle_tool_calls = AsyncMock(return_value=(True, "stop_after_execution", [], []))
    return engine


async def _handle(engine: MaisakaReasoningEngine, response: ChatResponse, budget: int = 0):
    state = CycleRuntimeState(response=response)
    budget, ended = await engine._handle_planner_response_actions(
        response=response,
        cycle_detail=CycleDetail(cycle_id=1),
        state=state,
        planner_no_tool_count=budget,
        planner_extra_lines=state.planner_extra_lines,
    )
    return budget, ended, state


@pytest.mark.asyncio
async def test_real_reply_call_executes_once_without_correction():
    engine = _engine()
    response = _response("应该回复。", [ToolCall(call_id="call-1", func_name="reply", args={"msg_id": "real-target"})])
    budget, ended, _ = await _handle(engine, response)
    assert ended and budget == 1
    engine._handle_tool_calls.assert_awaited_once()
    assert engine._handle_tool_calls.call_args.args[0][0].args["msg_id"] == "real-target"
    assert not any(engine._is_planner_no_tool_hint_message(m) for m in engine._runtime._chat_history)


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("content", ["无需回复，结束思考。", "他们正在聊天，我保持安静。", "应该回复一下。", ""])
async def test_silence_or_general_analysis_does_not_retry(group, content):
    engine = _engine(group)
    budget, ended, state = await _handle(engine, _response(content))
    assert ended and budget == 0 and state.cycle_end.reason == "planner_no_tool_end"
    engine._handle_tool_calls.assert_not_awaited()
    engine._runtime._enter_stop_state.assert_called_once()


@pytest.mark.asyncio
async def test_explicit_missing_call_retries_with_context_then_executes_once():
    engine = _engine()
    original_message = engine._runtime._chat_history[0]
    budget, ended, state = await _handle(engine, _response("现在应该回应一下。直接调用 reply。"))
    assert budget == 1 and not ended and state.cycle_end.reason == "planner_no_tool_retry"
    engine._handle_tool_calls.assert_not_awaited()
    engine._runtime._enter_stop_state.assert_not_called()
    hint = engine._runtime._chat_history[-1]
    assert engine._is_planner_no_tool_hint_message(hint)
    assert hint.consume_once()  # 原有 finalize 在下一轮请求前会消费参考消息。
    service = MaisakaChatLoopService(chat_system_prompt="system")
    messages, _ = service._build_request_messages(engine._runtime._chat_history, enable_visual_message=False)
    assert any('msg_id="real-target"' in get_item_text(m) for m in messages)
    assert any("reply" in get_item_text(m) and "纠错" in get_item_text(m) for m in messages)
    assert original_message is engine._runtime._chat_history[0]
    _, ended, _ = await _handle(
        engine,
        _response("执行回复", [ToolCall(call_id="call-2", func_name="reply", args={"msg_id": "real-target"})]),
        budget,
    )
    assert ended
    engine._handle_tool_calls.assert_awaited_once()
    assert not any(engine._is_planner_no_tool_hint_message(m) for m in engine._runtime._chat_history)


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["无需回复。", "直接调用 reply。"])
async def test_correction_can_choose_silence_and_never_retries_twice(second):
    engine = _engine(group=True)
    budget, ended, _ = await _handle(engine, _response("直接调用 reply。"))
    assert not ended and budget == 1
    budget, ended, state = await _handle(engine, _response(second), budget)
    assert ended and budget == 1 and state.cycle_end.reason == "planner_no_tool_end"
    engine._handle_tool_calls.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_tool_does_not_reset_used_correction_budget():
    engine = _engine()
    engine._handle_tool_calls.return_value = (False, "", [], [])
    budget, ended, _ = await _handle(
        engine, _response("查询信息", [ToolCall(call_id="call-3", func_name="query_memory")]), 1
    )
    assert not ended and budget == 1
    _, ended, _ = await _handle(engine, _response("直接调用 reply。"), budget)
    assert ended
    engine._handle_tool_calls.assert_awaited_once()


@pytest.mark.asyncio
async def test_prior_reply_does_not_trigger_duplicate_send_from_text_plan():
    engine = _engine()
    engine._handle_tool_calls.return_value = (False, "", [], [])
    budget, _, _ = await _handle(engine, _response("执行回复", [ToolCall(call_id="call-4", func_name="reply")]))
    _, ended, _ = await _handle(engine, _response("直接调用 reply。"), budget)
    assert ended
    engine._handle_tool_calls.assert_awaited_once()


@pytest.mark.asyncio
async def test_reasoning_only_or_reply_unavailable_never_retries():
    engine = _engine()
    _, ended, _ = await _handle(engine, _response("", reasoning="直接调用 reply。"))
    assert ended
    engine._runtime.is_action_tool_currently_available = lambda name: False
    _, ended, _ = await _handle(engine, _response("直接调用 reply。"))
    assert ended


@pytest.mark.parametrize(
    "content",
    [
        "不应该调用 reply。",
        "不要直接调用 reply。",
        "如果需要就直接调用 reply。",
        "直接调用 reply？",
        "用户说“直接调用 reply”。",
        "可能直接调用 reply。",
        "I will not call reply.",
        "They say call reply.",
    ],
)
def test_negated_conditional_quoted_or_ambiguous_group_analysis_is_not_intent(content):
    assert not MaisakaReasoningEngine._has_explicit_reply_tool_intent(content)


@pytest.mark.parametrize(
    "content", ["直接调用 reply。", "现在应该调用 `reply`。", "I will call reply.", "今 reply を呼び出す。"]
)
def test_explicit_tool_action_intent(content):
    assert MaisakaReasoningEngine._has_explicit_reply_tool_intent(content)


def test_action_reminder_is_final_user_instruction_and_translated():
    service = MaisakaChatLoopService(chat_system_prompt="system")
    reminder = service._build_planner_final_user_reminder()
    messages, _ = service._build_request_messages([], enable_visual_message=False, final_user_message=reminder)
    assert get_item_text(messages[-1]) == reminder.strip()
    assert "msg_id" in reminder and "reply" in reminder
    root = Path(__file__).resolve().parents[2] / "prompts"
    for locale in ("zh-CN", "en-US", "ja-JP"):
        text = (root / locale / "maisaka_planner_action_reminder.prompt").read_text(encoding="utf-8")
        assert "{bot_name}" in text and "msg_id" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_full_loop_reuses_original_target_and_stops_after_single_correction(monkeypatch, group):
    engine = _engine(group)
    runtime = engine._runtime
    runtime.session_id = "test-session"
    runtime._running = True
    runtime._max_internal_rounds = 5
    runtime._agent_state = "stop"
    runtime._STATE_RUNNING = "run"
    runtime._internal_turn_queue = asyncio.Queue()
    runtime._internal_turn_queue.put_nowait("trigger")
    runtime._consume_forced_turn_reason = lambda: None
    runtime._start_planner_continuation = Mock()
    trigger = SimpleNamespace(message_id="real-target")
    engine._prepare_turn_start_context = AsyncMock(
        return_value=SimpleNamespace(
            trigger_message=trigger,
            cached_messages=[trigger],
            silent_reply_frequency=False,
            logical_turn_id="logical-turn",
        )
    )
    monkeypatch.setattr(focus_mode_manager, "can_decide", lambda *args, **kwargs: True)
    engine._collect_pending_messages_before_next_round = AsyncMock(return_value=[])
    engine._start_cycle_round = AsyncMock(return_value=(CycleDetail(cycle_id=1), "round"))
    engine._refresh_visual_placeholders_for_cycle = AsyncMock()
    engine._refresh_mid_term_memory_reference_for_continuation = AsyncMock()
    requests = []

    async def request(**kwargs):
        requests.append((kwargs["trigger_message"], kwargs["source_messages"]))
        kwargs["state"].response = _response("直接调用 reply。")

    async def finalize(**kwargs):
        # 模拟生产 finalize 的一次性参考消息消费，不替换受测的循环和响应控制流。
        runtime._chat_history = [m for m in runtime._chat_history if m.consume_once()]
        if kwargs["state"].cycle_end.reason == "planner_no_tool_end":
            runtime._running = False
        return kwargs["state"].cycle_end

    engine._run_planner_request = request
    engine._finalize_cycle = finalize
    await asyncio.wait_for(engine.run_loop(), timeout=1)
    assert requests == [(trigger, [trigger]), (trigger, [trigger])]
    engine._collect_pending_messages_before_next_round.assert_awaited_once_with(0)
    engine._handle_tool_calls.assert_not_awaited()
    assert not any(engine._is_planner_no_tool_hint_message(m) for m in runtime._chat_history)
