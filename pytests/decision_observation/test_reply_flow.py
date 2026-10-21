"""Mock-only generation/driver boundaries, identity isolation and behavior comparisons."""

from types import SimpleNamespace
from threading import Event
from unittest.mock import AsyncMock, Mock
import asyncio
import json
import sqlite3
import time

import pytest

from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator, MaisakaReplyContext
from src.common.data_models.llm_service_data_models import LLMResponseResult
from src.common.data_models.message_component_data_model import EmojiComponent, MessageSequence, TextComponent
from src.common.data_models.reply_generation_data_models import ReplyGenerationResult
from src.core.tooling import ToolInvocation
from src.decision_observation import recorder, reply_flow
from src.decision_observation.execution import ExecutionTracker
from src.decision_observation.models import Policy, SCHEMA_VERSION
from src.decision_replay import recorder as replay, replies
from src.decision_replay.reply_metrics import statistics
from src.decision_replay.store import ReplayStore
from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
from src.maisaka.builtin_tool import reply as reply_tool
from src.maisaka.builtin_tool.context import PostProcessedReplyMessage
from src.maisaka.reasoning_engine import CycleRuntimeState, MaisakaReasoningEngine
from src.platform_io.manager import PlatformIOManager
from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverKind, RouteKey
from src.services import send_service
from pytests.maisaka.test_reply_attachments import reply_context as _reply_context
from test_priority_execution import enable, env as _env, read

reply_context = _reply_context
env = _env


def rows(env, stage=None, sid="group-a"):
    return [r for r in read(env, sid)["reply_events"] if stage is None or r["stage"] == stage]


def tracker_for(env, rt=None, mid="m1"):
    rt = rt or env.chats["group-a"]
    msg = env.message(mid, rt.session_id)
    recorder.pending(rt, [msg])
    ref = recorder.evaluate(rt, [msg], "pass", "mock_gate")
    tracker = ExecutionTracker()
    tracker.execution_id = "execution:" + rt.session_id + ":" + mid
    tracker.logical_turn_id = "turn:" + rt.session_id
    tracker.cycle_id = "1"
    tracker.cycle_references = (ref,) if ref is not None else ()
    return tracker


def generator(monkeypatch, sid="group-a", outputs=("synthetic output",), *, failure=None):
    monkeypatch.setattr("src.services.tag_memory_recall.build_tag_memory_reference", AsyncMock(return_value=""))
    gen = object.__new__(BaseMaisakaReplyGenerator)
    gen._build_reply_context = AsyncMock(return_value=MaisakaReplyContext())
    gen._resolve_session_id = lambda stream: sid
    gen._build_request_messages = Mock(return_value=[])
    gen._persist_reply_preview = Mock()
    hook = SimpleNamespace(invoke_hook=AsyncMock(return_value=SimpleNamespace(kwargs={})))
    gen._get_runtime_manager = lambda: hook
    calls = []

    async def model(*args, **kwargs):
        index = len(calls)
        calls.append((args, kwargs))
        request = SimpleNamespace(trace_context=SimpleNamespace(
            request_type="maisaka.replyer", request_id=sid + ":request", attempt=index + 1, session_id=sid))
        reply_flow.request_started(request, "replyer", sid)
        await replies.generation_request(request, "replyer", sid)
        if failure is not None:
            raise failure
        text = outputs[min(index, len(outputs) - 1)]
        item = ContextItemBuilder().set_role(RoleType.Assistant).add_text_content(text).build()
        return LLMResponseResult(output_items=(item,), model_name="mock")

    gen.express_model = SimpleNamespace(task_name="replyer", generate_response_with_context=AsyncMock(side_effect=model))
    gen.request_type = "maisaka.replyer"
    gen.observed_parameters = []
    original = gen.generate_reply_with_context

    async def generate(**kwargs):
        gen.observed_parameters.append(dict(
            keys=sorted(kwargs), stream_id=kwargs.get("stream_id"),
            target_id=kwargs["reply_message"].message_id if kwargs.get("reply_message") else None,
            history_count=len(kwargs.get("chat_history", [])), reply_tool_args=kwargs.get("reply_tool_args"),
            log_reply=kwargs.get("log_reply")))
        return await original(**kwargs)

    gen.generate_reply_with_context = generate
    return gen, calls


def outbound(oid, sid="group-a", kind="text"):
    parts = [TextComponent("private generated body")] if kind == "text" else [
        EmojiComponent(binary_hash="synthetic", binary_data=b"x")]
    return SimpleNamespace(message_id=oid, session_id=sid, platform="qq", is_at=False,
                           message_info=SimpleNamespace(additional_config={}, group_info=SimpleNamespace(group_id="g")),
                           raw_message=MessageSequence(parts))


def pipeline(monkeypatch, *, behavior="returned", drivers=1):
    manager = PlatformIOManager()
    calls = []

    async def dispatch(*, message, route_key, metadata):
        calls.append((message.session_id, message.message_id, replies.classify(message), dict(metadata or {})))
        if behavior == "cancel":
            raise asyncio.CancelledError()
        if behavior == "exception":
            raise RuntimeError("private error body")
        if behavior == "timeout":
            raise TimeoutError("private timeout")
        return DeliveryReceipt(message.message_id, route_key, DeliveryStatus.UNKNOWN,
                               driver_id="mock", driver_kind=DriverKind.PLUGIN)

    instances = [SimpleNamespace(driver_id="mock" + str(i), descriptor=SimpleNamespace(kind=DriverKind.PLUGIN),
                                 send_message=AsyncMock(side_effect=dispatch)) for i in range(drivers)]
    monkeypatch.setattr(manager, "resolve_drivers", lambda route: instances)

    async def via(message, **kwargs):
        await manager.send_message(message, RouteKey("qq", "bot"), metadata={"typing": kwargs["typing"]})
        return message  # Deliberately returns a message even with UNKNOWN: never infer platform success.

    monkeypatch.setattr(send_service, "_send_via_platform_io", via)
    sent = []

    async def send(**kwargs):
        msg = outbound(kwargs["outbound_message_id"], kwargs["stream_id"])
        msg.raw_message = kwargs["message_sequence"]
        sent.append((kwargs["stream_id"], replies.classify(msg), kwargs["set_reply"], kwargs["typing"]))
        return await send_service.send_session_message_with_message(msg, typing=kwargs["typing"])

    monkeypatch.setattr(send_service, "_send_to_target_with_message", send)
    return manager, calls, sent


def ordinary(env, reply_context, monkeypatch, gen, segments=2):
    rt = reply_context.runtime
    rt.session_id = "group-a"
    rt.chat_stream.account_id = "bot"
    rt.chat_stream.is_group_session = True
    rt.chat_stream.group_id = "g"
    rt.chat_stream.user_id = None
    rt.record_planner_reply = Mock()
    rt.find_source_message_by_id("msg-1").session_id = "group-a"
    monkeypatch.setattr(reply_tool.replyer_manager, "get_replyer", lambda **kwargs: gen)
    monkeypatch.setattr(reply_tool, "_invoke_before_post_process_hook", AsyncMock(return_value=("synthetic", {})))
    monkeypatch.setattr(reply_context, "post_process_reply_message_items_async", AsyncMock(return_value=[
        PostProcessedReplyMessage(MessageSequence([TextComponent("bubble " + str(i))])) for i in range(segments)]))
    return rt


@pytest.mark.asyncio
async def test_real_reply_tool_generator_multiple_bubbles_and_attachment(env, reply_context, monkeypatch):
    enable(env)
    gen, model_calls = generator(monkeypatch)
    rt = ordinary(env, reply_context, monkeypatch, gen)
    tracker = tracker_for(env)
    manager, driver_calls, sends = pipeline(monkeypatch)

    async def attach(sid, tag, content):
        if len(driver_calls) == 1:
            await send_service.send_session_message_with_message(outbound("attachment", sid, "emoji"))

    monkeypatch.setattr("src.emoji_system.reply_attachment.send_after_text", attach)
    invocation = ToolInvocation("reply", call_id="call-1", arguments={"msg_id": "msg-1"})
    with reply_flow.planner_scope(tracker, rt, invocation):
        result = await reply_tool.handle_tool(reply_context, invocation)
    assert result.success and len(model_calls) == 1 and len(driver_calls) == 3 and len(sends) == 2
    events = rows(env)
    assert [r["status"] for r in events if r["stage"] == "generation"] == ["generation_started", "generation_completed"]
    assert rows(env, "reply_output")[0]["detail"]["segment_count"] == 2
    assert len(rows(env, "driver_send")) == 3
    assert len({r["attempt_id"] for r in rows(env, "driver_send")}) == 3
    assert len({r["reply_id"] for r in events if r["reply_id"]}) == 1
    assert all(r["generation_group_id"] is None for r in events)  # Replay disabled, no invented IDs.
    assert all(r["execution_id"] == tracker.execution_id and r["tool_call_id"] == "call-1" for r in events)
    assert all(r["detail"]["platform_confirmation"] == "not_observed" for r in events)
    payload = json.dumps(events)
    assert "private generated body" not in payload and "synthetic output" not in payload
    (env.root / "reply-complete-example.json").write_text(json.dumps(read(env), indent=2), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "empty", "plugin_abort", "cancel", "retry"])
async def test_real_generation_failure_empty_cancel_and_regeneration(env, reply_context, monkeypatch, mode):
    enable(env)
    gen, calls = generator(monkeypatch, outputs=("[发言内容]invalid", "clean") if mode == "retry" else ("",)
                           if mode == "empty" else ("clean",),
                           failure=asyncio.CancelledError() if mode == "cancel" else RuntimeError("private")
                           if mode == "failure" else None)
    rt = ordinary(env, reply_context, monkeypatch, gen)
    _, dispatches, _ = pipeline(monkeypatch)
    if mode == "plugin_abort":
        monkeypatch.setattr(reply_tool, "_invoke_before_post_process_hook", AsyncMock(return_value=("", {})))
    tracker = tracker_for(env)
    call = ToolInvocation("reply", arguments={"msg_id": "msg-1"})
    with reply_flow.planner_scope(tracker, rt, call):
        if mode == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await reply_tool.handle_tool(reply_context, call)
        else:
            result = await reply_tool.handle_tool(reply_context, call)
            assert result.success == (mode == "retry")
    assert len(rows(env, "generation_request")) == len(calls)
    assert len(calls) == (2 if mode == "retry" else 1)
    assert bool(dispatches) == (mode == "retry")
    last = rows(env, "generation")[-1]
    assert last["status"] == ("interrupted" if mode == "cancel" else "generation_failed"
                              if mode in {"failure", "empty"} else "generation_completed")


@pytest.mark.asyncio
async def test_generation_success_without_valid_output_and_wait_no_observations(env, monkeypatch):
    enable(env)
    tracker = tracker_for(env)
    rt = env.chats["group-a"]
    with reply_flow.planner_scope(tracker, rt, ToolInvocation("wait")):
        reply_flow.event(rt.session_id, "generation", "generation_started")
    assert not rows(env)

    @reply_flow.observe_generation
    async def empty(**kwargs):
        return True, ReplyGenerationResult(success=True)

    with reply_flow.planner_scope(tracker, rt, ToolInvocation("reply")):
        await empty(stream_id=rt.session_id)
    last = rows(env, "generation")[-1]
    assert last["status"] == "generation_completed" and last["detail"]["valid_output"] is False
    assert not rows(env, "driver_send")


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["returned", "exception", "timeout", "cancel"])
async def test_actual_driver_attempts_keep_unknown_and_never_retry(env, monkeypatch, behavior):
    enable(env)
    tracker = tracker_for(env)
    manager, calls, _ = pipeline(monkeypatch, behavior=behavior)
    with reply_flow.planner_scope(tracker, env.chats["group-a"], ToolInvocation("reply")):
        if behavior == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await manager.send_message(outbound("out"), RouteKey("qq", "bot"))
        else:
            batch = await manager.send_message(outbound("out"), RouteKey("qq", "bot"))
            assert batch.receipts[0].status == DeliveryStatus.UNKNOWN
    assert len(calls) == len(rows(env, "driver_send")) == 1
    end = rows(env, "driver_return")[0]
    assert end["status"] == ("send_returned" if behavior == "returned" else "interrupted"
                             if behavior == "cancel" else "unknown")
    assert end["detail"]["platform_confirmation"] == "not_observed"
    assert end["detail"]["driver_reported_status"] == ("unknown" if behavior == "returned" else None)


@pytest.mark.asyncio
async def test_priority_actual_text_fixed_and_original_text_fallback(env, monkeypatch):
    enable(env)
    gen, calls = generator(monkeypatch)
    monkeypatch.setattr("src.chat.replyer.maisaka_generator.MaisakaReplyGenerator", lambda **kwargs: gen)
    _, dispatches, _ = pipeline(monkeypatch)
    ordinal = 0

    async def priority(sequence, message, **kwargs):
        nonlocal ordinal
        ordinal += 1
        msg = outbound("priority:" + str(ordinal))
        msg.raw_message = sequence
        if message.message_id == "fallback" and ordinal == 3:
            raise RuntimeError("existing send failure")
        return await send_service.send_session_message_with_message(msg) is not None

    monkeypatch.setattr(send_service, "send_priority_at_reply", priority)
    await env.receiver.process_message(env.message("text"))
    await env.drain()
    monkeypatch.setattr("src.services.priority_at_reply_service.random.random", lambda: 0.0)
    await env.receiver.process_message(env.message("emoji", text=""))
    await env.drain()
    await env.receiver.process_message(env.message("fallback", text=""))
    await env.drain()
    assert len(calls) == 2 and len(dispatches) == 3
    assert len(rows(env, "direct_emoji")) == 2
    assert all(r["logical_turn_id"] is None for r in rows(env))
    emoji_oid = next(o["opportunity_id"] for o in read(env)["opportunities"]
                     if any(m["platform_message_id"] == "emoji" and m["message_event_id"] in o["message_event_ids"]
                            for m in read(env)["messages"]))
    assert not [r for r in rows(env, "generation") if r["opportunity_id"] == emoji_oid]


@pytest.mark.asyncio
async def test_multigroup_multireference_and_inherited_context_rejected(env, monkeypatch):
    enable(env, "group-a", "group-b")
    env.runtime("group-b", "bot-two")
    a, b = tracker_for(env), tracker_for(env, env.chats["group-b"])
    second = tracker_for(env, mid="m2").cycle_references[0]
    a.cycle_references += (second,)

    async def run(tracker, sid):
        with reply_flow.planner_scope(tracker, env.chats[sid], ToolInvocation("reply", call_id=sid)):
            reply_flow.event(sid, "test_entry", "observed", key="same")
            await asyncio.create_task(async_probe(sid))
            await asyncio.to_thread(reply_flow.event, sid, "thread_leak", "observed")
            callback_done = asyncio.get_running_loop().create_future()

            def callback():
                reply_flow.event(sid, "callback_leak", "observed")
                callback_done.set_result(None)

            asyncio.get_running_loop().call_soon(callback)
            await callback_done
            reply_flow.event("group-b" if sid == "group-a" else "group-a", "cross_leak", "observed")
            await asyncio.sleep(0)
            reply_flow.event(sid, "test_end", "observed")

    async def async_probe(sid):
        reply_flow.event(sid, "child_leak", "observed")

    await asyncio.gather(run(a, "group-a"), run(b, "group-b"))
    saved_a, saved_b = rows(env), rows(env, sid="group-b")
    assert len(saved_a) == 4 and len(saved_b) == 2
    assert len({r["occurrence_id"] for r in saved_a}) == 2
    assert {r["execution_id"] for r in saved_a} == {a.execution_id}
    assert {r["execution_id"] for r in saved_b} == {b.execution_id}
    with reply_flow.planner_scope(b, env.chats["group-b"], ToolInvocation("reply")):
        reply_flow.driver_event(outbound("wrong-account", "group-b"), RouteKey("qq", "bot"),
                                "bad", "driver_send", "send_attempted", "mock")
    assert not rows(env, "driver_send", "group-b")


@pytest.mark.asyncio
async def test_schema_three_migration_idempotency_and_fk_integrity(env, monkeypatch):
    enable(env)
    tracker = tracker_for(env)
    assert env.store.flush()
    with sqlite3.connect(env.store.path) as c:
        before = c.execute("SELECT * FROM evaluations").fetchall()
        c.execute("DROP TABLE delivery_links")
        c.execute("DROP TABLE delivery_receipts")
        c.execute("DROP TABLE delivery_scopes")
        c.execute("DROP TABLE reply_events")
        c.execute("DROP TABLE annotation_revisions")
        c.execute("DROP TABLE annotation_targets")
        c.execute("PRAGMA user_version=3")
    assert read(env)["reply_events"] == []  # read-only query must not migrate.
    with reply_flow.planner_scope(tracker, env.chats["group-a"], ToolInvocation("reply")):
        reply_flow.event("group-a", "duplicate", "observed", key="stable")
        reply_flow.event("group-a", "duplicate", "observed", key="stable")
    assert len(rows(env)) == 1
    with sqlite3.connect(env.store.path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert c.execute("SELECT * FROM evaluations").fetchall() == before
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    await asyncio.to_thread(env.store.delete_session, "group-a")
    assert rows(env) == []
    with reply_flow.planner_scope(tracker, env.chats["group-a"], ToolInvocation("reply")):
        reply_flow.event("group-a", "after_deletion", "observed")
    assert rows(env) == []


@pytest.mark.asyncio
async def test_replay_identities_and_disabled_enabled_fault_behavior_match(env, reply_context, monkeypatch):
    snapshots = []
    for mode in ("off", "on", "fault"):
        env.store.configure(Policy(mode != "off", frozenset({"group-a"})))
        replay_store = ReplayStore(env.root / (mode + "-replay.db"))
        monkeypatch.setattr(replay, "store", replay_store)
        monkeypatch.setattr(replay, "enabled", lambda: True)
        gen, requests = generator(monkeypatch)
        rt = ordinary(env, reply_context, monkeypatch, gen)
        tracker = tracker_for(env)
        _, dispatches, sends = pipeline(monkeypatch)
        original = env.store._write
        if mode == "fault":
            monkeypatch.setattr(env.store, "_write", Mock(side_effect=sqlite3.OperationalError("injected")))
        trace = replay.new_trace("group-a", "planner", turn_id=tracker.logical_turn_id)
        with replay.bind(trace):
            replay.cycle(1)
            with reply_flow.planner_scope(tracker, rt, ToolInvocation("reply", call_id="same")):
                result = await reply_tool.handle_tool(reply_context, ToolInvocation("reply", call_id="same",
                                                     arguments={"msg_id": "msg-1"}))
        assert replay_store.flush() and env.store.flush()
        snapshots.append(dict(mode=mode, model_requests=len(requests), success=result.success, sends=sends,
                              replyer_parameters=gen.observed_parameters,
                              planner_reply_recorded=rt.record_planner_reply.call_count,
                              history_count=len(rt._chat_history),
                              driver_calls=[(sid, kind, meta) for sid, oid, kind, meta in dispatches],
                              statistics=statistics(replay_store)["counts"]))
        if mode == "on":
            assert rows(env, "generation")[-1]["generation_group_id"] is not None
            assert rows(env, "driver_send")[-1]["generation_group_id"] is not None
            (env.root / "reply-replay-example.json").write_text(json.dumps(read(env), indent=2), encoding="utf-8")
        if mode == "fault":
            assert env.store.write_errors > 0
        monkeypatch.setattr(env.store, "_write", original)
    assert len({json.dumps({k: v for k, v in row.items() if k != "mode"}, sort_keys=True)
                for row in snapshots}) == 1
    (env.root / "reply-behavior-parity.json").write_text(json.dumps(snapshots, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_actual_engine_dispatch_two_cycles_and_no_context_leftover(env, reply_context, monkeypatch):
    enable(env)
    gen, requests = generator(monkeypatch)
    rt = ordinary(env, reply_context, monkeypatch, gen, segments=1)
    _, dispatches, _ = pipeline(monkeypatch, drivers=2)
    rt._tool_registry = SimpleNamespace(list_tools=AsyncMock(return_value=[]),
        invoke=lambda invocation, context: reply_tool.handle_tool(reply_context, invocation, context))
    rt.is_action_tool_currently_available = lambda name: True
    rt._reset_consecutive_wait_count = Mock()
    engine = MaisakaReasoningEngine(rt)
    engine._observation = tracker_for(env)
    engine._build_tool_execution_context = Mock(return_value=None)
    engine._build_tool_availability_context = Mock(return_value=None)
    engine._log_tool_call_source = Mock()
    engine._build_tool_invocation = lambda call, thought: ToolInvocation(
        call.func_name, call_id=call.call_id, arguments={"msg_id": "msg-1"})
    engine._record_tool_execution_effects = AsyncMock()
    engine._append_tool_execution_result = Mock()
    engine._append_tool_display_results = AsyncMock()
    engine._emit_planner_progress = AsyncMock()
    engine._append_tool_post_history_messages = Mock()
    for cycle in ("1", "2"):
        engine._observation.cycle_id = cycle
        await engine._handle_tool_calls([SimpleNamespace(func_name="reply", call_id="tool-" + cycle)], "",
                                       SimpleNamespace(), CycleRuntimeState())
    assert len(requests) == 2 and len(dispatches) == 4
    assert {r["cycle_id"] for r in rows(env, "driver_send")} == {"1", "2"}
    assert {r["tool_call_id"] for r in rows(env, "generation")} == {"tool-1", "tool-2"}
    assert len(rows(env, "reply_selected")) == 2
    reply_flow.event("group-a", "leftover", "observed")
    assert not rows(env, "leftover")


@pytest.mark.asyncio
async def test_redelivery_has_single_real_generation_and_send_owner(env, reply_context, monkeypatch):
    from test_execution import harness
    from test_observation import schedule

    enable(env)
    gen, model_calls = generator(monkeypatch)
    monkeypatch.setattr("src.chat.replyer.maisaka_generator.MaisakaReplyGenerator", lambda **kwargs: gen)
    _, dispatches, _ = pipeline(monkeypatch)
    counter = 0

    async def priority(sequence, message, **kwargs):
        nonlocal counter
        counter += 1
        msg = outbound("priority:" + message.message_id + ":" + str(counter))
        msg.raw_message = sequence
        return await send_service.send_session_message_with_message(msg) is not None

    monkeypatch.setattr(send_service, "send_priority_at_reply", priority)
    env.service._max_concurrent = env.service._max_pending = env.service._max_pending_per_session = 1
    await env.receiver.process_message(env.message("active"))
    await env.receiver.process_message(env.message("queued"))
    first = env.message("redelivered")
    await env.receiver.process_message(first)
    assert env.chats["group-a"].message_cache == [first]
    await env.drain()
    await env.receiver.process_message(env.message("redelivered"))
    await env.drain()
    rt = ordinary(env, reply_context, monkeypatch, gen, segments=1)
    rt.find_source_message_by_id = lambda mid: first if mid == "redelivered" else None
    actual_runtime = env.chats["group-a"]
    engine, planner_calls = harness(monkeypatch, actual_runtime, tools=("reply",))
    original_actions = engine._handle_planner_response_actions

    async def actions(**kwargs):
        invocation = ToolInvocation("reply", call_id="normal-redelivery", arguments={"msg_id": "redelivered"})
        with reply_flow.planner_scope(engine._observation, rt, invocation):
            assert (await reply_tool.handle_tool(reply_context, invocation)).success
        return await original_actions(**kwargs)

    engine._handle_planner_response_actions = actions
    actual_runtime._has_forced_turn_trigger = lambda: True
    schedule(monkeypatch, actual_runtime)
    await engine.run_loop()
    assert len(planner_calls) == 1 and len(model_calls) == 3 and len(dispatches) == 3
    saved = read(env)
    transfer = saved["transfers"][0]
    involved = [r for r in rows(env, "generation") if r["opportunity_id"] in
                {transfer["source_opportunity_id"], transfer["target_opportunity_id"]}
                and r["status"] == "generation_completed"]
    assert len(involved) == 1 and len({r["execution_id"] for r in involved}) == 1
    assert {r["detail"]["source"] for r in involved} == {"planner"}
    assert len([r for r in rows(env, "driver_send") if r["opportunity_id"] in
                {transfer["source_opportunity_id"], transfer["target_opportunity_id"]}]) == 1
    (env.root / "reply-redelivery-example.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_queue_saturation_and_blocked_writer_does_not_block_generation(env, monkeypatch):
    enable(env)
    tracker = tracker_for(env)
    assert env.store.flush()
    env.store.configure(Policy(True, frozenset({"group-a"}), queue_capacity=1))
    tracker = tracker_for(env)
    assert env.store.flush()
    entered, release = Event(), Event()
    original = env.store._write

    def blocked(batches):
        entered.set()
        release.wait(3)
        original(batches)

    monkeypatch.setattr(env.store, "_write", blocked)
    gen, requests = generator(monkeypatch)
    durations = []
    try:
        with reply_flow.planner_scope(tracker, env.chats["group-a"], ToolInvocation("reply")):
            reply_flow.event("group-a", "writer_blocked", "observed")
            assert await asyncio.to_thread(entered.wait, 1)
            for _ in range(100):
                start = time.perf_counter_ns()
                reply_flow.event("group-a", "load", "observed")
                durations.append((time.perf_counter_ns() - start) / 1000)
            result = await asyncio.wait_for(gen.generate_reply_with_context(stream_id="group-a", chat_history=[]), 1)
            assert result[0] and len(requests) == 1
        assert env.store.dropped > 0
    finally:
        release.set()
    assert await asyncio.to_thread(env.store.flush)
    durations.sort()
    report = dict(iterations=len(durations), median_us=durations[50], p95_us=durations[95], max_us=max(durations),
                  dropped=env.store.dropped, queue_bound=1)
    (env.root / "reply-performance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_send_api_aborted_before_driver_and_return_id_mutation(env, monkeypatch):
    enable(env)
    tracker = tracker_for(env)
    original = send_service._send_via_platform_io

    async def blocked(message, **kwargs):
        return None

    monkeypatch.setattr(send_service, "_send_via_platform_io", blocked)
    with reply_flow.planner_scope(tracker, env.chats["group-a"], ToolInvocation("reply")):
        assert await send_service.send_session_message_with_message(outbound("blocked")) is None
    assert not rows(env, "driver_send")
    assert rows(env, "send_api_ended")[0]["detail"]["returned_non_null"] is False

    async def mutated(message, **kwargs):
        message.message_id = "external-platform-id"
        return message

    monkeypatch.setattr(send_service, "_send_via_platform_io", mutated)
    with reply_flow.planner_scope(tracker, env.chats["group-a"], ToolInvocation("reply")):
        await send_service.send_session_message_with_message(outbound("internal-id"))
    assert rows(env, "send_api_ended")[-1]["outbound_id"] == "internal-id"
    monkeypatch.setattr(send_service, "_send_via_platform_io", original)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "emoji", "fallback"])
async def test_priority_generation_and_send_behavior_parity_with_replay(env, monkeypatch, kind):
    results = []
    for mode in ("off", "on", "fault"):
        # Reset the Mock input state, so every comparison starts with the same history and dedup state.
        env.runtime("group-a")
        env.service._rights.priority.clear()
        env.service._rights.normal.clear()
        env.service._observation_references.clear()
        env.service._replay_traces.clear()
        env.store.configure(Policy(mode != "off", frozenset({"group-a"})))
        replay_store = ReplayStore(env.root / (mode + "-priority-replay.db"))
        monkeypatch.setattr(replay, "store", replay_store)
        monkeypatch.setattr(replay, "enabled", lambda: True)
        gen, requests = generator(monkeypatch)
        monkeypatch.setattr("src.chat.replyer.maisaka_generator.MaisakaReplyGenerator", lambda _gen=gen, **kwargs: _gen)
        monkeypatch.setattr("src.services.priority_at_reply_service.random.random", lambda: 0.0)
        _, dispatches, _ = pipeline(monkeypatch)
        attempts = []

        async def priority(sequence, message, _attempts=attempts, _mode=mode, **kwargs):
            _attempts.append((replies.classify_sequence(sequence), kwargs.get("set_reply", True)))
            if kind == "fallback" and len(_attempts) == 1:
                return False
            msg = outbound("priority:" + _mode + ":" + str(len(_attempts)))
            msg.raw_message = sequence
            return await send_service.send_session_message_with_message(msg) is not None

        monkeypatch.setattr(send_service, "send_priority_at_reply", priority)
        original = env.store._write
        if mode == "fault":
            monkeypatch.setattr(env.store, "_write", Mock(side_effect=sqlite3.OperationalError("injected")))
        msg = env.message("parity-input", text="question" if kind == "text" else "")
        await env.receiver.process_message(msg)
        await env.drain()
        await env.receiver.process_message(env.message("parity-input", text="question" if kind == "text" else ""))
        await env.drain()  # Existing seen-message dedup remains effective.
        assert env.store.flush() and replay_store.flush()
        results.append(dict(mode=mode, model_calls=len(requests), send_calls=attempts,
                            replyer_parameters=gen.observed_parameters,
                            driver_calls=len(dispatches), handled=msg.message_info.additional_config.get("priority_at_handled"),
                            pending=len(env.chats["group-a"].message_cache), tasks=len(env.service._tasks),
                            queued=len(env.service._pending), statistics=statistics(replay_store)["counts"]))
        if mode == "on":
            observed = rows(env, "driver_send")
            assert observed[-1]["reply_id"] is not None
            assert (observed[-1]["generation_group_id"] is None) == (kind == "emoji")
            assert observed[-1]["detail"]["platform_confirmation"] == "not_observed"
        monkeypatch.setattr(env.store, "_write", original)
    assert len({json.dumps({k: v for k, v in row.items() if k != "mode"}, sort_keys=True) for row in results}) == 1
    (env.root / ("reply-priority-" + kind + "-parity.json")).write_text(json.dumps(results, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_real_orchestrator_request_identity_without_network(env, monkeypatch):
    from src.config.model_configs import APIProvider, ModelInfo
    from src.llm_models.model_client.base_client import APIResponse, RequestTraceContext, ResponseRequest
    from src.llm_models.utils_model import LLMOrchestrator

    enable(env)
    tracker = tracker_for(env)
    provider = APIProvider(name="mock", base_url="https://example.invalid/v1", auth_type="none", max_retry=1)
    request = ResponseRequest(ModelInfo(name="mock", model_identifier="mock", api_provider="mock"), [],
                              trace_context=RequestTraceContext(request_id="explicit-model-request", task_name="replyer",
                                                                request_type="maisaka.replyer", session_id="group-a"))
    client = SimpleNamespace(get_response=AsyncMock(return_value=APIResponse()))
    orchestrator = LLMOrchestrator("replyer", "maisaka.replyer", "group-a")
    with reply_flow.planner_scope(tracker, env.chats["group-a"], ToolInvocation("reply")):
        reply_flow.generation_entered("group-a")
        result = await orchestrator._attempt_request_on_model(provider, client, request)
    assert client.get_response.await_count == 1
    assert result.generation_attempts[0].attempt_id == rows(env, "generation_request")[0]["request_id"]
    assert rows(env, "generation_request")[0]["request_id"] == "explicit-model-request:1"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_target", "extension_prepare", "extension_transform"])
async def test_target_and_extension_stops_do_not_invent_send(env, reply_context, monkeypatch, failure):
    enable(env)
    gen, requests = generator(monkeypatch)
    rt = ordinary(env, reply_context, monkeypatch, gen)
    _, dispatches, _ = pipeline(monkeypatch)
    if failure == "extension_prepare":
        monkeypatch.setattr(reply_tool.ReplyExtensionExecution, "prepare", AsyncMock(side_effect=RuntimeError("private")))
    elif failure == "extension_transform":
        async def prepare(extension, options):
            extension.targets = [SimpleNamespace()]
        monkeypatch.setattr(reply_tool.ReplyExtensionExecution, "prepare", prepare)
        monkeypatch.setattr(reply_tool.ReplyExtensionExecution, "transform", AsyncMock(side_effect=RuntimeError("private")))
    tracker = tracker_for(env)
    call = ToolInvocation("reply", arguments={"msg_id": "absent" if failure == "missing_target" else "msg-1"})
    with reply_flow.planner_scope(tracker, rt, call):
        assert not (await reply_tool.handle_tool(reply_context, call)).success
    assert not dispatches and not rows(env, "driver_send")
    assert len(requests) == (1 if failure == "extension_transform" else 0)
    if failure == "missing_target":
        assert rows(env, "reply_target")[0]["reason"] == "target_not_found"
    else:
        assert rows(env, "reply_extension")[0]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_two_accounts_concurrent_real_generations_and_driver_sends(env, monkeypatch):
    enable(env, "group-a", "group-b")
    env.runtime("group-b", "bot-two")
    manager, dispatches, _ = pipeline(monkeypatch)
    a, _ = generator(monkeypatch, "group-a")
    b, _ = generator(monkeypatch, "group-b")

    async def run(sid, account, gen):
        tracker = tracker_for(env, env.chats[sid], mid="same-platform-message-id")
        with reply_flow.planner_scope(tracker, env.chats[sid], ToolInvocation("reply", call_id="same-tool-id")):
            assert (await gen.generate_reply_with_context(stream_id=sid, chat_history=[]))[0]
            await manager.send_message(outbound(sid + ":outbound", sid), RouteKey("qq", account))

    await asyncio.gather(run("group-a", "bot", a), run("group-b", "bot-two", b))
    assert len(dispatches) == 2
    for sid, account in (("group-a", "bot"), ("group-b", "bot-two")):
        saved = rows(env, sid=sid)
        assert len([r for r in saved if r["status"] == "generation_completed"]) == 1
        assert len(rows(env, "driver_send", sid)) == 1
        assert all(r["detail"]["account_id"] == account for r in saved)
        assert all(r["execution_id"].startswith("execution:" + sid + ":") for r in saved)
        assert {r["request_id"] for r in saved if r["request_id"]} == {sid + ":request:1"}
