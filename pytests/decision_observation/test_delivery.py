"""Trusted receipt evidence is simulated explicitly; no QQ, network or paid models."""

from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio
import json
import sqlite3
import time

import pytest

from src.core.tooling import ToolInvocation
from src.decision_observation import delivery, recorder, reply_flow
from src.decision_observation.models import Batch, Policy, SCHEMA_VERSION
from src.decision_observation.store import ObservationStore
from src.decision_replay import recorder as replay, replies
from src.decision_replay.reply_metrics import statistics
from src.decision_replay.store import ReplayStore
from src.maisaka.builtin_tool import reply as reply_tool
from src.platform_io.drivers.plugin_driver import PluginPlatformDriver
from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverKind, RouteKey
from src.services import send_service
from test_priority_execution import enable, env as _env, read
from test_reply_flow import generator, ordinary, outbound, reply_context as _reply_context, tracker_for
import test_reply_flow as flow_tests

env = _env
reply_context = _reply_context
_original_pipeline = flow_tests.pipeline


def receipts(env, sid="group-a"):
    return read(env, sid)["delivery_receipts"]


def confirmed(env, sid="group-a"):
    return [r for r in receipts(env, sid) if r["attempt_conclusion"] == "platform_confirmed"]


def pipeline(monkeypatch, *, status=DeliveryStatus.SENT, pid="platform-id", metadata=None,
             behavior="returned", kind=DriverKind.PLUGIN, **unused):
    manager, calls, sent = _original_pipeline(monkeypatch)

    async def dispatch(*, message, route_key, metadata):
        calls.append((message.session_id, message.message_id, replies.classify(message), dict(metadata or {})))
        if behavior == "timeout":
            raise TimeoutError("private body must not be persisted")
        if behavior == "exception":
            raise RuntimeError("private body must not be persisted")
        if behavior == "cancel":
            raise asyncio.CancelledError()
        return DeliveryReceipt(message.message_id, route_key, status, driver_id="mock",
                               driver_kind=kind, external_message_id=pid(message) if callable(pid) else pid,
                               metadata=dict(evidence))

    evidence = metadata or {}
    driver = SimpleNamespace(driver_id="mock", descriptor=SimpleNamespace(kind=kind),
                             send_message=AsyncMock(side_effect=dispatch))
    monkeypatch.setattr(manager, "resolve_drivers", lambda route: [driver])
    return manager, calls, sent


async def send(env, manager, oid="out", sid="group-a", account="bot", tracker=None):
    tracker = tracker or tracker_for(env, env.chats[sid])
    with reply_flow.planner_scope(tracker, env.chats[sid], ToolInvocation("reply", call_id="call")):
        return await manager.send_message(outbound(oid, sid), RouteKey("qq", account))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,pid,meta,kind,state", [
    (DeliveryStatus.SENT, "qq-1", {}, DriverKind.PLUGIN, "platform_confirmed"),
    (DeliveryStatus.SENT, None, {}, DriverKind.PLUGIN, "unknown"),
    (DeliveryStatus.SENT, None, {"platform_acknowledged": True}, DriverKind.PLUGIN, "unknown"),
    (DeliveryStatus.SENT, None, {"delivery_id": "d1"}, DriverKind.PLUGIN, "unknown"),
    (DeliveryStatus.SENT, None, {"delivery_id": "d1", "platform_acknowledged": True}, DriverKind.PLUGIN, "platform_confirmed"),
    (DeliveryStatus.FAILED, None, {}, DriverKind.PLUGIN, "unknown"),
    (DeliveryStatus.FAILED, None, {"retcode": 1200}, DriverKind.PLUGIN, "unknown"),
    (DeliveryStatus.UNKNOWN, "untrusted-id", {}, DriverKind.PLUGIN, "unknown"),
    (DeliveryStatus.DROPPED, None, {}, DriverKind.PLUGIN, "unknown"),
    (DeliveryStatus.SENT, "local-only", {}, DriverKind.LOCAL, "not_applicable"),
])
async def test_receipt_evidence_is_distinct_from_driver_report(env, monkeypatch, status, pid, meta, kind, state):
    enable(env)
    manager, calls, _ = pipeline(monkeypatch, status=status, pid=pid, metadata=meta, kind=kind)
    batch = await send(env, manager)
    assert len(calls) == 1 and batch.receipts[0].status == status
    saved = receipts(env)
    assert len(saved) == 1 and saved[0]["driver_status"] == status.value
    assert saved[0]["attempt_conclusion"] == state and saved[0]["link_count"] == 1
    assert saved[0]["platform_confirmed_at"] is None
    assert saved[0]["recorded_at"] >= saved[0]["observed_at"]
    assert "private body" not in json.dumps(saved)


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["timeout", "exception", "cancel"])
async def test_uncertain_exceptions_never_become_failure_or_retry(env, monkeypatch, behavior):
    enable(env)
    manager, calls, _ = pipeline(monkeypatch, behavior=behavior)
    if behavior == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await send(env, manager)
    else:
        batch = await send(env, manager)
        assert batch.receipts[0].status == DeliveryStatus.UNKNOWN
    assert len(calls) == 1
    assert receipts(env)[0]["attempt_conclusion"] == "unknown"
    assert manager._outbound_tracker.get_uncertain("out", "mock") is not None


@pytest.mark.asyncio
async def test_real_reply_generation_bubbles_attachment_and_platform_evidence(env, reply_context, monkeypatch):
    enable(env)
    replay_store = ReplayStore(env.root / "replay.db")
    monkeypatch.setattr(replay, "store", replay_store)
    monkeypatch.setattr(replay, "enabled", lambda: True)
    gen, models = generator(monkeypatch)
    rt = ordinary(env, reply_context, monkeypatch, gen)
    manager, calls, _ = pipeline(monkeypatch, pid=lambda message: "platform:" + message.message_id)
    attachment = False

    async def attach(sid, tag, text):
        nonlocal attachment
        if not attachment:
            attachment = True
            await send_service.send_session_message_with_message(outbound("attached-emoji", sid, "emoji"))

    monkeypatch.setattr("src.emoji_system.reply_attachment.send_after_text", attach)
    tracker = tracker_for(env)
    with replay.bind(replay.new_trace("group-a", "planner", turn_id=tracker.logical_turn_id)):
        replay.cycle(1)
        with reply_flow.planner_scope(tracker, rt, ToolInvocation("reply", call_id="reply-call")):
            assert (await reply_tool.handle_tool(reply_context, ToolInvocation("reply", call_id="reply-call",
                                     arguments={"msg_id": "msg-1"}))).success
    assert len(models) == 1 and len(calls) == 3 and len(confirmed(env)) == 3
    assert replay_store.flush()
    assert statistics(replay_store)["counts"]["send_attempts"] == 3
    assert statistics(replay_store)["counts"]["platform_messages"] == 3
    assert len({r["attempt_id"] for r in confirmed(env)}) == 3
    (env.root / "delivery-complete.json").write_text(json.dumps(read(env), indent=2), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "emoji"])
async def test_priority_text_and_fixed_emoji_platform_evidence(env, monkeypatch, kind):
    enable(env)
    gen, models = generator(monkeypatch)
    monkeypatch.setattr("src.chat.replyer.maisaka_generator.MaisakaReplyGenerator", lambda **kwargs: gen)
    monkeypatch.setattr("src.services.priority_at_reply_service.random.random", lambda: 0.0)
    manager, calls, _ = pipeline(monkeypatch)

    async def priority(sequence, message, **kwargs):
        msg = outbound("priority-out")
        msg.raw_message = sequence
        return await send_service.send_session_message_with_message(msg) is not None

    monkeypatch.setattr(send_service, "send_priority_at_reply", priority)
    await env.receiver.process_message(env.message(text="text" if kind == "text" else ""))
    await env.drain()
    assert len(calls) == len(confirmed(env)) == 1
    assert len(models) == (1 if kind == "text" else 0)
    saved = read(env)
    assert saved["outcomes"][0]["planner_status"] == "not_applicable"
    assert saved["delivery_links"][0]["execution_id"]


@pytest.mark.asyncio
async def test_late_unknown_confirmation_duplicates_without_replay_or_live_context(env, monkeypatch):
    enable(env)
    manager, calls, _ = pipeline(monkeypatch, status=DeliveryStatus.UNKNOWN, pid=None)
    await send(env, manager)
    assert receipts(env)[0]["attempt_conclusion"] == "unknown"
    env.store.delivery_bindings.clear()  # Exercise the persisted join, not the memory shortcut.
    monkeypatch.setattr(replay, "store", SimpleNamespace(path=env.root / "expired-replay.db"))
    late = DeliveryReceipt("out", RouteKey("qq", "bot"), DeliveryStatus.SENT, driver_id="mock",
                           driver_kind=DriverKind.PLUGIN, external_message_id="late-platform-id")

    async def callback():
        manager._outbound_tracker.finish_tracking(late)

    await asyncio.create_task(callback())
    await asyncio.create_task(callback())
    saved = receipts(env)
    assert len(saved) == 2 and len(confirmed(env)) == 1
    assert saved[0]["attempt_id"] == saved[1]["attempt_id"]
    assert manager._outbound_tracker.get_uncertain("out", "mock") is None and len(calls) == 1
    (env.root / "delivery-late.json").write_text(json.dumps(read(env), indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_receipt_can_be_written_before_attempt_parent(env, monkeypatch):
    enable(env)
    original = env.store._write
    held = []
    monkeypatch.setattr(env.store, "_write", lambda batches: held.extend(batches))
    manager, calls, _ = pipeline(monkeypatch)
    await send(env, manager)
    assert env.store.flush()
    receipt_batches = [b for b in held if isinstance(b, Batch) and b.receipt is not None]
    others = [b for b in held if b not in receipt_batches]
    assert receipt_batches and others
    await asyncio.to_thread(original, receipt_batches)
    assert receipts(env)[0]["attempt_conclusion"] == "unknown"
    await asyncio.to_thread(original, others)
    assert len(confirmed(env)) == 1 and len(calls) == 1
    with sqlite3.connect(env.store.path) as c:
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    monkeypatch.setattr(env.store, "_write", original)


@pytest.mark.asyncio
async def test_multiple_attempts_do_not_assign_late_receipt_to_latest(env, monkeypatch):
    enable(env)
    manager, calls, _ = pipeline(monkeypatch, status=DeliveryStatus.FAILED, pid=None)
    tracker = tracker_for(env)
    await send(env, manager, tracker=tracker)
    await send(env, manager, tracker=tracker)
    assert len(calls) == 2 and len(receipts(env)) == 2
    late = DeliveryReceipt("out", RouteKey("qq", "bot"), DeliveryStatus.SENT, driver_id="mock",
                           driver_kind=DriverKind.PLUGIN, external_message_id="cannot-attribute")
    manager._outbound_tracker.finish_tracking(late)
    assert not confirmed(env)
    ambiguous = next(r for r in receipts(env) if r["ambiguous"])
    assert ambiguous["attempt_id"] is None and ambiguous["link_count"] == 0
    assert ambiguous["attempt_conclusion"] == "unknown"
    (env.root / "delivery-ambiguous.json").write_text(json.dumps(read(env), indent=2), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["disable", "revoke", "delete"])
async def test_revocation_deletion_and_reenable_do_not_allow_late_writeback(env, monkeypatch, action):
    enable(env)
    manager, _, _ = pipeline(monkeypatch, status=DeliveryStatus.UNKNOWN, pid=None)
    await send(env, manager)
    assert len(receipts(env)) == 1
    if action == "delete":
        await asyncio.to_thread(env.store.delete_session, "group-a")
    else:
        await asyncio.to_thread(env.store.configure, Policy(action != "disable", frozenset({"group-b"})))
        enable(env)  # Re-enabling never reactivates old collection scopes.
    manager._outbound_tracker.finish_tracking(DeliveryReceipt(
        "out", RouteKey("qq", "bot"), DeliveryStatus.SENT, driver_id="mock",
        driver_kind=DriverKind.PLUGIN, external_message_id="forbidden-late"))
    assert not confirmed(env)
    assert len(receipts(env)) == (0 if action == "delete" else 1)


@pytest.mark.asyncio
async def test_many_opportunities_share_one_attempt_without_double_count(env, monkeypatch):
    enable(env)
    tracker = tracker_for(env)
    env.chats["group-a"]._observation_workset.clear()
    tracker.cycle_references += tracker_for(env, mid="m2").cycle_references
    manager, calls, _ = pipeline(monkeypatch)
    await send(env, manager, tracker=tracker)
    saved = read(env)
    assert len(calls) == len(confirmed(env)) == 1
    assert len(saved["delivery_links"]) == 2
    assert len({r["opportunity_id"] for r in saved["delivery_links"]}) == 2


@pytest.mark.asyncio
async def test_two_accounts_concurrent_and_wrong_route_cannot_cross_link(env, monkeypatch):
    enable(env, "group-a", "group-b")
    env.runtime("group-b", "bot-two")
    manager, calls, _ = pipeline(monkeypatch)
    await asyncio.gather(send(env, manager, "a", "group-a", "bot"),
                         send(env, manager, "b", "group-b", "bot-two"))
    assert len(calls) == 2 and len(confirmed(env)) == len(confirmed(env, "group-b")) == 1
    env.store.delivery_bindings.clear()
    for route in (RouteKey("qq", "wrong"), RouteKey("other", "bot"), RouteKey("qq", "bot", "wrong-scope")):
        delivery.receipt_observed(DeliveryReceipt("a", route, DeliveryStatus.SENT, driver_id="mock",
                                   driver_kind=DriverKind.PLUGIN, external_message_id="wrong"))
    assert len(confirmed(env)) == len(confirmed(env, "group-b")) == 1


@pytest.mark.asyncio
async def test_redelivery_single_owner_keeps_its_actual_receipt(env, reply_context, monkeypatch):
    monkeypatch.setattr(flow_tests, "pipeline", pipeline)
    await flow_tests.test_redelivery_has_single_real_generation_and_send_owner(env, reply_context, monkeypatch)
    assert len(confirmed(env)) == 3
    saved = read(env)
    transfer = saved["transfers"][0]
    linked = [link for link in saved["delivery_links"] if link["opportunity_id"] in
              {transfer["source_opportunity_id"], transfer["target_opportunity_id"]}]
    assert len(linked) == 1 and len({link["execution_id"] for link in linked}) == 1


@pytest.mark.asyncio
async def test_actual_gateway_preflight_rejection_is_definitive_without_rpc(env, monkeypatch):
    enable(env)
    supervisor = SimpleNamespace(invoke_message_gateway=AsyncMock())
    driver = PluginPlatformDriver("disabled", "qq", supervisor, "send", supports_send=False,
                                  account_id="bot", plugin_id="mock")
    manager, _, _ = pipeline(monkeypatch)
    monkeypatch.setattr(manager, "resolve_drivers", lambda route: [driver])
    await send(env, manager)
    supervisor.invoke_message_gateway.assert_not_awaited()
    assert receipts(env)[0]["attempt_conclusion"] == "definitive_failure"
    assert receipts(env)[0]["evidence_strength"] == "gateway_send_disabled"


@pytest.mark.asyncio
async def test_schema_four_migration_keeps_history_without_backfill(env, monkeypatch):
    enable(env)
    manager, _, _ = pipeline(monkeypatch)
    await send(env, manager)
    assert receipts(env)
    assert await asyncio.to_thread(env.store.close)
    with sqlite3.connect(env.store.path) as c:
        before = c.execute("SELECT * FROM evaluations").fetchall()
        c.execute("UPDATE reply_events SET detail=json_remove(detail,'$.collection_scope_id','$.platform','$.route_scope')")
        old_events = c.execute("SELECT * FROM reply_events").fetchall()
        for table in ("delivery_links", "delivery_receipts", "delivery_scopes"):
            c.execute("DROP TABLE " + table)
        c.execute("DROP TABLE annotation_revisions")
        c.execute("DROP TABLE annotation_targets")
        c.execute("PRAGMA user_version=4")
    assert read(env)["delivery_receipts"] == []
    assert read(env)["delivery_schema_available"] is False
    assert read(env)["delivery_receipt_count"] is None
    restored = ObservationStore(env.store.path)
    monkeypatch.setattr(recorder, "store", restored)
    try:
        connection = restored._connect()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT * FROM evaluations").fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM delivery_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT * FROM reply_events").fetchall() == old_events
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        connection.close()
        assert restored.query("group-a")["delivery_attempt_states"][0]["status"] == "not_observed"
    finally:
        assert restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["off", "on", "write", "adapter"])
async def test_production_counts_and_metrics_unchanged_under_observation_faults(env, reply_context, monkeypatch, fault):
    if fault != "off":
        enable(env)
    replay_store = ReplayStore(env.root / "metrics.db")
    monkeypatch.setattr(replay, "store", replay_store)
    monkeypatch.setattr(replay, "enabled", lambda: True)
    if fault == "write":
        monkeypatch.setattr(env.store, "_write", Mock(side_effect=sqlite3.OperationalError("injected")))
    if fault == "adapter":
        monkeypatch.setattr(delivery, "_snapshot", Mock(side_effect=RuntimeError("injected")))
    gen, models = generator(monkeypatch)
    rt = ordinary(env, reply_context, monkeypatch, gen)
    manager, calls, sent = pipeline(monkeypatch)
    tracker = tracker_for(env)
    with reply_flow.planner_scope(tracker, rt, ToolInvocation("reply", call_id="same")):
        result = await reply_tool.handle_tool(reply_context, ToolInvocation("reply", call_id="same",
                                                     arguments={"msg_id": "msg-1"}))
    assert result.success and len(models) == 1 and len(calls) == len(sent) == 2
    assert replay_store.flush() and env.store.flush()
    counts = statistics(replay_store)["counts"]
    assert counts["send_attempts"] == 2 and counts["generation_requests"] == 1
    assert counts["logical_replies"] == counts["platform_messages"] == 1
    assert counts["send_unknowns"] == counts["send_failures"] == 0
    evidence = dict(mode=fault, model_calls=len(models), replyer_parameters=gen.observed_parameters,
                    sends=sent, metrics=counts, planner_reply_count=rt.record_planner_reply.call_count,
                    network_confirmation_requests=0, retries=0)
    (env.root / ("delivery-parity-" + fault + ".json")).write_text(json.dumps(evidence, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_bounded_queue_slow_writer_and_receipt_capture_performance(env, monkeypatch):
    enable(env)
    manager, calls, _ = pipeline(monkeypatch, status=DeliveryStatus.UNKNOWN, pid=None)
    await send(env, manager)
    assert env.store.flush()
    env.store.configure(Policy(True, frozenset({"group-a"}), queue_capacity=1))
    entered, release = Event(), Event()
    original = env.store._write

    def blocked(batches):
        entered.set()
        release.wait(3)
        original(batches)

    monkeypatch.setattr(env.store, "_write", blocked)
    late = DeliveryReceipt("out", RouteKey("qq", "bot"), DeliveryStatus.SENT, driver_id="mock",
                           driver_kind=DriverKind.PLUGIN, external_message_id="late")
    durations = []
    try:
        delivery.receipt_observed(late)
        assert await asyncio.to_thread(entered.wait, 1)
        for _ in range(100):
            started = time.perf_counter_ns()
            delivery.receipt_observed(late)
            durations.append((time.perf_counter_ns() - started) / 1000)
        # A real second driver call still executes while the observation writer is blocked.
        await asyncio.wait_for(send(env, manager, oid="second"), 1)
        assert len(calls) == 2 and env.store.dropped > 0
    finally:
        release.set()
    assert await asyncio.to_thread(env.store.flush)
    durations.sort()
    (env.root / "delivery-performance.json").write_text(json.dumps(dict(
        samples=100, median_us=durations[50], p95_us=durations[95], max_us=max(durations),
        dropped=env.store.dropped, queue_bound=1), indent=2), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,state", [
    ({"success": True, "result": {"success": True, "external_message_id": "napcat-id",
                                  "metadata": {"action": "send_group_msg"}}}, "platform_confirmed"),
    ({"success": True, "result": {"success": True, "external_message_id": "snowluma-id",
                                  "metadata": {"action": "send_msg"}}}, "platform_confirmed"),
    ({"success": True, "result": {"success": False, "error": "unconfirmed, may arrive later",
                                  "metadata": {"action": "send_msg"}}}, "unknown"),
    ({"success": False, "result": "RPC error"}, "unknown"),
    ({"success": True, "result": {"success": True}}, "unknown"),
])
async def test_real_plugin_driver_rpc_contract_has_no_extra_confirmation_call(env, monkeypatch, payload, state):
    from src.plugin_runtime.host.message_utils import PluginMessageUtils

    enable(env)
    supervisor = SimpleNamespace(invoke_message_gateway=AsyncMock(return_value=SimpleNamespace(error=None, payload=payload)))
    driver = PluginPlatformDriver("qq-gateway", "qq", supervisor, "send", supports_send=True,
                                  account_id="bot", plugin_id="adapter-mock")
    monkeypatch.setattr(PluginMessageUtils, "_session_message_to_dict", lambda m: {"message_id": m.message_id})
    manager, _, _ = pipeline(monkeypatch)
    monkeypatch.setattr(manager, "resolve_drivers", lambda route: [driver])
    await send(env, manager)
    assert supervisor.invoke_message_gateway.await_count == 1
    assert receipts(env)[0]["attempt_conclusion"] == state
    assert supervisor.invoke_message_gateway.call_args.kwargs["component_name"] == "send"


@pytest.mark.asyncio
async def test_persisted_observation_can_reconcile_after_store_restart_without_replay(env, monkeypatch):
    enable(env)
    manager, _, _ = pipeline(monkeypatch, status=DeliveryStatus.UNKNOWN, pid=None)
    await send(env, manager)
    assert receipts(env)[0]["attempt_conclusion"] == "unknown"
    assert await asyncio.to_thread(env.store.close)
    restored = ObservationStore(env.store.path)
    monkeypatch.setattr(recorder, "store", restored)
    restored.configure(Policy(True, frozenset({"group-a"}), 30, 4096))
    try:
        delivery.receipt_observed(DeliveryReceipt("out", RouteKey("qq", "bot"), DeliveryStatus.SENT,
                                   driver_id="mock", driver_kind=DriverKind.PLUGIN, external_message_id="after-restart"))
        assert await asyncio.to_thread(restored.flush)
        saved = restored.query("group-a", 200)
        assert len([r for r in saved["delivery_receipts"] if r["attempt_conclusion"] == "platform_confirmed"]) == 1
        assert saved["delivery_attempt_states"][0]["status"] == "platform_confirmed"
    finally:
        assert await asyncio.to_thread(restored.close)


@pytest.mark.asyncio
async def test_failed_revocation_write_still_blocks_current_process_late_callbacks(env, monkeypatch):
    from src.decision_observation import delivery_store

    enable(env)
    manager, _, _ = pipeline(monkeypatch, status=DeliveryStatus.UNKNOWN, pid=None)
    await send(env, manager)
    assert receipts(env)
    original = delivery_store.revoke
    monkeypatch.setattr(delivery_store, "revoke", Mock(side_effect=sqlite3.OperationalError("injected")))
    await asyncio.to_thread(env.store.configure, Policy(False))
    monkeypatch.setattr(delivery_store, "revoke", original)
    enable(env)
    delivery.receipt_observed(DeliveryReceipt("out", RouteKey("qq", "bot"), DeliveryStatus.SENT,
                               driver_id="mock", driver_kind=DriverKind.PLUGIN, external_message_id="old-scope"))
    assert not confirmed(env)


@pytest.mark.asyncio
async def test_positive_confirmation_is_not_overwritten_by_later_unknown(env, monkeypatch):
    enable(env)
    manager, _, _ = pipeline(monkeypatch)
    await send(env, manager)
    assert len(confirmed(env)) == 1
    manager._outbound_tracker.finish_tracking(DeliveryReceipt("out", RouteKey("qq", "bot"), DeliveryStatus.UNKNOWN,
                                              driver_id="mock", driver_kind=DriverKind.PLUGIN))
    saved = read(env)
    assert {r["driver_status"] for r in saved["delivery_receipts"]} == {"sent", "unknown"}
    assert saved["delivery_attempt_states"][0]["status"] == "platform_confirmed"


@pytest.mark.asyncio
async def test_disabled_receipts_never_create_database_and_cache_is_bounded(env, monkeypatch):
    late = DeliveryReceipt("out", RouteKey("qq", "bot"), DeliveryStatus.SENT,
                           driver_id="mock", driver_kind=DriverKind.PLUGIN, external_message_id="id")
    delivery.receipt_observed(late)
    assert not env.store.path.exists()
    assert read(env)["delivery_attempt_states"] == []
    assert read(env)["delivery_schema_available"] is False
    enable(env)
    tracker = tracker_for(env)
    driver = SimpleNamespace(driver_id="mock", descriptor=SimpleNamespace(kind=DriverKind.PLUGIN))
    with reply_flow.planner_scope(tracker, env.chats["group-a"], ToolInvocation("reply")):
        for index in range(4100):
            delivery.begin(outbound("out-" + str(index)), RouteKey("qq", "bot"), str(index), driver)
    assert len(env.store.delivery_bindings) == 4096
    assert not confirmed(env)


@pytest.mark.asyncio
async def test_existing_reconcile_entry_records_evidence_without_extra_replay_counts(env, monkeypatch):
    enable(env)
    replay_store = ReplayStore(env.root / "reconcile-replay.db")
    monkeypatch.setattr(replay, "store", replay_store)
    monkeypatch.setattr(replay, "enabled", lambda: True)
    manager, calls, _ = pipeline(monkeypatch, status=DeliveryStatus.UNKNOWN, pid=None)
    await send(env, manager)
    assert receipts(env)[0]["attempt_conclusion"] == "unknown"
    env.store.delivery_bindings.clear()
    late = DeliveryReceipt("out", RouteKey("qq", "bot"), DeliveryStatus.SENT, driver_id="mock",
                           driver_kind=DriverKind.PLUGIN, external_message_id="reconciled-platform")
    await replies.reconcile_receipt(late)
    await replies.reconcile_receipt(late)
    assert len(calls) == len(confirmed(env)) == 1
    assert replay_store.flush()
    counts = statistics(replay_store)["counts"]
    assert counts["send_attempts"] == counts["platform_messages"] == 1
    assert counts["send_unknowns"] == 1
