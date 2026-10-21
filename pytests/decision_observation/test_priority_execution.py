"""Real receiver/admission/dispatch/branch code; synthetic QQ, Replyer, bytes and send APIs only."""

from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio
import json
import sqlite3
import time

import pytest
import pytest_asyncio

from src.chat.heart_flow.heartflow_message_processor import HeartFCMessageReceiver
from src.common.data_models.message_component_data_model import AtComponent, MessageSequence, TextComponent
from src.decision_observation import recorder
from src.decision_observation.models import Policy, SCHEMA_VERSION
from src.decision_observation.store import ObservationStore
from src.decision_replay import recorder as replay
from src.maisaka.runtime import MaisakaHeartFlowChatting
from src.services.priority_at_reply_service import PriorityAtReplyService
from test_observation import message as ordinary_message, runtime as ordinary_runtime


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    from src.chat.heart_flow import heartflow_message_processor as receiver
    from src.chat.replyer import maisaka_generator
    from src.chat.utils import utils
    from src.common.utils import image_path
    from src.config.config import global_config
    from src.emoji_system import emoji_manager, reply_attachment
    from src.services import auto_repeat_service, bot_account_service, group_silence_emoji_service
    from src.services import (
        memory_flow_service,
        owner_control_service,
        priority_at_reply_service,
        send_service,
        tag_extraction_service,
    )
    from src.services.owner_admin import override_runtime, system_runtime

    store = ObservationStore(tmp_path / "observation.db")
    monkeypatch.setattr(recorder, "store", store)
    service = PriorityAtReplyService(max_concurrent=2, max_per_session=1, max_pending=4, max_pending_per_session=2)
    monkeypatch.setattr(priority_at_reply_service, "priority_at_reply_service", service)
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", True)
    monkeypatch.setattr(global_config.chat, "priority_at_reply_emoji_hashes", ["fixed"])
    monkeypatch.setattr(utils, "is_mentioned_bot_in_message", lambda m: (True, True, 0.0))
    monkeypatch.setattr(bot_account_service, "is_bot_self", lambda platform, uid: uid in {"bot", "bot-two"})
    monkeypatch.setattr(system_runtime, "session_paused", lambda sid: False)
    monkeypatch.setattr(system_runtime, "paused", lambda account: False)
    monkeypatch.setattr(system_runtime, "stale_lease", lambda account: False)
    monkeypatch.setattr(override_runtime, "_capture_presentation", lambda sid: None)
    token = system_runtime._lease.set(None)
    for target, name, mock in (
        (receiver.MessageUtils, "store_message_to_db_async", AsyncMock()),
        (receiver.Person, "register_person", Mock()),
        (group_silence_emoji_service.group_silence_emoji_service, "record_activity", Mock()),
        (tag_extraction_service.tag_extraction_service, "notify_message_saved", Mock()),
        (memory_flow_service.memory_automation_service, "on_incoming_message", AsyncMock()),
        (auto_repeat_service.auto_repeat_service, "on_incoming_message", AsyncMock()),
        (owner_control_service, "handle_owner_command", AsyncMock(return_value=None)),
        (reply_attachment, "send_after_text", AsyncMock()),
    ):
        monkeypatch.setattr(target, name, mock)
    errors = Mock()
    monkeypatch.setattr(receiver.logger, "error", errors)
    result = SimpleNamespace(
        completion=SimpleNamespace(response_text="synthetic reply"), selected_emoji_tag=None, error_message=""
    )
    generate = AsyncMock(return_value=(True, result))
    monkeypatch.setattr(
        maisaka_generator,
        "MaisakaReplyGenerator",
        Mock(return_value=SimpleNamespace(generate_reply_with_context=generate)),
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(send_service, "send_priority_at_reply", send)
    image = tmp_path / "synthetic.gif"
    image.write_bytes(b"synthetic-image")
    monkeypatch.setattr(image_path, "resolve_stored_image_path", lambda p: Path(p))
    monkeypatch.setattr(
        emoji_manager.emoji_manager,
        "get_emoji_by_hash",
        lambda key: SimpleNamespace(file_hash=key, full_path=str(image)),
    )
    chats = {}

    def runtime(sid="group-a", account="bot"):
        rt = ordinary_runtime(sid, [])
        rt.chat_stream.account_id = account
        rt._running = True
        rt._reasoning_engine = SimpleNamespace(
            _build_history_message=AsyncMock(side_effect=lambda m, **kw: m.message_id)
        )
        for name in (
            "_emit_monitor_message_ingested",
            "_ensure_background_tasks_running",
            "_record_external_message_interval",
            "_update_message_trigger_state",
            "_prune_processed_message_cache",
            "_request_planner_interrupt_for_message",
            "_schedule_message_turn",
            "_mark_message_debounce_required",
        ):
            setattr(rt, name, Mock())
        rt._is_reply_effect_tracking_enabled = lambda: False
        rt._should_continue_after_focus_gate = lambda msg: True
        rt.register_message = lambda msg, **kwargs: MaisakaHeartFlowChatting.register_message(rt, msg, **kwargs)
        chats[sid] = rt
        return rt

    runtime()
    monkeypatch.setattr(system_runtime, "account_for_session", lambda sid: chats[sid].chat_stream.account_id or "")
    monkeypatch.setattr(
        receiver.heartflow_manager, "get_or_create_heartflow_chat", AsyncMock(side_effect=lambda sid: chats[sid])
    )

    def message(mid="m1", sid="group-a", *, text="question", account="bot"):
        msg = ordinary_message(mid, sid)
        msg.message_info.additional_config["account_id"] = account
        msg.message_info.group_info = SimpleNamespace(group_id="synthetic-group", group_name=sid)
        msg.message_info.user_info.user_nickname = "synthetic"
        msg.message_info.user_info.user_cardname = ""
        msg.raw_message = MessageSequence([AtComponent(account)] + ([TextComponent(text)] if text else []))
        msg.processed_plain_text = text
        return msg

    async def drain():
        while service._tasks or service._pending:
            await asyncio.sleep(0.001)

    state = SimpleNamespace(
        store=store,
        service=service,
        chats=chats,
        runtime=runtime,
        message=message,
        receiver=HeartFCMessageReceiver(),
        generate=generate,
        send=send,
        drain=drain,
        errors=errors,
        result=result,
        root=tmp_path,
    )
    yield state
    await service.shutdown()
    system_runtime._lease.reset(token)
    assert await asyncio.to_thread(store.close)


def enable(env, *sessions):
    env.store.configure(Policy(True, frozenset(sessions or ("group-a",)), 30, 4096))


def read(env, sid="group-a"):
    assert env.store.flush()
    return env.store.query(sid, 200)


def events(env, stage=None, sid="group-a"):
    return [e for e in read(env, sid)["execution_events"] if stage is None or e["stage"] == stage]


@pytest.mark.asyncio
async def test_receiver_text_owned_execution_replyer_and_no_ordinary_duplicate(env):
    enable(env)
    msg = env.message()
    await env.receiver.process_message(msg)
    assert len(env.service._tasks) == 1
    assert events(env, "priority_task_created") and not events(env, "priority_task_started")
    assert msg.message_info.additional_config["priority_at_handled"] is True
    assert not env.chats["group-a"].message_cache
    env.chats["group-a"]._schedule_message_turn.assert_not_called()
    await asyncio.wait_for(env.drain(), 2)
    saved = read(env)
    assert len(saved["opportunities"]) == 1 and saved["opportunities"][0]["source_type"] == "priority_at"
    assert saved["outcomes"][0]["planner_status"] == "not_applicable"
    assert saved["outcomes"][0]["replyer_status"] == "entry_observed"
    assert saved["outcomes"][0]["send_status"] == "not_observed"
    assert not saved["evaluations"] and not saved["transfers"]
    assert events(env, "priority_reply_entry") and events(env, "priority_replyer_entered")
    assert events(env, "priority_path_selected")[0]["status"] == "text"
    assert all(e["logical_turn_id"] is None and e["cycle_id"] is None for e in saved["execution_events"])
    assert not events(env, "planner_started")
    assert env.generate.await_count == env.send.await_count == 1
    env.errors.assert_not_called()
    (env.root / "priority-text-example.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_pure_at_actual_fixed_emoji_choice_and_failed_emoji_text_fallback(env, monkeypatch):
    enable(env)
    monkeypatch.setattr("src.services.priority_at_reply_service.random.random", lambda: 0.0)
    await env.receiver.process_message(env.message("emoji", text=""))
    await env.drain()
    assert env.generate.await_count == 0 and env.send.await_count == 1
    assert events(env, "priority_path_selected")[0]["status"] == "fixed_emoji"
    assert events(env, "priority_fixed_emoji_selected") and not events(env, "priority_replyer_entered")
    assert env.send.call_args.kwargs["set_reply"] is False
    (env.root / "priority-emoji-example.json").write_text(json.dumps(read(env), indent=2), encoding="utf-8")
    env.send.side_effect = [False, True]
    await env.receiver.process_message(env.message("emoji-fallback", text=""))
    await env.drain()
    assert env.generate.await_count == 1 and env.send.await_count == 3
    assert events(env, "priority_emoji_fallback")
    assert all(o["send_status"] == "not_observed" for o in read(env)["outcomes"])


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["global", "session"])
async def test_capacity_full_explicit_fallback_relation_and_real_planner(env, monkeypatch, limit):
    from test_execution import harness
    from test_observation import schedule

    enable(env, "group-a", "group-b")
    env.runtime("group-b")
    env.service._max_concurrent = 1
    env.service._max_pending = 1 if limit == "global" else 5
    env.service._max_pending_per_session = 1
    release = asyncio.Event()

    async def generate(**kwargs):
        await release.wait()
        return True, env.result

    env.generate.side_effect = generate
    for mid in ("active", "queued"):
        await env.receiver.process_message(env.message(mid))
    target_sid = "group-b" if limit == "global" else "group-a"
    msg = env.message("fallback", target_sid)
    await env.receiver.process_message(msg)
    rt = env.chats[target_sid]
    assert rt.message_cache == [msg] and not msg.message_info.additional_config.get("priority_at_handled")
    assert len(env.service._tasks) == len(env.service._pending) == 1
    saved = read(env, target_sid)
    transfer = saved["transfers"][0]
    sources = {o["opportunity_id"]: o for o in saved["opportunities"]}
    assert sources[transfer["source_opportunity_id"]]["source_type"] == "priority_at"
    assert sources[transfer["target_opportunity_id"]]["source_type"] == "ordinary_pending"
    assert transfer["account_id"] == "bot"
    rejection = [
        e
        for e in events(env, "priority_rejected", target_sid)
        if e["opportunity_id"] == transfer["source_opportunity_id"]
    ]
    assert rejection[0]["reason"] == ("global_queue_full" if limit == "global" else "session_queue_full")
    assert not [
        e
        for e in events(env, "priority_task_created", target_sid)
        if e["opportunity_id"] == transfer["source_opportunity_id"]
    ]
    engine, calls = harness(monkeypatch, rt)
    rt._has_forced_turn_trigger = lambda: True
    schedule(monkeypatch, rt)
    await engine.run_loop()
    assert len(calls) == 1
    starts = events(env, "planner_started", target_sid)
    assert len(starts) == 1 and starts[0]["opportunity_id"] == transfer["target_opportunity_id"]
    assert read(env, target_sid)["evaluations"][0]["decision_reason"] == "priority_fallback_forced_trigger"
    (env.root / "priority-fallback-example.json").write_text(
        json.dumps(read(env, target_sid), indent=2), encoding="utf-8"
    )
    release.set()
    await env.drain()
    assert env.generate.await_count == 2  # rejected message did not launch independent Replyer


@pytest.mark.asyncio
async def test_concurrency_busy_is_queued_then_dispatched_without_fallback(env):
    enable(env, "group-a", "group-b")
    env.runtime("group-b")
    release = asyncio.Event()

    async def generate(**kwargs):
        await release.wait()
        return True, env.result

    env.generate.side_effect = generate
    for mid, sid in (("a1", "group-a"), ("a2", "group-a"), ("b1", "group-b"), ("b2", "group-b")):
        await env.receiver.process_message(env.message(mid, sid))
    assert len(env.service._tasks) == 2 and len(env.service._pending) == 2
    assert env.service._running_per_session == {"group-a": 1, "group-b": 1}
    assert any(e["reason"] == "waiting_for_concurrency_slot" for e in events(env, "priority_enqueued"))
    assert not read(env)["transfers"] and not read(env, "group-b")["transfers"]
    release.set()
    await env.drain()
    assert env.generate.await_count == env.send.await_count == 4
    assert len(events(env, "priority_task_started")) == len(events(env, "priority_task_started", "group-b")) == 2


@pytest.mark.asyncio
async def test_cancel_before_start_while_queued_and_during_execution(env):
    enable(env)
    await env.receiver.process_message(env.message("before-start"))
    env.service._tasks[env.service.message_key(env.message("before-start"))].cancel()
    await env.drain()
    assert not events(env, "priority_task_started") and env.generate.await_count == env.send.await_count == 0
    assert events(env, "priority_task_ended")[0]["reason"] == "cancelled_before_observed_start"
    entered = asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    env.generate.side_effect = generate
    await env.receiver.process_message(env.message("during"))
    await entered.wait()
    await env.receiver.process_message(env.message("queued"))
    await env.service.shutdown()
    ends = events(env, "priority_task_ended")
    assert any(e["reason"] == "cancelled_after_observed_start" for e in ends)
    assert events(env, "priority_queue_cancelled")[0]["reason"] == "shutdown_while_queued"
    assert env.send.await_count == 0


@pytest.mark.asyncio
async def test_execution_exception_and_guard_short_circuit_are_not_success(env, monkeypatch):
    from src.services.owner_admin import system_runtime

    enable(env)
    env.generate.side_effect = RuntimeError("synthetic failure")
    await env.receiver.process_message(env.message("failed"))
    await env.drain()
    assert events(env, "priority_task_ended")[0]["status"] == "unknown"
    assert env.send.await_count == 0
    env.generate.side_effect = None
    await env.receiver.process_message(env.message("guarded"))
    monkeypatch.setattr(system_runtime, "stale_lease", lambda account: True)
    await env.drain()
    ends = events(env, "priority_task_ended")
    assert any(e["reason"] == "reply_entry_not_observed" for e in ends)
    assert len(events(env, "priority_replyer_entered")) == 1


@pytest.mark.asyncio
async def test_redelivery_single_demand_single_task_and_owned_message_guard(env):
    enable(env)
    release = asyncio.Event()

    async def generate(**kwargs):
        await release.wait()
        return True, env.result

    env.generate.side_effect = generate
    for _ in range(3):
        await env.receiver.process_message(env.message())
    assert len(env.service._tasks) == 1 and len(read(env)["opportunities"]) == 1
    assert len(events(env, "priority_task_created")) == 1 and len(events(env, "priority_owned_existing")) == 2
    owner_execution = events(env, "priority_task_created")[0]["execution_id"]
    assert all(e["parent_execution_id"] == owner_execution for e in events(env, "priority_owned_existing"))
    release.set()
    await env.drain()
    await env.receiver.process_message(env.message())
    assert env.generate.await_count == env.send.await_count == 1
    assert env.chats["group-a"]._chat_history == ["m1"]
    assert not env.chats["group-a"].message_cache and not read(env)["transfers"]
    env.chats["group-a"]._schedule_message_turn.assert_not_called()


@pytest.mark.asyncio
async def test_cross_account_and_group_same_platform_message_ids_never_mix(env):
    enable(env, "group-a", "account-two-group-a", "group-b")
    env.runtime("account-two-group-a", "bot-two")
    env.runtime("group-b")
    await asyncio.gather(
        *(
            env.receiver.process_message(env.message("same", sid, account=account))
            for sid, account in (("group-a", "bot"), ("account-two-group-a", "bot-two"), ("group-b", "bot"))
        )
    )
    await env.drain()
    saved = [read(env, sid) for sid in ("group-a", "account-two-group-a", "group-b")]
    assert len({s["opportunities"][0]["opportunity_id"] for s in saved}) == 3
    assert len({s["messages"][0]["message_event_id"] for s in saved}) == 3
    assert [s["opportunities"][0]["account_id"] for s in saved] == ["bot", "bot-two", "bot"]
    assert env.generate.await_count == env.send.await_count == 3
    bad = env.message("wrong-account", account="bot-two")
    assert recorder.priority_candidate(env.chats["group-a"], bad) is None
    assert env.store.errors > 0
    assert len(read(env)["opportunities"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "on", "write_failure", "adapter_failure"])
async def test_production_parity_with_queue_rejection_duplicate_and_fixed_path(env, monkeypatch, mode):
    if mode != "off":
        enable(env)
    if mode == "write_failure":
        monkeypatch.setattr(env.store, "_write", Mock(side_effect=sqlite3.OperationalError("synthetic")))
    elif mode == "adapter_failure":
        monkeypatch.setattr(recorder, "_priority_scope", Mock(side_effect=RuntimeError("synthetic")))
    monkeypatch.setattr("src.services.priority_at_reply_service.random.random", lambda: 0.0)
    env.service._max_pending_per_session = 1
    for mid, text in (("active", "question"), ("queued", ""), ("fallback", "question"), ("active", "question")):
        await env.receiver.process_message(env.message(mid, text=text))
    before = {
        "tasks": len(env.service._tasks),
        "pending": len(env.service._pending),
        "running": dict(env.service._running_per_session),
        "seen": len(env.service._seen),
        "normal_messages": [m.message_id for m in env.chats["group-a"].message_cache],
    }
    assert before == {"tasks": 1, "pending": 1, "running": {"group-a": 1}, "seen": 2, "normal_messages": ["fallback"]}
    await env.drain()
    after = {
        "tasks": len(env.service._tasks),
        "pending": len(env.service._pending),
        "running": dict(env.service._running_per_session),
        "model_requests": env.generate.await_count,
        "send_calls": env.send.await_count,
        "send_reply_flags": [c.kwargs.get("set_reply", True) for c in env.send.call_args_list],
        "normal_schedule_calls": env.chats["group-a"]._schedule_message_turn.call_count,
        "history": list(env.chats["group-a"]._chat_history),
        "seen": len(env.service._seen),
    }
    assert after == {
        "tasks": 0,
        "pending": 0,
        "running": {},
        "model_requests": 1,
        "send_calls": 2,
        "send_reply_flags": [True, False],
        "normal_schedule_calls": 1,
        "history": ["active", "queued"],
        "seen": 2,
    }
    assert env.store.flush()
    if mode == "off":
        assert not env.store.path.exists()
    elif mode == "write_failure":
        assert env.store.write_errors > 0
    elif mode == "adapter_failure":
        assert env.store.errors > 0
    (env.root / f"priority-parity-{mode}.json").write_text(
        json.dumps({"before": before, "after": after}, indent=2), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_replay_actual_trace_only_and_no_extra_replay_events(env, monkeypatch):
    enable(env)
    monkeypatch.setattr(replay, "enabled", lambda: True)
    await env.receiver.process_message(env.message())
    expected = env.service._replay_traces[env.service.message_key(env.message("m1"))].logical_turn_id
    await env.drain()
    entry = events(env, "priority_reply_entry")[0]
    assert entry["logical_turn_id"] == expected and entry["replay_status"] == "trace_bound_persistence_unknown"
    assert all(e["cycle_id"] is None for e in events(env))
    assert replay.store.flush()
    with sqlite3.connect(replay.store.path) as connection:
        assert connection.execute("SELECT count(DISTINCT logical_turn_id) FROM events").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["guard", "self", "not_at"])
async def test_actual_claim_rejections_do_not_create_fake_candidates(env, monkeypatch, reason):
    from src.chat.utils import utils
    from src.config.config import global_config

    enable(env)
    msg = env.message()
    if reason == "guard":
        monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", False)
    elif reason == "self":
        msg.message_info.user_info.user_id = "bot"
    else:
        monkeypatch.setattr(utils, "is_mentioned_bot_in_message", lambda m: (True, False, 0))
    await env.receiver.process_message(msg)
    saved = read(env)
    assert len(saved["claim_checks"]) == 1
    assert not [o for o in saved["opportunities"] if o["source_type"] == "priority_at"]
    assert not events(env, "priority_task_created")
    assert env.generate.await_count == env.send.await_count == 0


@pytest.mark.asyncio
async def test_schema_two_migration_preserves_old_rows_and_no_priority_backfill(env):
    enable(env)
    rt = env.chats["group-a"]
    recorder.pending(rt, [env.message()])
    assert env.store.close()
    with sqlite3.connect(env.store.path) as connection:
        connection.execute("DROP TABLE delivery_links")
        connection.execute("DROP TABLE delivery_receipts")
        connection.execute("DROP TABLE delivery_scopes")
        connection.execute("DROP TABLE reply_events")
        connection.execute("DROP TABLE priority_claim_checks")
        connection.execute("DROP TABLE opportunity_transfers")
        connection.execute("ALTER TABLE opportunities DROP COLUMN account_id")
        connection.execute("DROP TABLE annotation_revisions")
        connection.execute("DROP TABLE annotation_targets")
        connection.execute("PRAGMA user_version=2")
    assert env.store.query("group-a")["transfers"] == []
    connection = env.store._connect()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute("SELECT source_type,account_id FROM opportunities").fetchall() == [
        ("ordinary_pending", None)
    ]
    assert connection.execute("SELECT count(*) FROM opportunity_transfers").fetchone()[0] == 0
    connection.close()


@pytest.mark.asyncio
async def test_priority_adapter_benchmark_and_async_writer_isolation(env, monkeypatch):
    enable(env)
    rt, msg = env.chats["group-a"], env.message()
    durations = []
    for _ in range(300):
        start = time.perf_counter_ns()
        recorder.priority_candidate(rt, msg)
        durations.append((time.perf_counter_ns() - start) / 1000)
    assert await asyncio.to_thread(env.store.flush, 10)
    durations.sort()
    print("OBS_PRIORITY_CANDIDATE_US", {"p50": durations[149], "p95": durations[284], "p99": durations[296]})
    assert len(read(env)["opportunities"]) == 1


@pytest.mark.asyncio
async def test_blocked_observation_writer_does_not_wait_in_production(env, monkeypatch):
    enable(env)
    entered, release = Event(), Event()
    write = env.store._write

    def blocked(batches):
        entered.set()
        release.wait(2)
        write(batches)

    monkeypatch.setattr(env.store, "_write", blocked)
    try:
        await env.receiver.process_message(env.message())
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(env.drain(), 1)
        assert env.generate.await_count == env.send.await_count == 1
        assert env.store.queue.unfinished_tasks > 0
    finally:
        release.set()
    assert await asyncio.to_thread(env.store.flush, 2)
    assert len(events(env, "priority_task_started")) == 1


@pytest.mark.asyncio
async def test_revocation_and_session_delete_invalidate_active_receipts(env):
    enable(env)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return True, env.result

    env.generate.side_effect = generate
    await env.receiver.process_message(env.message())
    await entered.wait()
    assert await asyncio.to_thread(env.store.flush)
    env.store.configure(Policy())
    before = len(read(env)["execution_events"])
    enable(env)
    release.set()
    await env.drain()
    assert len(read(env)["execution_events"]) == before
    assert env.send.await_count == 1
    await asyncio.to_thread(env.store.delete_session, "group-a")
    assert not read(env)["messages"] and not read(env)["opportunities"] and not read(env)["claim_checks"]
    await env.receiver.process_message(env.message("after-delete"))
    await env.drain()
    assert env.send.await_count == 2 and not read(env)["opportunities"]


@pytest.mark.asyncio
async def test_duplicate_batch_and_transfer_insertion_are_idempotent(env, monkeypatch):
    enable(env)
    env.service._closing = True
    batches = []
    submit = env.store.submit

    def capture(batch):
        if batch.transfer is not None:
            batches.append(batch)
        return submit(batch)

    monkeypatch.setattr(env.store, "submit", capture)
    await env.receiver.process_message(env.message())
    assert len(batches) == 1 and len(read(env)["transfers"]) == 1
    assert submit(batches[0])
    assert len(read(env)["transfers"]) == 1
    # Re-submit an exactly identical execution event, not a second invocation.
    ref = recorder.priority_candidate(env.chats["group-a"], env.message("other"))
    captured = []
    monkeypatch.setattr(env.store, "submit", lambda b: captured.append(b) or True)
    recorder.priority_event(None, env.message("other"), ref, "priority_candidate", "observed", "mock")
    assert submit(captured[0]) and submit(captured[0])
    saved = read(env)
    assert len({e["event_id"] for e in saved["execution_events"]}) == len(saved["execution_events"])
    assert sum(e["event_id"] == captured[0].execution.event_id for e in saved["execution_events"]) == 1


@pytest.mark.asyncio
async def test_legacy_unknown_account_and_explicit_receipt_scope(env):
    enable(env)
    rt = env.chats["group-a"]
    rt.chat_stream.account_id = None
    msg = env.message()
    msg.message_info.additional_config.pop("account_id")
    ref = recorder.priority_candidate(rt, msg)
    assert ref is not None and ref.opportunity.account_id is None
    assert env.service.start_reply(msg, rt, observation_reference=ref)
    await env.drain()
    assert read(env)["opportunities"][0]["account_id"] is None
    assert env.generate.await_count == 1


@pytest.mark.asyncio
async def test_replay_on_off_observation_does_not_add_trace_or_model_calls(env, monkeypatch):
    monkeypatch.setattr(replay, "enabled", lambda: True)
    counts = []
    for observation_enabled in (False, True):
        env.store.configure(Policy(observation_enabled, frozenset({"group-a"}), 30, 4096))
        await env.receiver.process_message(env.message("on" if observation_enabled else "off"))
        await env.drain()
        assert replay.store.flush()
        with sqlite3.connect(replay.store.path) as connection:
            counts.append(connection.execute("SELECT count(*) FROM events").fetchone()[0])
    assert counts[1] - counts[0] == counts[0]
    assert env.generate.await_count == env.send.await_count == 2
    assert all(e["logical_turn_id"] is None for e in events(env, "priority_task_started"))


@pytest.mark.asyncio
@pytest.mark.parametrize("observing", [False, True])
async def test_rejected_then_redelivered_preserves_single_ordinary_owner(env, monkeypatch, observing):
    from test_execution import harness
    from test_observation import schedule

    if observing:
        enable(env)
    env.service._max_concurrent = env.service._max_pending = env.service._max_pending_per_session = 1
    await env.receiver.process_message(env.message("active"))
    await env.receiver.process_message(env.message("queued"))
    first = env.message("redelivered")
    await env.receiver.process_message(first)
    assert env.chats["group-a"].message_cache == [first]
    await env.drain()
    later = env.message("redelivered")
    await env.receiver.process_message(later)
    await env.drain()
    assert later.message_info.additional_config.get("priority_at_handled") is not True
    # The earlier fallback retains ordinary ownership; the candidate redelivery
    # remains observable but is not a second accepted execution.
    assert env.chats["group-a"].message_cache == [first]
    assert not first.message_info.additional_config.get("priority_at_handled")
    engine, calls = harness(monkeypatch, env.chats["group-a"])
    env.chats["group-a"]._has_forced_turn_trigger = lambda: True
    schedule(monkeypatch, env.chats["group-a"])
    await engine.run_loop()
    assert len(calls) == 1 and env.generate.await_count == env.send.await_count == 2
    if observing:
        saved = read(env)
        transfer = saved["transfers"][0]
        assert (
            len(
                [
                    e
                    for e in saved["execution_events"]
                    if e["stage"] == "priority_taken" and e["opportunity_id"] == transfer["source_opportunity_id"]
                ]
            )
            == 0
        )
        assert events(env, "priority_ordinary_owned")
        assert events(env, "planner_started")[0]["opportunity_id"] == transfer["target_opportunity_id"]
        (env.root / "priority-redelivery-boundary.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_concurrent_duplicate_history_build_has_one_task_and_explicit_parent(env):
    enable(env)
    both = asyncio.Event()
    building = 0

    async def build(msg, **kwargs):
        nonlocal building
        building += 1
        if building == 2:
            both.set()
        await both.wait()
        return msg.message_id

    env.chats["group-a"]._reasoning_engine._build_history_message = build
    await asyncio.wait_for(asyncio.gather(*(env.receiver.process_message(env.message()) for _ in range(2))), 1)
    await env.drain()
    assert env.generate.await_count == env.send.await_count == 1
    assert env.chats["group-a"]._chat_history == ["m1"] and not env.chats["group-a"].message_cache
    assert len(read(env)["opportunities"]) == len(events(env, "priority_task_created")) == 1
    existing = events(env, "priority_owned_existing")[0]
    assert existing["parent_execution_id"] == events(env, "priority_task_created")[0]["execution_id"]
    assert not read(env)["transfers"]


@pytest.mark.asyncio
async def test_queue_pause_lease_invalidation_does_not_invent_task_start_or_fallback(env):
    enable(env)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return True, env.result

    env.generate.side_effect = generate
    await env.receiver.process_message(env.message("active"))
    await entered.wait()
    await env.receiver.process_message(env.message("paused-queued"))
    env.service._pending_pause_epochs[env.service.message_key(env.message("paused-queued"))] = ("bot", "synthetic-old-lease")
    release.set()
    await env.drain()
    assert env.generate.await_count == env.send.await_count == 1
    assert len(events(env, "priority_task_created")) == 1
    assert events(env, "priority_queue_cancelled")[0]["reason"] == "owner_pause_lease_invalidated_while_queued"
    assert not read(env)["transfers"] and not env.chats["group-a"].message_cache
