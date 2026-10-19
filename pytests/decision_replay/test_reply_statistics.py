"""Deterministic acknowledgement, replay and window checks; never contact QQ."""
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.decision_replay import recorder, replies
from src.decision_replay.reply_metrics import statistics
from src.decision_replay.store import ReplayStore
from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverKind, RouteKey
from src.webui.dependencies import require_auth
from src.webui.routers import decision_replay as api


@pytest.fixture
def capture(tmp_path, monkeypatch):
    store = ReplayStore(tmp_path / "replay.db")
    monkeypatch.setattr(recorder, "store", store)
    monkeypatch.setattr(recorder, "enabled", lambda: True)
    monkeypatch.setattr(api, "store", store)
    yield store
    store.flush()


def message(oid="m", kind="text", session="g", group=True):
    from src.common.data_models.message_component_data_model import EmojiComponent, ImageComponent, MessageSequence, TextComponent
    parts = [TextComponent("mock")] if kind == "text" else [EmojiComponent(binary_hash="mock", binary_data=b"x")]
    if kind == "mixed":
        parts = [TextComponent("mock"), ImageComponent(binary_hash="mock", binary_data=b"x")]
    return SimpleNamespace(message_id=oid, session_id=session, platform="test", is_at=False,
        message_info=SimpleNamespace(group_info=SimpleNamespace(group_id="g") if group else None, additional_config={}),
        raw_message=MessageSequence(parts))


async def deliver(msg, status=DeliveryStatus.SENT, pid="p", attempt="a"):
    route = RouteKey("test", "bot")
    await replies.emit("send_attempt", attempt, detail={"outbound_message_id": replies.outbound_id(msg),
        "attempt_id": attempt, "content_type": replies.classify(msg)})
    result = DeliveryReceipt(msg.message_id, route, status, driver_id="mock", driver_kind=DriverKind.PLUGIN,
                             external_message_id=pid)
    await replies.receipt(msg, route, result, attempt)
    return result


@pytest.mark.asyncio
async def test_three_bubbles_and_one_emoji_one_logical_reply(capture):
    async with replies.action("g", "planner", "group", reply_id="r"):
        await replies.emit("generation_request", "req", detail={"generation_request_id": "req"})
        await replies.emit("generation_success", "gen", detail={"generation_group_id": "gen"})
        for i, kind in enumerate(["text", "text", "text", "emoji"]):
            msg = message(str(i), kind)
            await replies.plan(msg, optional=kind == "emoji")
            await deliver(msg, pid=f"p{i}", attempt=f"a{i}")
    result = statistics(capture)
    assert result["counts"] == dict(generation_requests=1, generation_successes=1, logical_replies=1,
        successful_replies=1, partial_replies=0, text_messages=3, emoji_messages=1, platform_messages=4,
        send_attempts=4, send_failures=0, send_skips=0, send_unknowns=0)
    assert result["replies"][0]["state"] == "all_success"
    assert len(result["replies"][0]["messages"]) == 4


@pytest.mark.asyncio
async def test_optional_emoji_cooldown_skip_is_not_reply_failure(capture):
    async with replies.action("g", "priority_at", "group", reply_id="r"):
        text, emoji = message("t"), message("e", "emoji")
        await replies.plan(text)
        await replies.plan(emoji, optional=True)
        await deliver(text)
        await replies.skip(emoji, "cooldown_blocked")
    result = statistics(capture)
    assert result["counts"]["text_messages"] == 1 and result["counts"]["emoji_messages"] == 0
    assert result["counts"]["send_failures"] == 0 and result["counts"]["send_skips"] == 1
    assert result["replies"][0]["state"] == "all_success"
    assert result["replies"][0]["messages"][1]["reason"] == "cooldown_blocked"


@pytest.mark.asyncio
async def test_retry_and_duplicate_callback_replay_count_success_once(capture):
    async with replies.action("g", "planner", "private", reply_id="r"):
        msg = message()
        await replies.plan(msg)
        await deliver(msg, DeliveryStatus.FAILED, None, "failed")
        await deliver(msg, pid="platform", attempt="retry")
        await replies.receipt(msg, RouteKey("test", "bot"),
            DeliveryReceipt("m", RouteKey("test", "bot"), DeliveryStatus.SENT, driver_kind=DriverKind.PLUGIN,
                            external_message_id="platform"), "retry")
    with closing(capture.connect()) as c:
        original = [dict(row) for row in c.execute("SELECT * FROM events")]
    replay = [{**row, "detail": json.loads(row["detail"]), "message_ids": json.loads(row["message_ids"]), "source": "planner"} for row in original]
    capture.write_batch(replay)
    restored = ReplayStore(capture.path)
    counts = statistics(restored)["counts"]
    assert counts["send_attempts"] == 2 and counts["send_failures"] == 1
    assert counts["platform_messages"] == counts["successful_replies"] == 1
    assert counts["logical_replies"] == 1


@pytest.mark.asyncio
async def test_generation_without_action_or_send_is_not_logical_reply(capture):
    @recorder.traced_replyer
    async def generate(**kwargs):
        await replies.emit("generation_request", "req", detail={"generation_request_id": "req"})
        return True, SimpleNamespace(text_fragments=["mock"], selected_emoji_tag="", completion=SimpleNamespace(model_name="mock"))
    await generate(stream_id="g")
    capture.flush()
    result = statistics(capture)
    assert result["counts"]["generation_successes"] == result["counts"]["generation_requests"] == 1
    assert result["counts"]["logical_replies"] == result["counts"]["platform_messages"] == 0


@pytest.mark.asyncio
async def test_partial_plan_never_declared_fully_successful(capture):
    async with replies.action("g", "planner", "group", reply_id="r"):
        msgs = [message(str(i)) for i in range(3)]
        for msg in msgs:
            await replies.plan(msg)
        await deliver(msgs[0])
        await deliver(msgs[1], DeliveryStatus.FAILED, None, "fail")
        assert statistics(capture)["replies"][0]["state"] == "awaiting_confirmation"
    result = statistics(capture)
    assert result["replies"][0]["state"] == "partial_success"
    assert result["counts"]["partial_replies"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source,group,system,kind", [("priority_at", True, False, "text"),
    ("planner", True, False, "text"), ("planner", False, False, "text"),
    ("silence_emoji", True, False, "emoji"), ("auto_repeat", True, False, "text"),
    ("independent_send", False, False, "emoji"), ("system", False, True, "text")])
async def test_path_classification_and_system_exclusion(capture, source, group, system, kind):
    async with replies.action("g", source, "group" if group else "private", system=system):
        msg = message(kind=kind, group=group)
        await replies.plan(msg)
        await deliver(msg)
    result = statistics(capture, source=source, session_type="group" if group else "private", system=system)
    assert result["counts"]["platform_messages"] == 1
    assert result["counts"]["logical_replies"] == int(not system)
    assert result["counts"]["successful_replies"] == int(not system)


@pytest.mark.asyncio
async def test_mixed_platform_message_counts_once(capture):
    async with replies.action("g", "planner", "group"):
        msg = message(kind="mixed")
        await replies.plan(msg)
        await deliver(msg)
    result = statistics(capture)
    assert result["counts"]["text_messages"] == 1 and result["counts"]["emoji_messages"] == 0
    assert result["counts"]["platform_messages"] == 1


@pytest.mark.asyncio
async def test_unknown_after_restart_blocked_then_late_ack_cross_window(capture):
    async with replies.action("g", "planner", "group", reply_id="r"):
        msg = message()
        await replies.plan(msg)
        await deliver(msg, DeliveryStatus.UNKNOWN, None, "a")
    assert await replies.unresolved(msg)
    before = time.time()
    async with replies.action("g", "planner", "group", reply_id="r"):
        result = DeliveryReceipt("m", RouteKey("test", "bot"), DeliveryStatus.SENT,
                                driver_kind=DriverKind.PLUGIN, external_message_id="late")
        await replies.receipt(msg, RouteKey("test", "bot"), result, "a")
    restored = ReplayStore(capture.path)
    first = statistics(restored, start=0, end=before)
    later = statistics(restored, start=before, end=time.time()+1)
    assert first["counts"]["logical_replies"] == 1 and first["counts"]["platform_messages"] == 0
    assert later["counts"]["logical_replies"] == 0 and later["counts"]["platform_messages"] == 1
    assert first["replies"][0]["state"] == "all_success"
    assert first["counts"]["send_unknowns"] == 1 and first["counts"]["send_failures"] == 0


def test_half_open_window_and_legacy_unknown(capture):
    base = dict(event_id="x", timestamp=time.time(), session_id="g", logical_turn_id="d", source="planner",
        cycle_id=None, request_id=None, message_ids=[], stage="reply_stats", event_type="reply_stats.generation_request",
        decision="started", reason_code="", detail={"reply_id":"r","source":"planner"}, duration_ms=None,schema_version=2)
    capture.write_batch([base])
    a = statistics(capture, end=base["timestamp"])
    b = statistics(capture, start=base["timestamp"], end=base["timestamp"]+1)
    assert a["counts"]["generation_requests"] == 0 and b["counts"]["generation_requests"] == 1
    legacy = {**base, "event_id":"legacy", "event_type":"send.completed", "schema_version":1}
    capture.write_batch([legacy])
    assert statistics(capture)["legacy_event_count"] == 1
    assert statistics(capture)["counts"]["platform_messages"] == 0


@pytest.mark.asyncio
async def test_panel_export_and_trace_use_same_projection_and_auth(capture):
    async with replies.action("g", "planner", "group", reply_id="api-reply"):
        msg = message()
        await replies.plan(msg)
        await deliver(msg)
    app = FastAPI()
    app.include_router(api.router)
    async def auth(): return True
    app.dependency_overrides[require_auth] = auth
    with TestClient(app) as client:
        query = "?session_id=g&start=0&end=9999999999&source=planner"
        a = client.get("/api/webui/decision-replay/statistics"+query)
        b = client.get("/api/webui/decision-replay/statistics/export"+query)
        assert a.status_code == b.status_code == 200 and a.json() == b.json()
        assert a.json()["counts"]["platform_messages"] == 1
        decision_id = a.json()["all_replies"][0]["decision_id"]
        detail = client.get(f"/api/webui/decision-replay/{decision_id}?session_id=g").json()
        assert detail["reply_statistics"][0]["reply_id"] == "api-reply"
        assert {r["event_id"] for r in detail["metric_records"]} == {r["event_id"] for r in a.json()["records"]}
        assert a.json()["window"]["boundary"] == "[start,end)"
        assert client.get("/api/webui/decision-replay/statistics?start=2&end=1").status_code == 422
        app.dependency_overrides.clear()
        assert client.get("/api/webui/decision-replay/statistics").status_code == 401


@pytest.mark.asyncio
async def test_native_generation_requests_include_retries_and_exclude_planner(capture):
    request = SimpleNamespace(trace_context=SimpleNamespace(request_type="priority_at.replyer", request_id="real-request", attempt=1))
    async with replies.generation_scope("g", "gen"):
        await replies.generation_request(request, "replyer", "g")
        await replies.generation_request(request, "replyer", "g")  # duplicate callback
        request.trace_context.attempt = 2
        await replies.generation_request(request, "replyer", "g")
        request.trace_context.request_type = "maisaka.planner"
        await replies.generation_request(request, "planner", "g")
    assert statistics(capture)["counts"]["generation_requests"] == 2


@pytest.mark.asyncio
async def test_real_broker_tracks_driver_retry_success_and_prevents_unknown_resubmit(capture, monkeypatch):
    from src.platform_io.manager import PlatformIOManager
    manager = PlatformIOManager()
    route = RouteKey("test", "bot")
    msg = message()
    driver = SimpleNamespace(driver_id="mock", descriptor=SimpleNamespace(kind=DriverKind.PLUGIN), send_message=AsyncMock())
    driver.send_message.side_effect = [DeliveryReceipt("m", route, DeliveryStatus.FAILED, driver_kind=DriverKind.PLUGIN, driver_id="mock"),
        DeliveryReceipt("m", route, DeliveryStatus.SENT, driver_kind=DriverKind.PLUGIN, driver_id="mock", external_message_id="ack")]
    monkeypatch.setattr(manager, "resolve_drivers", lambda key: [driver])
    assert not (await manager.send_message(msg, route)).has_success
    assert (await manager.send_message(msg, route)).has_success
    assert (await manager.send_message(msg, route)).has_success
    assert driver.send_message.await_count == 2
    assert statistics(capture)["counts"]["platform_messages"] == 1
    other = message("unknown")
    driver.send_message.side_effect = TimeoutError()
    await manager.send_message(other, route)
    restarted = PlatformIOManager()
    monkeypatch.setattr(restarted, "resolve_drivers", lambda key: [driver])
    result = await restarted.send_message(other, route)
    assert result.receipts[0].status == DeliveryStatus.UNKNOWN
    assert driver.send_message.await_count == 3
    assert statistics(capture)["counts"]["send_unknowns"] == 1


@pytest.mark.asyncio
async def test_retention_keeps_safety_keys_and_never_recounts_old_callbacks(capture):
    async with replies.action("g", "planner", "group", reply_id="retained"):
        msg = message("retained-message")
        await replies.plan(msg)
        result = await deliver(msg, pid="retained-platform", attempt="retained-attempt")
    capture.cleanup(now=time.time() + 9 * 86400)
    assert statistics(capture)["counts"]["platform_messages"] == 0
    assert (await replies.unresolved(msg))["status"] == "sent"
    async with replies.action("g", "planner", "group", reply_id="retained"):
        await replies.receipt(msg, RouteKey("test", "bot"), result, "retained-attempt")
    assert statistics(capture)["counts"]["platform_messages"] == 0
    assert statistics(capture)["counts"]["successful_replies"] == 0


@pytest.mark.asyncio
async def test_late_receipt_reconciles_persisted_identity_without_new_action(capture):
    async with replies.action("g", "planner", "group", reply_id="late-reply"):
        msg = message("late-message")
        await replies.plan(msg)
        await deliver(msg, DeliveryStatus.UNKNOWN, None, "uncertain")
    result = DeliveryReceipt("late-message", RouteKey("test", "bot"), DeliveryStatus.SENT,
                            driver_kind=DriverKind.PLUGIN, external_message_id="late-confirmation")
    await replies.reconcile_receipt(result)
    await replies.reconcile_receipt(result)
    data = statistics(capture)
    assert data["counts"]["logical_replies"] == data["counts"]["platform_messages"] == 1
    assert data["replies"][0]["state"] == "all_success"
    assert data["counts"]["send_attempts"] == 1


@pytest.mark.asyncio
async def test_sent_without_platform_confirmation_stays_unknown(capture):
    async with replies.action("g", "independent_send", "private"):
        msg = message()
        await replies.plan(msg)
        await deliver(msg, DeliveryStatus.SENT, None, "transport-only")
    data = statistics(capture)
    assert data["counts"]["platform_messages"] == 0 and data["counts"]["send_unknowns"] == 1
    assert data["replies"][0]["state"] == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_real_cooldown_does_not_log_message_failure(capture, monkeypatch, caplog):
    from src.services import send_service
    from src.services.group_emoji_gate import group_emoji_gate
    msg = message("attachment", "emoji")
    monkeypatch.setattr(send_service, "_build_outbound_session_message", lambda **kwargs: msg)
    monkeypatch.setattr(send_service, "_invoke_send_hook", AsyncMock(return_value=(SimpleNamespace(aborted=False, kwargs={}), msg)))
    monkeypatch.setattr(send_service, "_prepare_message_for_platform_io", AsyncMock())
    manager = SimpleNamespace(ensure_send_pipeline_ready=AsyncMock(),
                             build_route_key_from_message=lambda message: RouteKey("test"), send_message=AsyncMock())
    monkeypatch.setattr(send_service, "get_platform_io_manager", lambda: manager)
    monkeypatch.setattr(group_emoji_gate, "acquire", lambda key: False)
    async with replies.action("g", "planner", "group"):
        result = await send_service._send_to_target_with_message(msg.raw_message, "g", maisaka_source_kind="reply_emoji_attachment")
    assert result is None
    assert "[SendService] 发送消息失败" not in caplog.text
    assert statistics(capture)["counts"]["send_skips"] == 1
    assert statistics(capture)["counts"]["send_failures"] == 0


@pytest.mark.asyncio
async def test_unknown_priority_emoji_does_not_fall_back_to_second_text(capture, monkeypatch):
    from src.services.priority_at_reply_service import PriorityAtReplyService
    service = PriorityAtReplyService()
    msg = message(kind="emoji")
    msg.message_id = "at"
    monkeypatch.setattr(service, "_is_standalone_bot_at", lambda message: True)
    monkeypatch.setattr("src.services.priority_at_reply_service.random.random", lambda: 0)
    monkeypatch.setattr(service, "_send_emoji", AsyncMock(side_effect=replies.DeliveryUncertain("unknown")))
    send_text = AsyncMock()
    monkeypatch.setattr(service, "_send_text", send_text)
    with pytest.raises(replies.DeliveryUncertain):
        await service._reply(msg, SimpleNamespace(), [])
    send_text.assert_not_awaited()
