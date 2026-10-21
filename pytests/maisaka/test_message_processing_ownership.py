"""Shared processing ownership: synthetic QQ, models and drivers only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event as ThreadEvent
from types import SimpleNamespace
import asyncio

import pytest

from src.config.config import global_config
from src.decision_replay.replies import DeliveryUncertain
from src.maisaka.runtime import MaisakaHeartFlowChatting
from src.services.message_processing_rights import ProcessingRights, message_key
from src.services.send_service import send_priority_at_reply as real_priority_send
from test_priority_execution import env as _env, enable, read

env = _env

async def settled(service):
    while service._tasks or service._pending:
        tasks = tuple(service._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)  # Deliver done callbacks, never a race timer.


@pytest.mark.asyncio
@pytest.mark.parametrize("observing", [False, True])
async def test_full_queue_normal_pending_redelivery_has_no_second_execution(env, observing):
    if observing:
        enable(env)
    entered, release = asyncio.Event(), asyncio.Event()
    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return True, env.result
    env.generate.side_effect = generate
    env.service._max_concurrent = env.service._max_pending = env.service._max_pending_per_session = 1
    await env.receiver.process_message(env.message("active"))
    await entered.wait()
    await env.receiver.process_message(env.message("queued"))
    first = env.message("redelivered")
    await env.receiver.process_message(first)
    assert env.chats["group-a"].message_cache == [first]
    release.set()
    await settled(env.service)
    await env.receiver.process_message(env.message("redelivered"))
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 2
    assert env.chats["group-a"].message_cache == [first]


@pytest.mark.asyncio
@pytest.mark.parametrize("observing", [False, True])
async def test_normal_pending_first_rejects_direct_priority_takeover(env, monkeypatch, observing):
    if observing:
        enable(env)
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", False)
    original = env.message()
    await env.receiver.process_message(original)
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", True)
    assert not env.service.start_reply(env.message(), env.chats["group-a"])
    await env.receiver.process_message(env.message())
    assert env.chats["group-a"].message_cache == [original]
    assert env.generate.await_count == env.send.await_count == 0
    env.chats["group-a"]._schedule_message_turn.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("observing", [False, True])
async def test_priority_admission_suppresses_ordinary_copy(env, monkeypatch, observing):
    if observing:
        enable(env)
    await env.receiver.process_message(env.message())
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", False)
    await env.receiver.process_message(env.message())
    await settled(env.service)
    assert not env.chats["group-a"].message_cache
    assert env.chats["group-a"]._chat_history == ["m1"]
    assert env.generate.await_count == env.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("priority_first", [False, True])
async def test_two_entries_racing_history_build_have_only_one_owner(env, priority_first):
    rt = env.chats["group-a"]
    entered, release = asyncio.Event(), asyncio.Event()
    async def build(message, **kwargs):
        entered.set()
        await release.wait()
        return message.message_id
    rt._reasoning_engine._build_history_message = build
    first, second = env.message(), env.message()
    first.message_info.additional_config["priority_at_requested"] = True
    task = asyncio.create_task(MaisakaHeartFlowChatting.register_message(rt, first))
    await entered.wait()
    if priority_first:
        release.set()
        await task
    await MaisakaHeartFlowChatting.register_message(rt, second)
    if not priority_first:
        release.set()
        await task
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == int(priority_first)
    assert len(rt.message_cache) == int(not priority_first)


@pytest.mark.asyncio
async def test_two_normal_copies_commit_once(env, monkeypatch):
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", False)
    await asyncio.gather(*(env.receiver.process_message(env.message()) for _ in range(2)))
    rt = env.chats["group-a"]
    assert len(rt.message_cache) == 1
    rt._schedule_message_turn.assert_called_once()
    assert env.generate.await_count == env.send.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["account", "group", "platform", "id", "missing_id"])
async def test_real_identity_dimensions_never_dedupe_same_text(env, dimension):
    first = env.message("same" if dimension != "missing_id" else "")
    await env.receiver.process_message(first)
    if dimension == "account":
        env.runtime("group-a", "bot-two")
        second = env.message("same", account="bot-two")
    elif dimension == "group":
        env.runtime("group-b")
        second = env.message("same", "group-b")
    else:
        second = env.message("different" if dimension == "id" else "same" if dimension != "missing_id" else "")
        if dimension == "platform":
            second.platform = "other"
    assert first.processed_plain_text == second.processed_plain_text
    await env.receiver.process_message(second)
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 2
    assert message_key(first) != message_key(second)


@pytest.mark.asyncio
async def test_missing_account_keeps_explicit_legacy_namespace(env):
    first = env.message()
    first.message_info.additional_config.pop("account_id")
    await env.receiver.process_message(first)
    await env.receiver.process_message(env.message())
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 2


@pytest.mark.asyncio
async def test_closed_queue_preserves_one_ordinary_pending(env):
    await env.service.shutdown()
    first = env.message()
    await env.receiver.process_message(first)
    await env.receiver.process_message(env.message())
    assert env.chats["group-a"].message_cache == [first]
    assert env.generate.await_count == env.send.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("redelivery_first", [False, True])
async def test_consume_and_redelivery_keep_normal_execution_pinned(env, monkeypatch, redelivery_first):
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", False)
    msg = env.message()
    await env.receiver.process_message(msg)
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", True)
    ready, release = asyncio.Event(), asyncio.Event()
    async def consume():
        if redelivery_first:
            ready.set()
            await release.wait()
        batch = MaisakaHeartFlowChatting._collect_pending_messages(env.chats["group-a"])
        assert batch == [msg]
        if not redelivery_first:
            ready.set()
            await release.wait()
    pending = asyncio.create_task(consume())
    await ready.wait()
    await env.receiver.process_message(env.message())
    release.set()
    await pending
    rt = env.chats["group-a"]
    rt.message_cache.clear()  # Simulate pruning while a slow normal tool is active.
    rt._last_processed_index = 0
    await env.receiver.process_message(env.message())
    assert not rt.message_cache
    assert env.generate.await_count == env.send.await_count == 0
    env.service._rights.finish_normal(rt)
    assert env.service._rights.get(message_key(msg)).phase == "retained"


@pytest.mark.asyncio
async def test_unclassified_false_send_is_not_proof_of_no_delivery(env):
    env.send.return_value = False
    msg = env.message()
    await env.receiver.process_message(msg)
    await settled(env.service)
    await env.receiver.process_message(env.message())
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 1
    assert not env.chats["group-a"].message_cache
    assert env.service._rights.get(message_key(msg)).phase == "retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("observing", [False, True])
@pytest.mark.parametrize("replaying", [False, True])
@pytest.mark.parametrize("status_name", ["SENT", "UNKNOWN", "FAILED"])
async def test_real_driver_replay_unknown_and_duplicate_late_ack(env, monkeypatch, observing, replaying, status_name):
    from src.decision_replay import recorder as replay, replies
    from src.decision_replay.reply_metrics import statistics
    from src.decision_replay.store import ReplayStore
    from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverKind, RouteKey
    from src.services import send_service
    from test_reply_flow import generator, outbound
    from test_delivery import pipeline

    if observing:
        enable(env)
    replay_store = ReplayStore(env.root / "rights-replay.db")
    monkeypatch.setattr(replay, "store", replay_store)
    monkeypatch.setattr(replay, "enabled", lambda: replaying)
    gen, models = generator(monkeypatch)
    monkeypatch.setattr("src.chat.replyer.maisaka_generator.MaisakaReplyGenerator", lambda **kwargs: gen)
    status = DeliveryStatus[status_name]
    manager, calls, _ = pipeline(monkeypatch, status=status,
                                 pid="synthetic-ack" if status == DeliveryStatus.SENT else None)
    monkeypatch.setattr(send_service, "send_priority_at_reply", real_priority_send)
    def build(sequence, session_id, **kwargs):
        msg = outbound("temporary", session_id)
        msg.raw_message = sequence
        return msg
    monkeypatch.setattr(send_service, "_build_outbound_session_message", build)
    monkeypatch.setattr(send_service, "_sync_sent_message_to_maisaka_history", lambda *a, **k: None)
    async def via(message, **kwargs):
        batch = await manager.send_message(message, RouteKey("qq", "bot"))
        return message if batch.has_success else None
    monkeypatch.setattr(send_service, "_send_via_platform_io", via)
    try:
        await env.receiver.process_message(env.message("stable-source"))
        await settled(env.service)
        assert len(models) == len(calls) == 1
        await env.receiver.process_message(env.message("stable-source"))
        await settled(env.service)
        assert len(models) == len(calls) == 1
        if status == DeliveryStatus.UNKNOWN:
            oid = calls[0][1]
            assert manager.outbound_tracker.get_uncertain(oid, "mock") is not None
            ack = DeliveryReceipt(oid, RouteKey("qq", "bot"), DeliveryStatus.SENT,
                                  driver_id="mock", driver_kind=DriverKind.PLUGIN,
                                  external_message_id="synthetic-late-ack")
            reconciled = asyncio.Event()
            completed = 0
            original = replies.reconcile_receipt
            async def reconcile(receipt):
                nonlocal completed
                await original(receipt)
                completed += 1
                if completed == 2:
                    reconciled.set()
            monkeypatch.setattr(replies, "reconcile_receipt", reconcile)
            manager.outbound_tracker.finish_tracking(ack)
            manager.outbound_tracker.finish_tracking(ack)
            if replaying:
                await asyncio.wait_for(reconciled.wait(), 5)
            assert manager.outbound_tracker.get_uncertain(oid, "mock") is None
            await env.receiver.process_message(env.message("stable-source"))
            await settled(env.service)
            assert len(models) == len(calls) == 1
        assert not env.chats["group-a"].message_cache
        if replaying:
            assert replay_store.flush()
            counts = statistics(replay_store)["counts"]
            assert counts["logical_replies"] == counts["generation_requests"] == counts["send_attempts"] == 1
            assert counts["platform_messages"] == int(status != DeliveryStatus.FAILED)
        if observing:
            saved = read(env)
            assert len([r for r in saved["reply_events"] if r["stage"] == "generation" and r["status"] == "generation_started"]) == 1
            assert len([r for r in saved["reply_events"] if r["stage"] == "driver_send"]) == 1
            assert not saved["transfers"]
    finally:
        await asyncio.to_thread(replay_store.close)


@pytest.mark.asyncio
async def test_failed_normal_registration_does_not_reserve_a_phantom_owner(env):
    rt = env.chats["group-a"]
    msg = env.message()
    original = rt._update_message_trigger_state
    def broken(message):
        raise ValueError("synthetic registration fault")
    rt._update_message_trigger_state = broken
    with pytest.raises(ValueError):
        await MaisakaHeartFlowChatting.register_message(rt, msg)
    assert not rt.message_cache
    assert env.service._rights.get(message_key(msg)) is None
    rt._update_message_trigger_state = original
    await MaisakaHeartFlowChatting.register_message(rt, msg)
    assert rt.message_cache == [msg]


@pytest.mark.asyncio
async def test_event_identity_is_frozen_across_awaits_not_read_from_body(env):
    msg = env.message()
    captured = message_key(msg)
    msg.message_id = "mutated-during-context-build"
    msg.message_info.additional_config["account_id"] = "changed"
    assert message_key(msg) == captured
    assert message_key(env.message()) == captured


@pytest.mark.asyncio
async def test_mixed_batch_releases_ignored_unconsumed_right_without_changing_owner_rules(env, monkeypatch):
    from src.services.owner_admin import trigger_policy
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", False)
    ignored, eligible = env.message("ignored"), env.message("eligible")
    await env.receiver.process_message(ignored)
    await env.receiver.process_message(eligible)
    monkeypatch.setattr(trigger_policy, "ignore_rules", lambda: frozenset({("chat", "group-a", "synthetic")}))
    monkeypatch.setattr(trigger_policy, "is_ignored", lambda msg, **kwargs: msg.message_id == "ignored")
    assert MaisakaHeartFlowChatting._collect_pending_messages(env.chats["group-a"]) == [eligible]
    assert env.service._rights.get(message_key(ignored)) is None
    assert env.service._rights.get(message_key(eligible)).phase == "running"
    env.service._rights.finish_normal(env.chats["group-a"])
    assert not env.service._rights.active



@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_start", "generation", "send"])
async def test_accepted_cancellation_keeps_original_no_new_fallback(env, stage):
    entered = asyncio.Event()
    async def block(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    if stage == "generation":
        env.generate.side_effect = block
    elif stage == "send":
        env.send.side_effect = block
    msg = env.message()
    await env.receiver.process_message(msg)
    if stage != "before_start":
        await entered.wait()
    env.service._tasks[message_key(msg)].cancel()
    await settled(env.service)
    await env.receiver.process_message(env.message())
    await settled(env.service)
    assert env.generate.await_count == int(stage != "before_start")
    assert env.send.await_count == int(stage == "send")
    assert not env.chats["group-a"].message_cache
    assert env.service._rights.get(message_key(msg)).owner == "priority"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "failed", "exception"])
async def test_accepted_generation_failure_does_not_add_ordinary_retry(env, failure):
    if failure == "exception":
        env.generate.side_effect = ValueError("synthetic private exception")
    else:
        env.result.completion.response_text = "" if failure == "empty" else "unused"
        env.generate.return_value = (failure == "empty", env.result)
    msg = env.message()
    await env.receiver.process_message(msg)
    await settled(env.service)
    await env.receiver.process_message(env.message())
    await settled(env.service)
    assert env.generate.await_count == 1 and env.send.await_count == 0
    assert not env.chats["group-a"].message_cache


@pytest.mark.asyncio
async def test_shutdown_keeps_existing_discard_semantics_not_new_fallback(env):
    entered = asyncio.Event()
    async def block(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    env.generate.side_effect = block
    env.service._max_concurrent = 1
    await env.receiver.process_message(env.message("running"))
    await entered.wait()
    await env.receiver.process_message(env.message("queued"))
    await env.service.shutdown()
    assert not env.chats["group-a"].message_cache
    assert env.chats["group-a"]._chat_history == ["running", "queued"]
    assert env.generate.await_count == 1 and env.send.await_count == 0
    assert not env.service._rights.active and not env.service._seen


def test_two_threads_have_single_owner_and_stale_version_cannot_release_new():
    rights = ProcessingRights()
    key = ("g", "m", "qq", "bot")
    barrier = Barrier(2)
    def take(owner):
        barrier.wait()
        return rights.take(key, owner, "pending", object())
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(take, ["normal", "priority"]))
    assert sum(result is not None for result in results) == 1
    old = next(result for result in results if result is not None)
    rights.clear()
    new = rights.take(key, "normal", "pending", object())
    assert new != old
    assert not rights.release(old)
    rights.finish(old)
    assert rights.get(key).version == new[1] and rights.get(key).phase == "pending"


def test_capacity_2000_each_pins_active_and_separates_finished_quotas():
    rights = ProcessingRights()
    rt = object()
    active = rights.take(("g", "active", "qq", "bot"), "normal", "pending", rt)
    for owner in ("normal", "priority"):
        for i in range(2001):
            key = ("g", owner + str(i), "qq", "bot")
            rights.finish(rights.take(key, owner, "running", rt))
        assert rights.get(("g", owner + "0", "qq", "bot")) is None
        assert rights.get(("g", owner + "1", "qq", "bot")) is not None
    assert len(rights.normal) == len(rights.priority) == 2000
    assert rights.get(active[0]).phase == "pending"
    # Eviction deliberately ends the finite guarantee, not a delivery proof.
    assert rights.take(("g", "priority0", "qq", "bot"), "priority", "queued", rt) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
async def test_normal_active_right_survives_history_pruning_and_finished_pressure(env, monkeypatch, consumed):
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", False)
    rt = env.chats["group-a"]
    msg = env.message()
    await env.receiver.process_message(msg)
    if consumed:
        assert MaisakaHeartFlowChatting._collect_pending_messages(rt) == [msg]
    rt.message_cache.clear()
    rt._last_processed_index = 0
    rights = env.service._rights
    for i in range(2100):
        key = ("other", str(i), "qq", "bot")
        rights.finish(rights.take(key, "normal", "running", rt))
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", True)
    await env.receiver.process_message(env.message())
    assert env.generate.await_count == env.send.await_count == 0
    assert rights.get(message_key(msg)).phase == ("running" if consumed else "pending")


@pytest.mark.asyncio
async def test_unknown_survives_actual_cache_expiry_and_late_ack_without_new_execution(env, monkeypatch):
    from src.platform_io import dedupe, outbound_tracker
    from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverKind, RouteKey
    clock = [100.0]
    monkeypatch.setattr(dedupe, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(outbound_tracker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    inbound = dedupe.MessageDeduplicator()
    tracker = outbound_tracker.OutboundTracker()
    route = RouteKey("qq", "bot")
    assert inbound.mark_seen("source")
    tracker.begin_tracking("dispatch", route, "mock")
    tracker.finish_tracking(DeliveryReceipt("dispatch", route, DeliveryStatus.UNKNOWN, driver_id="mock"))
    env.send.side_effect = DeliveryUncertain("synthetic unresolved")
    msg = env.message()
    await env.receiver.process_message(msg)
    await settled(env.service)
    clock[0] += 1801
    assert inbound.mark_seen("source")
    assert tracker.get_uncertain("dispatch", "mock") is None
    await env.receiver.process_message(env.message())
    await settled(env.service)
    for _ in range(2):
        tracker.finish_tracking(DeliveryReceipt("dispatch", route, DeliveryStatus.SENT, driver_id="mock",
            driver_kind=DriverKind.PLUGIN, external_message_id="trusted-synthetic-ack"))
    await env.receiver.process_message(env.message())
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 1
    assert env.service._rights.get(message_key(msg)).phase == "retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("future_priority", [False, True])
async def test_old_message_remains_legal_future_context_and_reference(env, monkeypatch, future_priority):
    await env.receiver.process_message(env.message("old", text="same synthetic text"))
    await settled(env.service)
    assert env.chats["group-a"]._chat_history == ["old"]
    monkeypatch.setattr(global_config.chat, "priority_at_reply_enabled", future_priority)
    future = env.message("new", text="same synthetic text")
    future.message_info.additional_config["synthetic_quote_of"] = "old"
    await env.receiver.process_message(future)
    await settled(env.service)
    if future_priority:
        assert env.generate.await_count == 2
        assert env.generate.await_args.kwargs["chat_history"] == ["old", "new"]
    else:
        rt = env.chats["group-a"]
        assert MaisakaHeartFlowChatting._collect_pending_messages(rt) == [future]
        assert rt._chat_history == ["old"]
    assert env.service.owns(env.message("old"))


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_finite_retention_eviction_and_service_restart_are_explicit_non_guarantees(env, unknown):
    if unknown:
        env.send.side_effect = DeliveryUncertain("synthetic retained but unresolved")
    env.service._rights = ProcessingRights(max_priority=1)
    for mid in ("old", "new", "old"):
        await env.receiver.process_message(env.message(mid))
        await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 3
    rights = env.service._rights
    assert ProcessingRights().get(message_key(env.message("old"))) is None
    rights.clear()  # Same-process service reset ends the guarantee too.
    await env.receiver.process_message(env.message("old"))
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("observing", [False, True])
async def test_partial_segment_success_unknown_and_late_ack_do_not_reexecute_source(env, monkeypatch, observing):
    from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverKind, RouteKey
    from test_delivery import pipeline
    from test_reply_flow import outbound
    if observing:
        enable(env)
    manager, calls, _ = pipeline(monkeypatch)
    driver = manager.resolve_drivers(RouteKey("qq", "bot"))[0]
    async def dispatch(*, message, route_key, metadata):
        calls.append(message.message_id)
        return DeliveryReceipt(message.message_id, route_key,
            DeliveryStatus.SENT if message.message_id == "segment-1" else DeliveryStatus.UNKNOWN,
            driver_id="mock", driver_kind=DriverKind.PLUGIN,
            external_message_id="synthetic-first" if message.message_id == "segment-1" else None)
    driver.send_message.side_effect = dispatch
    async def split_send(*args, **kwargs):
        for mid in ("segment-1", "segment-2"):
            batch = await manager.send_message(outbound(mid, "group-a"), RouteKey("qq", "bot"))
            if not batch.has_success:
                raise DeliveryUncertain("synthetic second segment unknown")
        return True
    env.send.side_effect = split_send
    await env.receiver.process_message(env.message("source"))
    await settled(env.service)
    await env.receiver.process_message(env.message("source"))
    await settled(env.service)
    assert calls == ["segment-1", "segment-2"]
    for _ in range(2):
        manager._outbound_tracker.finish_tracking(DeliveryReceipt("segment-2", RouteKey("qq", "bot"),
            DeliveryStatus.SENT, driver_id="mock", driver_kind=DriverKind.PLUGIN,
            external_message_id="synthetic-late-second"))
    await env.receiver.process_message(env.message("source"))
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 1
    assert calls == ["segment-1", "segment-2"]
    assert not env.chats["group-a"].message_cache


def test_fresh_interpreter_does_not_inherit_rights_explicit_restart_boundary():
    from pathlib import Path
    import json
    import subprocess
    import sys
    rights = ProcessingRights()
    key = ("synthetic-group", "retained-source", "qq", "bot")
    rights.finish(rights.take(key, "priority", "running", object()))
    assert rights.get(key) is not None
    code = ("import socket; socket.socket.connect=lambda *a: (_ for _ in ()).throw(RuntimeError('network disabled')); "
            "from src.services.message_processing_rights import ProcessingRights; "
            "import json; print(json.dumps({'new_process_has_right': ProcessingRights().get("
            "('synthetic-group','retained-source','qq','bot')) is not None}))")
    result = subprocess.run([sys.executable, "-c", code], cwd=Path.cwd(), capture_output=True, text=True, check=True)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"new_process_has_right": False}


@pytest.mark.asyncio
async def test_late_priority_callback_cannot_pop_current_task_with_wrong_version(env):
    entered, release = asyncio.Event(), asyncio.Event()
    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return True, env.result
    env.generate.side_effect = generate
    msg = env.message()
    await env.receiver.process_message(msg)
    await entered.wait()
    key = message_key(msg)
    task = env.service._tasks[key]
    version = env.service._rights.get(key).version
    env.service._finish(key, task, (key, version - 1))
    assert env.service._tasks[key] is task
    assert env.service._rights.get(key).phase == "running"
    release.set()
    await settled(env.service)
    assert env.generate.await_count == env.send.await_count == 1
    assert env.service._rights.get(key).phase == "retained"


@pytest.mark.parametrize("stage", ["consume", "finish"])
def test_pending_batch_does_not_hold_global_lock_between_item_transitions(monkeypatch, stage):
    from src.services import message_processing_rights as module
    rights = ProcessingRights()
    messages = [SimpleNamespace(_processing_message_key=("g", str(i), "qq", "bot")) for i in range(2)]
    rt = SimpleNamespace(message_cache=messages, _last_processed_index=0)
    for message in messages:
        rights.take(message_key(message), "normal", "pending", rt)
    ready, release = ThreadEvent(), ThreadEvent()
    if stage == "consume":
        original = module.message_key
        calls = 0
        def key(message):
            nonlocal calls
            if message is messages[-1]:
                calls += 1
                if calls == 2:
                    ready.set()
                    assert release.wait(5)
            return original(message)
        monkeypatch.setattr(module, "message_key", key)
        def action():
            return rights.consume_normal(rt, messages)
    else:
        rights.consume_normal(rt, messages)
        original = rights.finish
        def finish(token):
            ready.set()
            assert release.wait(5)
            return original(token)
        monkeypatch.setattr(rights, "finish", finish)
        def action():
            return rights.finish_normal(rt)
    with ThreadPoolExecutor(2) as pool:
        batch = pool.submit(action)
        assert ready.wait(5)
        other = pool.submit(rights.take, ("other", "m", "qq", "bot"), "priority", "queued", object())
        try:
            assert other.result(timeout=3) is not None
        finally:
            release.set()
        batch.result(timeout=5)
