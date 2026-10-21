"""Mock-only Phase 2A contracts, failure injection and production gate parity."""

from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio
import json
import sqlite3
import time

import pytest

from src.decision_observation import recorder
from src.decision_observation.models import Batch, MessageEvent, PendingWorkset, Policy, identity
from src.decision_observation.store import ObservationStore


def message(mid="m1", session="group-a", text="麦麦知道吗？", mention=True):
    return SimpleNamespace(
        message_id=mid,
        session_id=session,
        platform="qq",
        timestamp=datetime.fromtimestamp(1700000000),
        processed_plain_text=text,
        is_at=False,
        is_mentioned=mention,
        is_notify=False,
        is_command=False,
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="synthetic-user"), additional_config={}),
    )


def runtime(session="group-a", messages=None):
    messages = [message(session=session)] if messages is None else messages
    rt = SimpleNamespace(
        session_id=session,
        session_name=session,
        log_prefix="[synthetic]",
        chat_stream=SimpleNamespace(session_id=session, platform="qq", is_group_session=True),
        _observation_workset=PendingWorkset(),
        message_cache=list(messages),
        _last_processed_index=0,
        _chat_history=[],
        _agent_state="stop",
        _STATE_STOP="stop",
        _STATE_WAIT="wait",
        _STATE_RUNNING="running",
        _message_turn_scheduled=False,
        _internal_turn_queue=asyncio.Queue(),
        _idle_backoff=SimpleNamespace(should_delay=lambda n: False),
        _get_effective_reply_frequency=lambda: 0.5,
        _is_reply_frequency_silent=lambda: False,
        _has_forced_turn_trigger=lambda: False,
        _defer_message_turn_check=Mock(),
    )
    rt._get_pending_message_count = lambda: len(rt.message_cache[rt._last_processed_index :])

    def enqueue(reference=None):
        from src.maisaka.runtime import MaisakaHeartFlowChatting

        rt._cancel_deferred_message_turn_task = Mock()
        MaisakaHeartFlowChatting._enqueue_message_turn(rt, reference)

    rt._enqueue_message_turn = Mock(side_effect=enqueue)
    return rt


@pytest.fixture
def observed(tmp_path, monkeypatch):
    from src.services import priority_at_reply_service
    from src.services.message_processing_rights import ProcessingRights

    service = priority_at_reply_service.priority_at_reply_service
    # Each synthetic test world starts with fresh execution ownership as well as
    # a fresh ObservationStore. Reusing m1 across tests is not a platform redelivery.
    monkeypatch.setattr(service, "_rights", ProcessingRights())
    instance = ObservationStore(tmp_path / "observation.db")
    monkeypatch.setattr(recorder, "store", instance)
    yield instance
    assert instance.close()


def enable(instance, *sessions, capacity=128):
    instance.configure(Policy(True, frozenset(sessions or ("group-a",)), 30, capacity))


def schedule(monkeypatch, rt):
    from src.maisaka.turn_trigger import gates, scheduler
    from src.services.owner_admin import system_runtime

    monkeypatch.setattr(system_runtime, "session_paused", lambda sid: False)
    monkeypatch.setattr(scheduler.focus_mode_manager, "can_decide", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "is_dynamic_reply_trigger_enabled", lambda: True)
    monkeypatch.setattr(gates, "is_bot_self", lambda *a: False)
    monkeypatch.setattr(gates.time, "time", lambda: 1700000010.0)
    service = scheduler.MessageTurnScheduler(rt)
    service._dynamic_reply_gate.evaluate = Mock(wraps=service._dynamic_reply_gate.evaluate)
    inputs = []
    build = service._dynamic_reply_gate._build_likelihood_input

    def capture_inputs(*args, **kwargs):
        value = build(*args, **kwargs)
        inputs.append(value)
        return value

    service._dynamic_reply_gate._build_likelihood_input = capture_inputs
    service.observed_production_inputs = inputs
    service.schedule_message_turn()
    return service


def test_disabled_creates_no_database_and_does_not_touch_runtime(observed):
    recorder.incoming_runtime(object(), object())
    recorder.registered(object())
    recorder.rejected(object(), "focus_unavailable")
    recorder.finalize(object(), "interrupted")
    assert observed.flush()
    assert not observed.path.exists()
    assert observed.health()["write_errors"] == 0


def test_authorized_groups_only_and_no_chat_bodies(observed):
    enable(observed)
    for rt in (runtime(), runtime("group-b"), runtime("private")):
        rt.chat_stream.is_group_session = rt.session_id != "private"
        recorder.incoming(rt.chat_stream, rt.message_cache[0])
        recorder.pending(rt, rt.message_cache)
    assert observed.flush()
    saved = observed.query("group-a")
    assert len(saved["messages"]) == len(saved["opportunities"]) == 1
    assert saved["messages"][0]["received_timestamp"] is not None
    assert not observed.query("group-b")["messages"]
    assert not observed.query("private")["messages"]
    assert "麦麦知道吗" not in json.dumps(saved, ensure_ascii=False)


@pytest.mark.parametrize("mention,text,expected", [(True, "麦麦知道吗？", "pass"), (False, "[图片]", "reject")])
def test_real_gate_persists_pass_and_reject(monkeypatch, observed, mention, text, expected):
    enable(observed)
    rt = runtime(messages=[message(text=text, mention=mention)])
    service = schedule(monkeypatch, rt)
    assert observed.flush()
    saved = observed.query("group-a")
    evaluation = saved["evaluations"][0]
    assert evaluation["gate_result"] == expected
    assert evaluation["feature_snapshot"]["values"]["mention_bot"] == mention
    assert evaluation["effective_threshold"] is not None
    assert evaluation["calculated_probability"] is not None
    assert evaluation["weights_hash"]
    assert evaluation["evaluated_at"] == 1700000010.0
    assert rt._enqueue_message_turn.call_count == int(expected == "pass")
    assert len(service._dynamic_reply_gate._gate._proactive_scores) == 1
    assert all(saved["outcomes"][0][k] == "not_observed" for k in ("planner_status", "replyer_status", "send_status"))


def test_workset_multiple_messages_rechecks_and_cross_group(monkeypatch, observed):
    enable(observed, "group-a", "group-b")
    rt = runtime(messages=[message(text="[图片]", mention=False)])
    service = schedule(monkeypatch, rt)
    rt.message_cache.append(message("m2", text="[图片]", mention=False))
    service.schedule_message_turn()
    service.schedule_message_turn()
    other = runtime("group-b", [message(session="group-b", text="[图片]", mention=False)])
    schedule(monkeypatch, other)
    assert observed.flush()
    a, b = observed.query("group-a"), observed.query("group-b")
    assert len(a["opportunities"]) == 1
    assert len(a["opportunities"][0]["message_event_ids"]) == 2
    assert [e["evaluation_sequence"] for e in a["evaluations"]] == [1, 2, 3]
    assert a["opportunities"][0]["opportunity_id"] != b["opportunities"][0]["opportunity_id"]
    assert len(a["evaluations"][0]["feature_snapshot"]["message_event_ids"]) == 1
    assert len(a["evaluations"][1]["feature_snapshot"]["message_event_ids"]) == 2


def test_actual_delayed_gate_recheck_uses_same_opportunity(monkeypatch, observed):
    from src.maisaka.turn_trigger import gates

    enable(observed)
    rt = runtime(messages=[message(text="[图片]", mention=False)])
    service = schedule(monkeypatch, rt)
    # Use a real PendingObservation near-miss, not an extra observation evaluate.
    observation = service._dynamic_reply_gate._observation
    observation._used.clear()
    observation._order.clear()
    from src.maisaka.turn_trigger.dynamic_gate import DynamicGateDecision

    production_gate = service._dynamic_reply_gate._gate
    monkeypatch.setattr(
        production_gate, "evaluate", lambda p, f, now, **kw: DynamicGateDecision(False, p, p + 0.02, 1.0, 0.5, 0, 0.5)
    )
    service.schedule_message_turn()
    rt._defer_message_turn_check.assert_called_once_with(10.0)
    monkeypatch.setattr(gates.time, "time", lambda: 1700000020.0)
    service.schedule_message_turn()
    assert observed.flush()
    saved = observed.query("group-a")
    assert len(saved["opportunities"]) == 1
    assert [e["decision_reason"] for e in saved["evaluations"]] == ["dynamic_wait", "dynamic_delay", "dynamic_trigger"]
    assert saved["evaluations"][-1]["evidence"]["budget"]["should_trigger"] is False
    assert saved["evaluations"][-1]["evidence"]["observation"] is True


def test_consumption_is_not_planner_execution_and_new_pending_opens_new_identity(monkeypatch, observed):
    from src.maisaka.runtime import MaisakaHeartFlowChatting
    from src.maisaka import runtime as runtime_module

    enable(observed)
    monkeypatch.setattr(runtime_module.focus_mode_manager, "mark_read", Mock())
    rt = runtime()
    schedule(monkeypatch, rt)
    first = rt._observation_workset.opportunity.opportunity_id
    assert MaisakaHeartFlowChatting._collect_pending_messages(rt) == rt.message_cache
    rt.message_cache.append(message("m2"))
    recorder.registered(rt)
    assert rt._observation_workset.opportunity.opportunity_id != first
    assert observed.flush()
    saved = observed.query("group-a")
    assert {o["status"] for o in saved["opportunities"]} == {"pending", "consumed"}
    assert all(o["planner_status"] == "not_observed" for o in saved["outcomes"])


def test_duplicate_messages_and_batches_are_idempotent(observed):
    enable(observed)
    rt = runtime()
    recorder.incoming(rt.chat_stream, rt.message_cache[0])
    recorder.incoming(rt.chat_stream, rt.message_cache[0])
    recorder.evaluate(rt, rt.message_cache, "reject", "dynamic_wait")
    assert observed.flush()
    saved = observed.query("group-a")
    assert len(saved["messages"]) == len(saved["opportunities"]) == len(saved["evaluations"]) == 1
    # Replay precisely the immutable queued payload, including the same evaluation ID.
    captured = []
    original = observed.submit
    observed.submit = lambda batch: captured.append(batch) or True
    recorder.evaluate(rt, rt.message_cache, "reject", "dynamic_wait")
    observed.submit = original
    original(captured[-1])
    original(captured[-1])
    assert observed.flush()
    assert len(observed.query("group-a")["evaluations"]) == 2


@pytest.mark.parametrize("operation", ["disable", "revoke", "delete"])
def test_revocation_and_delete_block_queued_and_late_writes(monkeypatch, observed, operation):
    enable(observed)
    rt = runtime()
    recorder.evaluate(rt, rt.message_cache, "reject", "dynamic_wait")
    assert observed.flush()
    entered, release = Event(), Event()
    write = observed._write

    def blocked(batches):
        entered.set()
        assert release.wait(2)
        write(batches)

    monkeypatch.setattr(observed, "_write", blocked)
    recorder.evaluate(rt, rt.message_cache, "reject", "dynamic_wait")
    assert entered.wait(2)
    if operation == "delete":
        observed.delete_session("group-a")
    else:
        observed.configure(Policy() if operation == "disable" else Policy(True, frozenset({"group-b"})))
    release.set()
    assert observed.flush()
    recorder.evaluate(rt, rt.message_cache, "pass", "late")
    assert observed.flush()
    saved = observed.query("group-a")
    assert len(saved["evaluations"]) == (0 if operation == "delete" else 1)
    assert observed.revoked > 0


def test_storage_lock_failure_is_counted_without_blocking_admission(observed):
    enable(observed)
    rt = runtime()
    recorder.evaluate(rt, rt.message_cache, "reject", "initial")
    assert observed.flush()
    connection = sqlite3.connect(observed.path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        recorder.evaluate(rt, rt.message_cache, "pass", "locked")
        assert time.perf_counter() - started < 0.05
        assert observed.flush()
        assert observed.errors > 0 and observed.dropped > 0
    finally:
        connection.rollback()
        connection.close()
    assert len(observed.query("group-a")["evaluations"]) == 1


def test_full_queue_and_writer_exception_do_not_escape(monkeypatch, observed, caplog):
    enable(observed, capacity=1)
    entered, release = Event(), Event()

    def broken(batches):
        entered.set()
        assert release.wait(2)
        raise sqlite3.OperationalError("secret chat body must not appear")

    monkeypatch.setattr(observed, "_write", broken)
    rt = runtime()
    recorder.incoming(rt.chat_stream, rt.message_cache[0])
    assert entered.wait(2)
    for _ in range(10):
        recorder.incoming(rt.chat_stream, rt.message_cache[0])
    release.set()
    assert observed.flush()
    assert observed.dropped >= 9 and observed.errors > 0
    assert len([r for r in caplog.records if r.name == "decision_observation"]) == 1
    assert "secret chat body" not in caplog.text


def test_retention_query_no_side_effect_and_restart_interruption(observed):
    assert not observed.query("group-a")["messages"] and not observed.path.exists()
    enable(observed)
    rt = runtime()
    recorder.evaluate(rt, rt.message_cache, "reject", "dynamic_wait")
    assert observed.flush()
    assert observed.close()
    next_run = ObservationStore(observed.path)
    enable(next_run)
    try:
        next_run.cleanup(now=1700000011.0)
        saved = next_run.query("group-a")
        assert saved["opportunities"][0]["status"] == "interrupted"
        next_run.cleanup(now=time.time() + 31 * 86400)
        assert not next_run.query("group-a")["messages"]
        assert not next_run.query("group-a")["opportunities"]
    finally:
        assert next_run.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mention,text", [(True, "麦麦知道吗？"), (False, "[图片]")])
@pytest.mark.parametrize("fault", ["none", "writer", "adapter"])
async def test_enabled_disabled_production_behavior_parity(monkeypatch, observed, mention, text, fault):
    from src.maisaka.runtime import MaisakaHeartFlowChatting
    from src.maisaka.reasoning_engine import CycleRuntimeState, MaisakaReasoningEngine
    from src.maisaka import runtime as runtime_module

    monkeypatch.setattr(runtime_module.focus_mode_manager, "mark_read", Mock())
    snapshots = []
    for enabled in (False, True):
        observed.configure(Policy(enabled, frozenset({"group-a"})))
        if enabled and fault == "writer":
            monkeypatch.setattr(observed, "_write", Mock(side_effect=sqlite3.OperationalError("injected")))
        if enabled and fault == "adapter":
            monkeypatch.setattr(recorder, "_message", Mock(side_effect=ValueError("injected")))
        rt = runtime(messages=[message(text=text, mention=mention)])
        service = schedule(monkeypatch, rt)
        planner, replyer, send = AsyncMock(), AsyncMock(), AsyncMock()
        planner.return_value = SimpleNamespace(
            content="", raw_messages=[], tool_calls=[SimpleNamespace(func_name="reply", call_id="one")]
        )
        engine = MaisakaReasoningEngine(rt)
        engine._should_replace_reasoning = lambda content: False
        engine._emit_planner_progress = AsyncMock()

        async def tools(calls, thought, cycle, state, rt=rt, replyer=replyer, send=send):
            for call in calls:
                if call.func_name == "reply":
                    await replyer(rt.session_id, [m.message_id for m in rt.message_cache])
                    await send(rt.session_id)
            return True, "wait", [], []

        engine._handle_tool_calls = tools
        if not rt._internal_turn_queue.empty():
            rt._internal_turn_queue.get_nowait()
            consumed = MaisakaHeartFlowChatting._collect_pending_messages(rt)
            response = await planner([m.message_id for m in consumed])
            await engine._handle_planner_response_actions(
                response=response,
                cycle_detail=SimpleNamespace(time_records={}),
                state=CycleRuntimeState(),
                planner_no_tool_count=0,
                planner_extra_lines=[],
            )
        gate = service._dynamic_reply_gate._gate
        assert service._dynamic_reply_gate.evaluate.call_count == 1
        snapshots.append(
            (
                rt._agent_state,
                rt._last_processed_index,
                rt._message_turn_scheduled,
                list(gate._proactive_scores),
                list(gate._reply_times),
                set(gate._counted_message_ids),
                service.observed_production_inputs,
                planner.call_args_list,
                replyer.call_args_list,
                send.call_args_list,
            )
        )
    assert snapshots[0] == snapshots[1]
    assert len(snapshots[1][-3]) == int(mention)
    assert observed.flush()


def test_payload_example_and_admission_benchmark(monkeypatch, observed, tmp_path):
    monkeypatch.setattr(time, "time", lambda: 1700000010.0)
    enable(observed, capacity=65536)
    rt = runtime()
    recorder.incoming(rt.chat_stream, rt.message_cache[0], received_at=1700000005.0)
    schedule(monkeypatch, rt)
    recorder.finalize(rt, "consumed")
    assert observed.flush()
    example = observed.query("group-a")
    assert example["messages"][0]["event_timestamp"] <= example["messages"][0]["received_timestamp"]
    assert example["messages"][0]["received_timestamp"] <= example["opportunities"][0]["created_at"]
    (tmp_path / "synthetic-example.json").write_text(
        json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    batch = Batch(
        "group-a",
        observed.generation("group-a"),
        (
            MessageEvent(
                identity("message", "group-a", "benchmark"),
                "group-a",
                "benchmark",
                "synthetic-user",
                1700000000.0,
                None,
                observed.run_id,
            ),
        ),
    )
    latencies = []
    for _ in range(2000):
        start = time.perf_counter_ns()
        observed.submit(batch)
        latencies.append((time.perf_counter_ns() - start) / 1000)
    assert observed.flush()
    latencies.sort()
    print(
        "OBS_ADMISSION_US",
        {"p50": latencies[999], "p95": latencies[1899], "p99": latencies[1979], "max": latencies[-1]},
    )
    print("OBS_SYNTHETIC_EXAMPLE", json.dumps(example, ensure_ascii=False))


@pytest.mark.asyncio
async def test_real_receiver_and_runtime_registration_are_observed(monkeypatch, observed):
    from src.chat.heart_flow import heartflow_message_processor as receiver
    from src.maisaka.runtime import MaisakaHeartFlowChatting
    from src.services import auto_repeat_service, group_silence_emoji_service, memory_flow_service
    from src.services import priority_at_reply_service, tag_extraction_service
    from src.services.owner_admin import system_runtime

    enable(observed)
    monkeypatch.setattr(system_runtime, "session_paused", lambda sid: False)
    rt = runtime(messages=[])
    rt._running = True
    rt._reasoning_engine = SimpleNamespace(_build_history_message=AsyncMock())
    for name in (
        "_ensure_background_tasks_running",
        "_record_external_message_interval",
        "_update_message_trigger_state",
        "_emit_monitor_message_ingested",
        "_prune_processed_message_cache",
        "_request_planner_interrupt_for_message",
    ):
        setattr(rt, name, Mock())
    rt._is_reply_effect_tracking_enabled = lambda: False
    rt._should_continue_after_focus_gate = lambda msg: True
    service = schedule(monkeypatch, rt)
    rt._schedule_message_turn = service.schedule_message_turn
    rt.register_message = lambda msg: MaisakaHeartFlowChatting.register_message(rt, msg)
    monkeypatch.setattr(receiver.heartflow_manager, "get_or_create_heartflow_chat", AsyncMock(return_value=rt))
    monkeypatch.setattr(receiver.MessageUtils, "store_message_to_db_async", AsyncMock())
    monkeypatch.setattr(receiver.Person, "register_person", Mock())
    monkeypatch.setattr(priority_at_reply_service.priority_at_reply_service, "claim", Mock(return_value=False))
    monkeypatch.setattr(group_silence_emoji_service.group_silence_emoji_service, "record_activity", Mock())
    monkeypatch.setattr(tag_extraction_service.tag_extraction_service, "notify_message_saved", Mock())
    monkeypatch.setattr(memory_flow_service.memory_automation_service, "on_incoming_message", AsyncMock())
    monkeypatch.setattr(auto_repeat_service.auto_repeat_service, "on_incoming_message", AsyncMock())
    msg = message()
    msg.message_info.group_info = SimpleNamespace(group_name="synthetic-group", group_id="synthetic-group-id")
    msg.message_info.user_info.user_nickname = "synthetic-user"
    msg.message_info.user_info.user_cardname = "synthetic-user"
    errors = Mock()
    monkeypatch.setattr(receiver.logger, "error", errors)
    await receiver.HeartFCMessageReceiver().process_message(msg)
    errors.assert_not_called()
    assert observed.flush()
    saved = observed.query("group-a")
    assert saved["messages"][0]["received_timestamp"] == 1700000010.0
    assert saved["evaluations"][0]["gate_result"] == "pass"
    assert rt.message_cache == [msg]
    assert rt._internal_turn_queue.get_nowait() == "message"


def test_disable_then_enable_creates_new_opportunity_and_interrupts_old(observed):
    enable(observed)
    rt = runtime()
    recorder.evaluate(rt, rt.message_cache, "reject", "before_disable")
    assert observed.flush()
    first = rt._observation_workset.opportunity.opportunity_id
    observed.configure(Policy())
    recorder.evaluate(rt, rt.message_cache, "pass", "while_disabled")
    enable(observed)
    recorder.evaluate(rt, rt.message_cache, "reject", "after_enable")
    assert observed.flush()
    saved = observed.query("group-a")
    assert len(saved["opportunities"]) == 2
    assert rt._observation_workset.opportunity.opportunity_id != first
    assert {e["decision_reason"] for e in saved["evaluations"]} == {"before_disable", "after_enable"}
    assert next(o for o in saved["opportunities"] if o["opportunity_id"] == first)["status"] == "interrupted"


def test_deleted_session_cannot_be_recreated_from_runtime_pending(observed):
    enable(observed)
    rt = runtime()
    recorder.evaluate(rt, rt.message_cache, "reject", "initial")
    assert observed.flush()
    observed.delete_session("group-a")
    recorder.registered(rt)
    recorder.evaluate(rt, rt.message_cache, "pass", "old_pending")
    recorder.incoming(rt.chat_stream, message("new-arrival"))
    assert observed.flush()
    assert not observed.query("group-a")["messages"]


@pytest.mark.asyncio
async def test_database_delay_does_not_stall_event_loop(monkeypatch, observed):
    enable(observed)
    entered, release = Event(), Event()
    write = observed._write

    def slow(batches):
        entered.set()
        release.wait(2)
        write(batches)

    monkeypatch.setattr(observed, "_write", slow)
    rt = runtime()
    recorder.incoming(rt.chat_stream, rt.message_cache[0])
    assert await asyncio.to_thread(entered.wait, 2)
    ticks = 0
    for _ in range(10):
        recorder.evaluate(rt, rt.message_cache, "reject", "slow_storage")
        await asyncio.sleep(0.001)
        ticks += 1
    release.set()
    assert ticks == 10
    assert await asyncio.to_thread(observed.flush)


def test_normal_observation_logs_are_silent_and_feature_schema_is_strict(monkeypatch, observed, caplog):
    enable(observed)
    rt = runtime()
    schedule(monkeypatch, rt)
    assert observed.flush()
    assert not [r for r in caplog.records if r.name == "decision_observation"]
    recorder.evaluate(rt, rt.message_cache, "reject", "invalid_feature", {"features": {"text": "secret"}})
    assert observed.flush()
    assert observed.errors == 1
    assert "secret" not in json.dumps(observed.query("group-a"))


def test_cross_session_payload_is_rejected_without_corrupting_production(observed):
    enable(observed, "group-a", "group-b")
    rt = runtime()
    recorder.pending(rt, [message(session="group-b")])
    assert observed.errors == 1
    assert observed.flush()
    assert not observed.query("group-a")["messages"]


def test_snapshot_does_not_include_later_message_mutations(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    schedule(monkeypatch, rt)
    rt.message_cache[0].processed_plain_text = "[图片]"
    rt.message_cache[0].is_mentioned = False
    rt.message_cache.append(message("future", text="future content"))
    assert observed.flush()
    saved = observed.query("group-a")["evaluations"][0]["feature_snapshot"]
    assert saved["values"]["mention_bot"] is True
    assert saved["values"]["pending_count"] == 1
    assert identity("message", "group-a", "future") not in saved["message_event_ids"]


@pytest.mark.asyncio
async def test_reload_policy_operates_independently_of_replay(monkeypatch, observed):
    from src.config.config import global_config

    monkeypatch.setattr(global_config.decision_observation, "enabled", True)
    monkeypatch.setattr(global_config.decision_observation, "authorized_session_ids", ["group-a"])
    await recorder.reload_policy()
    assert observed.generation("group-a") is not None
    monkeypatch.setattr(global_config.decision_observation, "enabled", False)
    await recorder.reload_policy()
    assert observed.generation("group-a") is None


def test_end_to_end_observation_adapter_cost(monkeypatch, observed, tmp_path):
    enable(observed, capacity=65536)
    rt = runtime()
    service = schedule(monkeypatch, rt)
    detail = service._dynamic_reply_gate.evaluate(
        pending_messages=rt.message_cache, frequency=0.5, allow_observation=False
    ).replay_detail
    timings = {}
    for enabled in (False, True):
        observed.configure(Policy(enabled, frozenset({"group-a"}), 30, 65536))
        latencies = []
        for _ in range(1000):
            start = time.perf_counter_ns()
            recorder.evaluate(rt, rt.message_cache, "pass", "synthetic_benchmark", detail)
            latencies.append((time.perf_counter_ns() - start) / 1000)
        assert observed.flush(10)
        latencies.sort()
        timings[str(enabled)] = {
            "p50_us": latencies[499],
            "p95_us": latencies[949],
            "p99_us": latencies[989],
            "max_us": latencies[-1],
        }
    (tmp_path / "adapter-benchmark.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
    print("OBS_ADAPTER_US", timings)


@pytest.mark.asyncio
async def test_disable_rolls_back_an_inflight_transaction(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    recorder.incoming(rt.chat_stream, rt.message_cache[0])
    assert observed.flush()
    entered, release = Event(), Event()
    connect = observed._connect

    def traced_connect():
        connection = connect()

        def trace(sql):
            if sql.startswith("INSERT") and not entered.is_set():
                entered.set()
                release.wait(2)

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(observed, "_connect", traced_connect)
    recorder.evaluate(rt, rt.message_cache, "reject", "inflight")
    assert await asyncio.to_thread(entered.wait, 2)
    disabled = asyncio.create_task(asyncio.to_thread(observed.configure, Policy()))
    for _ in range(100):
        if observed.generation("group-a") is None:
            break
        await asyncio.sleep(0.001)
    assert observed.generation("group-a") is None
    release.set()
    await asyncio.wait_for(disabled, 2)
    assert observed.flush()
    assert not observed.query("group-a")["opportunities"]
    assert observed.revoked > 0


def test_close_has_a_bounded_drain_and_prevents_late_data(monkeypatch, observed):
    enable(observed)
    entered, release = Event(), Event()
    write = observed._write

    def blocked(batches):
        entered.set()
        release.wait(2)
        write(batches)

    monkeypatch.setattr(observed, "_write", blocked)
    rt = runtime()
    recorder.incoming(rt.chat_stream, rt.message_cache[0])
    assert entered.wait(2)
    start = time.perf_counter()
    assert observed.close(0.02) is False
    assert time.perf_counter() - start < 0.2
    release.set()
    assert observed.flush()
    assert not observed.query("group-a")["messages"]
