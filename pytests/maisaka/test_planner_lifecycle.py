from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.core.tooling import ToolExecutionResult
from src.chat.heart_flow.heartFC_utils import CycleDetail
from src.llm_models.payload_content.tool_option import ToolCall
from src.maisaka import planner_lifecycle
from src.maisaka.reasoning_engine import CycleRuntimeState, MaisakaReasoningEngine
from src.maisaka.turn_trigger import scheduler


def completed(name="arbitrary_completed_action"):
    return ToolExecutionResult(tool_name=name, success=True, metadata={
        "planner_task_completed": True, "planner_continuation_required": False,
    })


def test_multiple_completed_actions_end_only_after_all_results():
    assert planner_lifecycle.local_end_reason([completed(), completed()])
    assert not planner_lifecycle.local_end_reason([])
    assert not planner_lifecycle.local_end_reason([completed(), ToolExecutionResult(tool_name="reply", success=True)])


@pytest.mark.parametrize("result", [
    ToolExecutionResult(tool_name="search", success=True),
    ToolExecutionResult(tool_name="plugin", success=True),
    ToolExecutionResult(tool_name="reply", success=False),
    ToolExecutionResult(tool_name="chain", success=True, metadata={"planner_task_completed": True,
        "planner_continuation_required": True}),
])
def test_unknown_analysis_failure_and_explicit_chain_continue(result):
    assert not planner_lifecycle.local_end_reason([completed(), result])


@pytest.mark.asyncio
async def test_optimized_continuation_does_not_consume_new_messages(monkeypatch):
    monkeypatch.setattr(planner_lifecycle, "lifecycle_mode", lambda: "optimized")
    runtime = SimpleNamespace(_has_pending_messages=Mock(return_value=True),
        _collect_pending_messages=Mock(side_effect=AssertionError("消息不得被旧轮消费")))
    engine = MaisakaReasoningEngine(runtime)
    assert await engine._collect_pending_messages_before_next_round(1) == []
    runtime._collect_pending_messages.assert_not_called()


@pytest.mark.parametrize("mode", ["legacy", "observe", "optimized"])
@pytest.mark.parametrize("allowed", [True, False])
def test_running_messages_regate_after_stop_with_group_isolation(monkeypatch, mode, allowed):
    monkeypatch.setattr(planner_lifecycle, "lifecycle_mode", lambda: mode)
    monkeypatch.setattr(scheduler.focus_mode_manager, "can_decide", lambda *a, **kw: True)
    monkeypatch.setattr(scheduler, "is_dynamic_reply_trigger_enabled", lambda: True)
    runtimes=[]
    for group in ("g1", "g2"):
        runtime=SimpleNamespace(session_id=group, log_prefix=group, chat_stream=SimpleNamespace(is_group_session=True),
            _agent_state="running", _STATE_RUNNING="running", _STATE_WAIT="wait", _message_turn_scheduled=False,
            _get_pending_message_count=lambda: 1, _get_effective_reply_frequency=lambda: .5,
            _is_reply_frequency_silent=lambda: False, _has_forced_turn_trigger=lambda: False,
            _idle_backoff=SimpleNamespace(should_delay=lambda n: False), message_cache=[group],
            _last_processed_index=0, _enqueue_message_turn=Mock())
        service=scheduler.MessageTurnScheduler(runtime)
        service._dynamic_reply_gate=SimpleNamespace(evaluate=Mock(return_value=SimpleNamespace(
            should_trigger=allowed, detail="test", delay_seconds=None)))
        service.schedule_message_turn()
        if mode=="optimized":
            service._dynamic_reply_gate.evaluate.assert_not_called()
            runtime._enqueue_message_turn.assert_not_called()
        else:
            assert runtime._enqueue_message_turn.call_count==int(allowed)
        runtime._enqueue_message_turn.reset_mock()
        runtime._agent_state="stop"
        service.schedule_message_turn()
        assert runtime._enqueue_message_turn.call_count==int(allowed)
        assert runtime._last_processed_index==0
        runtime._message_turn_scheduled=True
        service.schedule_message_turn()
        assert runtime._enqueue_message_turn.call_count==int(allowed)
        runtimes.append(runtime)
    assert runtimes[0].message_cache!=runtimes[1].message_cache


@pytest.mark.asyncio
async def test_real_tool_batch_completes_before_local_decision(monkeypatch):
    results=[completed("reply"), completed("reply")]
    invoked=[]

    async def invoke(invocation, context):
        invoked.append(invocation.tool_name)
        return results[len(invoked)-1]

    runtime=SimpleNamespace(_tool_registry=SimpleNamespace(list_tools=AsyncMock(return_value=[]),invoke=invoke),
        _update_stage_status=Mock(), is_action_tool_currently_available=lambda n: True,
        _reset_consecutive_wait_count=Mock())
    engine=MaisakaReasoningEngine(runtime)
    engine._build_tool_execution_context=Mock(return_value=None)
    engine._build_tool_availability_context=Mock(return_value=None)
    engine._log_tool_call_source=Mock()
    engine._build_tool_invocation=lambda t, thought: SimpleNamespace(tool_name=t.func_name)
    engine._record_tool_execution_effects=AsyncMock()
    engine._append_tool_execution_result=Mock()
    engine._append_tool_display_results=AsyncMock()
    engine._emit_planner_progress=AsyncMock()
    engine._append_tool_post_history_messages=Mock()
    state=CycleRuntimeState()
    calls=[SimpleNamespace(func_name="reply",call_id=str(i)) for i in range(2)]
    paused,_,_,_=await engine._handle_tool_calls(calls,"",SimpleNamespace(),state)
    assert not paused
    assert invoked==["reply","reply"]
    assert state.tool_execution_results==results
    assert planner_lifecycle.local_end_reason(state.tool_execution_results)


def test_regate_once_without_consuming_messages(monkeypatch):
    monkeypatch.setattr(planner_lifecycle,"lifecycle_mode",lambda:"optimized")
    runtime=SimpleNamespace(session_id="one",_has_pending_messages=lambda:True,_schedule_message_turn=Mock())
    engine=MaisakaReasoningEngine(runtime)
    engine._regate_pending_after_turn()
    runtime._schedule_message_turn.assert_called_once()


def test_optimized_messages_do_not_interrupt_old_planner(monkeypatch):
    from src.maisaka.runtime import MaisakaHeartFlowChatting

    monkeypatch.setattr(planner_lifecycle,"lifecycle_mode",lambda:"optimized")
    runtime=SimpleNamespace(_planner_interrupt=SimpleNamespace(request=Mock()))
    MaisakaHeartFlowChatting._request_planner_interrupt_for_message(runtime,SimpleNamespace())
    runtime._planner_interrupt.request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "observe", "optimized"])
@pytest.mark.parametrize("unknown", [False, True])
async def test_response_handler_local_stop_and_conservative_continue(monkeypatch, mode, unknown):
    from test_planner_tool_correction import _engine, _response

    monkeypatch.setattr(planner_lifecycle,"lifecycle_mode",lambda:mode)
    engine=_engine(True)
    engine._runtime.session_id="test"
    engine._runtime._has_pending_messages=lambda: True

    async def execute(calls, thought, cycle, state):
        state.tool_execution_results=[completed()]
        if unknown:
            state.tool_execution_results.append(ToolExecutionResult(tool_name="plugin",success=True))
        return False,"",[],[]

    engine._handle_tool_calls=execute
    response=_response("执行回复",[ToolCall(call_id="one",func_name="reply")])
    state=CycleRuntimeState(response=response)
    _,ended=await engine._handle_planner_response_actions(response=response,cycle_detail=CycleDetail(cycle_id=1),
        state=state,planner_no_tool_count=0,planner_extra_lines=[])
    assert ended==(mode=="optimized" and not unknown)
    assert state.cycle_end.reason==("local_actions_completed" if ended else "tool_continue")
