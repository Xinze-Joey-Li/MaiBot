"""Explicit ordinary-path receipts through real scheduler/queue/engine with Mock QQ/LLM."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio
import json
import sqlite3
import time

import pytest

from src.chat.heart_flow.heartFC_utils import CycleDetail
from src.decision_observation import recorder
from src.decision_observation.execution import ExecutionTracker
from src.decision_observation.models import MessageTrigger, Policy, SCHEMA_VERSION, identity
from src.decision_observation.store import ObservationStore
from src.decision_replay import recorder as replay
from src.maisaka.reasoning_engine import CycleEnd, MaisakaReasoningEngine
from src.maisaka.runtime import MaisakaHeartFlowChatting
from src.maisaka.turn_trigger.gates import TurnGateResult
from test_observation import enable, message, runtime, schedule


@pytest.fixture
def observed(tmp_path, monkeypatch):
    from src.services import priority_at_reply_service
    from src.services.message_processing_rights import ProcessingRights

    # A fresh synthetic world must not inherit a prior test's retained m1.
    monkeypatch.setattr(priority_at_reply_service.priority_at_reply_service, "_rights", ProcessingRights())
    instance = ObservationStore(tmp_path / "observation.db")
    monkeypatch.setattr(recorder, "store", instance)
    yield instance
    assert instance.close()


def harness(monkeypatch, rt, *, cycles=1, tools=(), quiet=None, on_request=None, waiting=False):
    """Keep production dequeue, quiet-period preparation, consumption and run loop."""
    from src.maisaka import planner_lifecycle
    from src.services.owner_admin import override_runtime, system_runtime

    monkeypatch.setattr(override_runtime, "begin_turn", lambda rt: None)
    monkeypatch.setattr(system_runtime, "session_paused", lambda sid: False)
    monkeypatch.setattr(system_runtime, "paused", lambda account: False)
    monkeypatch.setattr(system_runtime, "stale_lease", lambda account: False)
    monkeypatch.setattr(system_runtime, "check_cycle", lambda sid: None)
    monkeypatch.setattr(planner_lifecycle, "lifecycle_mode", lambda: "legacy")
    rt._running = True
    rt._max_internal_rounds = cycles
    rt._max_context_size = 4096
    rt._decision_replay_turn_id = None
    rt._pending_wait_tool_call_id = None
    rt._pending_wait_logical_turn_id = None
    rt._wait_timeout_task = None
    rt._consume_forced_turn_reason = lambda: None
    rt._start_planner_continuation = Mock()
    rt._end_planner_continuation = Mock()
    rt._mark_message_turn_unscheduled = lambda: setattr(rt, "_message_turn_scheduled", False)
    rt._wait_for_message_quiet_period = quiet or AsyncMock()
    rt._has_pending_messages = lambda: rt._get_pending_message_count() > 0
    rt._has_pending_wait_tool_call = lambda: rt._pending_wait_tool_call_id is not None
    rt._enter_running_state = lambda: setattr(rt, "_agent_state", "running")
    rt._enter_stop_state = lambda: setattr(rt, "_agent_state", "stop")
    rt._update_stage_status = Mock()
    rt._bind_planner_interrupt_flag = Mock()
    rt._unbind_planner_interrupt_flag = Mock()
    rt._log_internal_loop_cancelled = Mock()
    rt._log_cycle_started = Mock()
    rt._clear_focus_cooldown_wakeup_scheduled = Mock()
    rt._cancel_deferred_message_turn_task = Mock()
    rt._collect_pending_messages = lambda: MaisakaHeartFlowChatting._collect_pending_messages(rt)
    rt._consume_pending_wait_state = lambda: MaisakaHeartFlowChatting._consume_pending_wait_state(rt)
    calls = []

    async def llm(history, **kwargs):
        calls.append(
            {
                "logical_turn_id": kwargs["logical_turn_id"],
                "messages": [m.message_id for m in rt.message_cache[: rt._last_processed_index]],
            }
        )
        if on_request is not None:
            await on_request(len(calls))
        await asyncio.sleep(0)
        return SimpleNamespace(tool_calls=[SimpleNamespace(func_name=name) for name in tools])

    rt._chat_loop_service = SimpleNamespace(chat_loop_step=llm, set_interrupt_flag=Mock())
    engine = MaisakaReasoningEngine(rt)
    engine._ingest_messages = AsyncMock()
    engine._clear_planner_no_tool_hints = Mock()
    engine._refresh_visual_placeholders_for_cycle = AsyncMock()
    engine._refresh_mid_term_memory_reference_for_continuation = AsyncMock()
    counter = 0

    async def start(index):
        nonlocal counter
        counter += 1
        replay.cycle(counter)
        return CycleDetail(cycle_id=counter), "mock round"

    async def request(**kwargs):
        engine._observation.request_batch(kwargs["source_messages"])
        kwargs["state"].response = await engine._run_interruptible_planner()

    async def actions(**kwargs):
        state = kwargs["state"]
        last = len(calls) >= cycles
        state.cycle_end = CycleEnd(
            "tool_pause:wait" if waiting else ("planner_no_tool_end" if last else "tool_continue"), "mock"
        )
        if waiting:
            rt._pending_wait_tool_call_id = "mock-wait-call"
            rt._pending_wait_logical_turn_id = engine.active_logical_turn_id
            rt._pending_wait_started_at = time.time()
            rt._pending_wait_seconds = 1.0
            rt._agent_state = "wait"
        return 0, last

    async def finalize(**kwargs):
        if len(calls) >= cycles:
            rt._running = False
        return kwargs["state"].cycle_end

    engine._start_cycle_round = start
    engine._run_planner_request = request
    engine._handle_planner_response_actions = actions
    engine._finalize_cycle = finalize
    return engine, calls


def events(instance, session="group-a", stage=None):
    assert instance.flush()
    saved = instance.query(session, 200)
    return [e for e in saved["execution_events"] if stage is None or e["stage"] == stage]


@pytest.mark.asyncio
async def test_gate_queue_consume_real_planner_and_replay_identity(monkeypatch, observed, tmp_path):
    enable(observed)
    monkeypatch.setattr(replay, "enabled", lambda: True)
    rt = runtime(messages=[message("a"), message("b")])
    engine, calls = harness(monkeypatch, rt, tools=("wait",), waiting=True)
    schedule(monkeypatch, rt)
    payload = rt._internal_turn_queue._queue[0]
    assert isinstance(payload, MessageTrigger) and payload == "message" and str(payload) == "message"
    receipt = payload.references[0]
    await asyncio.wait_for(engine.run_loop(), 1)
    saved = observed.query("group-a") if observed.flush() else None
    assert len(calls) == 1
    started = events(observed, stage="planner_started")
    assert len(started) == 1
    assert started[0]["evaluation_id"] == receipt.evaluation.evaluation_id
    assert started[0]["logical_turn_id"] == calls[0]["logical_turn_id"]
    assert started[0]["cycle_id"] == "1"
    assert started[0]["replay_status"] == "trace_bound_persistence_unknown"
    assert events(observed, stage="enqueued") and events(observed, stage="consumed")
    assert events(observed, stage="tool_selection")[0]["tool_names"] == ["wait"]
    assert events(observed, stage="execution_ended")[0]["reason"] == "tool_pause:wait"
    assert all(o["replyer_status"] == o["send_status"] == "not_observed" for o in saved["outcomes"])
    assert replay.store.flush()
    with sqlite3.connect(replay.store.path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE logical_turn_id=?", (calls[0]["logical_turn_id"],)
            ).fetchone()[0]
            > 0
        )
    (tmp_path / "phase2b1-example.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")


def test_reject_and_deferred_then_pass_preserve_evaluations(monkeypatch, observed):
    enable(observed)
    rt = runtime(messages=[message(mention=False, text="[图片]")])
    service = schedule(monkeypatch, rt)
    assert rt._internal_turn_queue.empty()
    assert not events(observed, stage="planner_started")
    service._dynamic_reply_gate.evaluate = Mock(return_value=TurnGateResult("delay", "mock", 1.0))
    service.schedule_message_turn()
    service._dynamic_reply_gate.evaluate = Mock(return_value=TurnGateResult("trigger", "mock"))
    service.schedule_message_turn()
    assert observed.flush()
    saved = observed.query("group-a")
    assert len(saved["opportunities"]) == 1
    assert [e["gate_result"] for e in saved["evaluations"]] == ["reject", "reject", "pass"]
    ref = rt._internal_turn_queue.get_nowait().references[0]
    assert ref.evaluation.evaluation_id == saved["evaluations"][-1]["evaluation_id"]
    assert events(observed, stage="enqueued")[0]["evaluation_id"] == ref.evaluation.evaluation_id


@pytest.mark.asyncio
async def test_quiet_batch_changes_do_not_rewrite_gate_snapshot(monkeypatch, observed):
    enable(observed)
    rt = runtime()

    async def quiet():
        rt.message_cache.append(message("during-quiet"))
        recorder.registered(rt)

    engine, calls = harness(monkeypatch, rt, quiet=quiet)
    schedule(monkeypatch, rt)
    await engine.run_loop()
    assert calls[0]["messages"] == ["m1", "during-quiet"]
    assert observed.flush()
    saved = observed.query("group-a")
    snapshot = saved["evaluations"][0]["feature_snapshot"]["message_event_ids"]
    assert snapshot == [identity("message", "group-a", "m1")]
    assert len(events(observed, stage="consumed")[0]["message_event_ids"]) == 2
    assert len(events(observed, stage="planner_started")) == 1


@pytest.mark.asyncio
async def test_legacy_merges_multiple_opportunities_and_cycles(monkeypatch, observed):
    enable(observed)
    rt = runtime()

    async def append_in_first_request(index):
        if index == 1:
            rt.message_cache.append(message("legacy-added"))
            recorder.registered(rt)

    engine, calls = harness(monkeypatch, rt, cycles=2, on_request=append_in_first_request)
    schedule(monkeypatch, rt)
    await engine.run_loop()
    starts = events(observed, stage="planner_started")
    assert len(calls) == 2
    assert len({e["logical_turn_id"] for e in starts}) == 1
    assert {e["cycle_id"] for e in starts} == {"1", "2"}
    assert len({e["opportunity_id"] for e in starts}) == 2
    added = [e for e in starts if e["evaluation_id"] is None]
    assert len(added) == 1 and added[0]["cycle_id"] == "2"


@pytest.mark.asyncio
async def test_wait_timeout_reuses_explicit_wait_lineage_without_new_message(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    engine, calls = harness(monkeypatch, rt, tools=("wait",), waiting=True)
    schedule(monkeypatch, rt)
    await engine.run_loop()
    original = events(observed, stage="planner_started")[0]
    rt._running = True
    rt._internal_turn_queue.put_nowait("timeout")
    await engine.run_loop()
    starts = events(observed, stage="planner_started")
    assert len(calls) == len(starts) == 2
    assert {e["logical_turn_id"] for e in starts} == {original["logical_turn_id"]}
    assert {e["opportunity_id"] for e in starts} == {original["opportunity_id"]}
    assert len({e["execution_id"] for e in starts}) == 2
    assert {e["cycle_id"] for e in starts} == {"1", "2"}
    resumed = [e for e in starts if e["link_source"] == "wait_continuation"]
    assert len(resumed) == 1
    assert resumed[0]["wait_tool_call_id"] == "mock-wait-call"
    assert resumed[0]["parent_execution_id"] == original["execution_id"]


@pytest.mark.asyncio
async def test_two_groups_concurrent_never_borrow_trace_or_receipts(monkeypatch, observed):
    enable(observed, "group-a", "group-b")
    monkeypatch.setattr(replay, "enabled", lambda: True)
    pairs = []
    for sid in ("group-a", "group-b"):
        rt = runtime(sid)
        engine, calls = harness(monkeypatch, rt, cycles=2)
        schedule(monkeypatch, rt)
        pairs.append((engine, calls))
    await asyncio.gather(*(engine.run_loop() for engine, _ in pairs))
    identities = []
    for sid, (_, calls) in zip(("group-a", "group-b"), pairs, strict=True):
        starts = events(observed, sid, "planner_started")
        assert len(starts) == len(calls) == 2
        assert all(e["replay_status"] == "trace_bound_persistence_unknown" for e in starts)
        identities.append({e["logical_turn_id"] for e in starts})
    assert not identities[0] & identities[1]
    assert replay.current_trace.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,broken", [(False, False), (True, False), (True, True)])
async def test_observation_disabled_or_writer_failure_same_production(monkeypatch, observed, enabled, broken):
    if enabled:
        enable(observed)
    if broken:
        monkeypatch.setattr(observed, "_write", Mock(side_effect=sqlite3.OperationalError("injected")))
    rt = runtime()
    engine, calls = harness(monkeypatch, rt, cycles=2, tools=("search",))
    service = schedule(monkeypatch, rt)
    await engine.run_loop()
    assert len(calls) == 2
    assert calls[0]["messages"] == calls[1]["messages"] == ["m1"]
    assert rt._last_processed_index == 1 and rt._internal_turn_queue.empty()
    assert service._dynamic_reply_gate.evaluate.call_count == 1
    assert observed.flush()
    if not enabled:
        assert not observed.path.exists()
    elif broken:
        assert observed.write_errors > 0
    else:
        assert len(events(observed, stage="planner_started")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [RuntimeError("mock model failure"), asyncio.CancelledError()])
async def test_model_error_or_cancel_never_invents_wait(monkeypatch, observed, exception):
    enable(observed)
    rt = runtime()

    async def fail(index):
        raise exception

    engine, calls = harness(monkeypatch, rt, on_request=fail)
    schedule(monkeypatch, rt)
    with pytest.raises(type(exception)):
        await engine.run_loop()
    assert len(events(observed, stage="planner_started")) == 1
    assert not events(observed, stage="tool_selection")
    assert not any("wait" in e["reason"] for e in events(observed))


@pytest.mark.asyncio
async def test_consumed_but_preparation_fails_not_planner_started(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    engine, calls = harness(monkeypatch, rt)
    engine._ingest_messages = AsyncMock(side_effect=ValueError("mock ingest"))
    schedule(monkeypatch, rt)
    with pytest.raises(ValueError):
        await engine.run_loop()
    assert events(observed, stage="consumed")
    assert not events(observed, stage="planner_started") and not calls
    assert events(observed, stage="execution_interrupted")[0]["status"] == "unknown"


def test_wrong_group_revoked_and_missing_wait_do_not_fabricate_links(monkeypatch, observed):
    enable(observed, "group-a", "group-b")
    ref = recorder.evaluate(runtime(), [message()], "pass", "mock")
    tracker = ExecutionTracker()
    tracker.reset(MessageTrigger((ref,)))
    tracker.consumed(recorder.consume(runtime(), [message()]), SimpleNamespace(session_id="group-a"))
    tracker.bind_cycle("1")
    recorder.execution((ref,), "planner_started", "started", "mock", session_id="group-b")
    assert not events(observed, "group-b")
    assert observed.errors > 0
    tracker.wait_binding = ("call", "turn", (ref,))
    tracker.reset("timeout")
    tracker.resume_wait("different-call", "turn")
    assert not tracker.references
    observed.configure(Policy())
    recorder.execution((ref,), "planner_started", "started", "mock", session_id="group-a")
    enable(observed, "group-a", "group-b")
    recorder.execution((ref,), "planner_started", "started", "mock", session_id="group-a")
    assert not events(observed, stage="planner_started")


def test_schema_one_migrates_additively_and_read_only_query_does_not_migrate(tmp_path, monkeypatch):
    path = tmp_path / "v1.db"
    instance = ObservationStore(path)
    monkeypatch.setattr(recorder, "store", instance)
    enable(instance)
    recorder.evaluate(runtime(), [message()], "reject", "historical_snapshot")
    assert instance.close()
    before = instance.query("group-a")["evaluations"]
    assert len(before) == 1
    connection = instance._connect()
    connection.execute("DROP TABLE delivery_links")
    connection.execute("DROP TABLE delivery_receipts")
    connection.execute("DROP TABLE delivery_scopes")
    connection.execute("DROP TABLE reply_events")
    connection.execute("DROP TABLE priority_claim_checks")
    connection.execute("DROP TABLE opportunity_transfers")
    connection.execute("ALTER TABLE opportunities DROP COLUMN account_id")
    connection.execute("DROP TABLE execution_events")
    connection.execute("DROP TABLE annotation_revisions")
    connection.execute("DROP TABLE annotation_targets")
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()
    assert instance.query("group-a")["execution_events"] == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    connection = instance._connect()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    connection.close()
    assert instance.query("group-a")["evaluations"] == before
    assert instance.close()


def test_execution_adapter_benchmark(monkeypatch, observed):
    enable(observed, capacity=65536)
    ref = recorder.evaluate(runtime(), [message()], "pass", "mock")
    durations = []
    for _ in range(500):
        started = time.perf_counter_ns()
        recorder.execution(
            (ref,),
            "planner_started",
            "started",
            "mock",
            session_id="group-a",
            execution_id="mock",
            logical_turn_id="mock-turn",
            cycle_id="1",
        )
        durations.append((time.perf_counter_ns() - started) / 1000)
    assert observed.flush(10)
    durations.sort()
    print("OBS_EXECUTION_US", {"p50": durations[249], "p95": durations[474], "p99": durations[494]})


@pytest.mark.asyncio
async def test_delayed_recheck_starts_only_with_passing_evaluation(monkeypatch, observed):
    enable(observed)
    rt = runtime(messages=[message(mention=False, text="[图片]")])
    engine, calls = harness(monkeypatch, rt)
    service = schedule(monkeypatch, rt)
    service._dynamic_reply_gate.evaluate = Mock(return_value=TurnGateResult("delay", "mock", 0.1))
    service.schedule_message_turn()
    service._dynamic_reply_gate.evaluate = Mock(return_value=TurnGateResult("trigger", "mock"))
    service.schedule_message_turn()
    await engine.run_loop()
    saved = observed.query("group-a") if observed.flush() else None
    assert len(calls) == 1
    assert events(observed, stage="planner_started")[0]["evaluation_id"] == saved["evaluations"][-1]["evaluation_id"]
    assert len(saved["evaluations"]) == 3


@pytest.mark.asyncio
async def test_wait_new_pending_links_old_and_new_opportunities(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    engine, calls = harness(monkeypatch, rt, tools=("wait",), waiting=True)
    schedule(monkeypatch, rt)
    await engine.run_loop()
    rt.message_cache.append(message("while-waiting"))
    recorder.registered(rt)
    rt._running = True
    rt._internal_turn_queue.put_nowait("timeout")
    await engine.run_loop()
    starts = events(observed, stage="planner_started")
    assert len(calls) == 2
    assert len(starts) == 3
    assert len({e["opportunity_id"] for e in starts}) == 2
    assert len({e["logical_turn_id"] for e in starts}) == 1
    assert [e for e in starts if e["evaluation_id"] is None][0]["cycle_id"] == "2"


@pytest.mark.asyncio
async def test_drained_queue_receipts_and_silent_consumption_do_not_fabricate_execution(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    engine, calls = harness(monkeypatch, rt)
    schedule(monkeypatch, rt)
    stale = rt._internal_turn_queue.get_nowait()
    # A previous consumer used these messages; a later timeout has no wait lineage.
    rt._collect_pending_messages()
    rt._internal_turn_queue.put_nowait(stale)
    rt._internal_turn_queue.put_nowait("timeout")
    context = await engine._prepare_turn_start_context(rt._internal_turn_queue.get_nowait())
    assert context.trigger_message is None
    assert rt._internal_turn_queue.empty() and not calls
    engine._observation.bind_cycle("unexpected")
    engine._observation.record(rt, "planner_started", "started", "mock")
    assert not events(observed, stage="planner_started")
    engine._observation.missing_start(rt, "prepare_skipped")
    assert any(e["reason"] == "queued_batch_not_consumed" for e in events(observed, stage="not_started"))
    recorder.discarded(rt, stale)
    assert any(e["reason"] == "runtime_stopped_before_dequeue" for e in events(observed, stage="not_started"))


@pytest.mark.asyncio
async def test_enabled_disabled_identical_production_calls_and_cursor(monkeypatch, observed, tmp_path):
    results = []
    for enabled in (False, True):
        from src.services import priority_at_reply_service
        from src.services.message_processing_rights import ProcessingRights

        monkeypatch.setattr(priority_at_reply_service.priority_at_reply_service, "_rights", ProcessingRights())
        observed.configure(Policy(enabled, frozenset({"group-a"}), 30, 128))
        rt = runtime(messages=[message("a"), message("b")])
        engine, calls = harness(monkeypatch, rt, cycles=2, tools=("wait",))
        service = schedule(monkeypatch, rt)
        await engine.run_loop()
        results.append(
            {
                "planner_calls": len(calls),
                "source_batches": [c["messages"] for c in calls],
                "cursor": rt._last_processed_index,
                "state": rt._agent_state,
                "queued": rt._internal_turn_queue.qsize(),
                "gate_calls": service._dynamic_reply_gate.evaluate.call_count,
                "gate_inputs": [str(i) for i in service.observed_production_inputs],
            }
        )
    assert results[0] == results[1]
    (tmp_path / "phase2b1-behavior-parity.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_forced_path_remains_out_of_scope(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    rt._has_forced_turn_trigger = lambda: True
    engine, calls = harness(monkeypatch, rt)
    schedule(monkeypatch, rt)
    await engine.run_loop()
    assert len(calls) == 1
    assert events(observed, stage="consumed")
    assert not events(observed, stage="planner_started")


@pytest.mark.asyncio
async def test_silent_consumption_and_focus_revocation_never_start_planner(monkeypatch, observed):
    from src.maisaka.turn_trigger import scheduler

    enable(observed)
    rt = runtime()
    engine, calls = harness(monkeypatch, rt)
    schedule(monkeypatch, rt)
    rt._is_reply_frequency_silent = lambda: True

    async def silent(**kwargs):
        rt._running = False

    engine._handle_silent_turn = silent
    await engine.run_loop()
    assert not calls and not events(observed, stage="planner_started")
    assert events(observed, stage="consumed")
    assert any(e["reason"] == "silent_consumption" for e in events(observed, stage="not_started"))
    rt = runtime()
    engine, calls = harness(monkeypatch, rt)
    schedule(monkeypatch, rt)
    payload = rt._internal_turn_queue.get_nowait()
    monkeypatch.setattr(scheduler.focus_mode_manager, "can_decide", lambda *a, **k: False)
    engine._observation.reset(payload)
    engine._observation.record(rt, "not_started", "not_observed", "focus_unavailable")
    assert not calls and not events(observed, stage="planner_started")


@pytest.mark.asyncio
async def test_interruption_consumed_new_batch_only_runs_in_next_cycle(monkeypatch, observed):
    from src.llm_models.exceptions import ReqAbortException

    enable(observed)
    rt = runtime()

    async def interrupt(index):
        if index == 1:
            rt.message_cache.append(message("interrupting"))
            recorder.registered(rt)
            raise ReqAbortException("mock interruption")

    engine, calls = harness(monkeypatch, rt, cycles=2, on_request=interrupt)
    schedule(monkeypatch, rt)
    await engine.run_loop()
    starts = events(observed, stage="planner_started")
    assert len(calls) == 2
    new_starts = [e for e in starts if e["evaluation_id"] is None]
    assert len(new_starts) == 1 and new_starts[0]["cycle_id"] == "2"
    assert all(e["cycle_id"] == "2" for e in events(observed, stage="tool_selection"))
    assert any(e["status"] == "interrupted" for e in events(observed, stage="cycle_ended"))


def test_repeated_evaluation_dispatches_keep_both_receipts(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    engine, _ = harness(monkeypatch, rt)
    refs = tuple(recorder.evaluate(rt, rt.message_cache, "pass", "mock") for _ in range(2))
    first = MessageTrigger((refs[0],))
    rt._internal_turn_queue.put_nowait(MessageTrigger((refs[1],)))
    engine._observation.reset(first)
    assert engine._drain_ready_turn_triggers(first) == (True, False, False)
    engine._observation.consumed(rt._collect_pending_messages(), rt)
    engine._observation.logical_turn_id = "mock-turn"
    engine._observation.bind_cycle("1")
    engine._observation.record(rt, "planner_started", "started", "mock")
    starts = events(observed, stage="planner_started")
    assert {e["evaluation_id"] for e in starts} == {ref.evaluation.evaluation_id for ref in refs}
    assert len({e["opportunity_id"] for e in starts}) == 1


@pytest.mark.asyncio
async def test_lost_gate_writes_recover_receipt_without_inventing_enqueue(monkeypatch, observed):
    enable(observed)
    rt = runtime()
    engine, calls = harness(monkeypatch, rt)
    submit = observed.submit
    monkeypatch.setattr(observed, "submit", lambda batch: False)
    schedule(monkeypatch, rt)
    ref = rt._internal_turn_queue._queue[0].references[0]
    monkeypatch.setattr(observed, "submit", submit)
    await engine.run_loop()
    starts = events(observed, stage="planner_started")
    assert len(calls) == len(starts) == 1
    assert starts[0]["evaluation_id"] == ref.evaluation.evaluation_id
    assert not events(observed, stage="enqueued")
    assert observed.query("group-a")["evaluations"][0]["evaluation_id"] == ref.evaluation.evaluation_id


def test_foreign_contextvar_cannot_replace_engine_identity(monkeypatch, observed):
    enable(observed)
    monkeypatch.setattr(replay, "enabled", lambda: True)
    rt = runtime()
    engine, _ = harness(monkeypatch, rt)
    ref = recorder.evaluate(rt, rt.message_cache, "pass", "mock")
    engine._observation.reset(MessageTrigger((ref,)))
    engine._observation.consumed(rt._collect_pending_messages(), rt)
    engine._observation.logical_turn_id = "engine-owned"
    engine._observation.bind_cycle("1")
    with replay.bind(replay.new_trace("group-b", turn_id="foreign")):
        engine._observation.record(rt, "planner_started", "started", "mock")
    started = events(observed, stage="planner_started")[0]
    assert started["logical_turn_id"] == "engine-owned"
    assert started["replay_status"] == "not_observed"
