"""QA-013：取消结束 pending，保留 UNKNOWN 防重发与迟到 ACK。"""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio

import pytest

from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.decision_replay import replies
from src.platform_io.manager import PlatformIOManager
from src.platform_io.outbound_tracker import OutboundTracker
from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverKind, RouteKey


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["attempt", "driver", "receipt"])
async def test_cancel_at_each_send_await_finishes_tracking(monkeypatch, phase):
    manager = PlatformIOManager()
    route = RouteKey("test", "bot")
    message = SimpleNamespace(message_id="out", session_id="group",
                              message_info=SimpleNamespace(additional_config={}),
                              raw_message=MessageSequence([TextComponent("hello")]))
    sent = DeliveryReceipt("out", route, DeliveryStatus.SENT, driver_id="driver",
                           driver_kind=DriverKind.PLUGIN, external_message_id="platform-id")
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    async def emit(*args, **kwargs):
        if phase == "attempt":
            await wait()

    async def send(**kwargs):
        if phase == "driver":
            await wait()
        return sent

    async def observe(*args):
        if phase == "receipt":
            await wait()

    driver = SimpleNamespace(driver_id="driver", descriptor=SimpleNamespace(kind=DriverKind.PLUGIN),
                             send_message=AsyncMock(side_effect=send))
    monkeypatch.setattr(manager, "resolve_drivers", lambda route: [driver])
    monkeypatch.setattr(replies, "emit", emit)
    observer = AsyncMock(side_effect=observe)
    monkeypatch.setattr(replies, "receipt", observer)
    task = asyncio.create_task(manager.send_message(message, route))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.outbound_tracker.get_pending("out", "driver") is None
        if phase == "receipt":
            assert manager.outbound_tracker.get_receipt_by_external_id("platform-id") is sent
        else:
            unknown = manager.outbound_tracker.get_uncertain("out", "driver")
            assert unknown.status == DeliveryStatus.UNKNOWN
            assert unknown.error == "CancelledError"
            assert observer.call_args.args[2] is unknown
            calls = driver.send_message.await_count
            retry = await manager.send_message(message, route)
            assert retry.receipts == [unknown]
            assert driver.send_message.await_count == calls
            # 独立迟到回执仍可消除 UNKNOWN 并建立平台 ID 索引。
            manager.outbound_tracker.finish_tracking(sent)
            assert manager.outbound_tracker.get_uncertain("out", "driver") is None
            assert manager.outbound_tracker.get_receipt_by_external_id("platform-id") is sent
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_receipt_observer_failure_cannot_leave_pending(monkeypatch):
    manager = PlatformIOManager()
    route = RouteKey("test")
    message = SimpleNamespace(message_id="out", session_id="group",
                              message_info=SimpleNamespace(additional_config={}),
                              raw_message=MessageSequence([TextComponent("hello")]))
    driver = SimpleNamespace(driver_id="driver", descriptor=SimpleNamespace(kind=DriverKind.PLUGIN),
                             send_message=AsyncMock(side_effect=RuntimeError("transport interrupted")))
    monkeypatch.setattr(manager, "resolve_drivers", lambda route: [driver])
    monkeypatch.setattr(replies, "receipt", AsyncMock(side_effect=RuntimeError("observer failed")))
    with pytest.raises(RuntimeError, match="observer failed"):
        await manager.send_message(message, route)
    assert manager.outbound_tracker.get_pending("out", "driver") is None
    assert manager.outbound_tracker.get_uncertain("out", "driver").status == DeliveryStatus.UNKNOWN


def test_unknown_ttl_is_bounded_and_driver_isolated(monkeypatch):
    module = import_module("src.platform_io.outbound_tracker")
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    tracker = OutboundTracker(ttl_seconds=10)
    route = RouteKey("test")
    for driver_id in ("a", "b"):
        tracker.begin_tracking("out", route, driver_id)
    unknown = DeliveryReceipt("out", route, DeliveryStatus.UNKNOWN, driver_id="a")
    tracker.finish_tracking(unknown)
    assert tracker.get_pending("out", "a") is None
    assert tracker.get_pending("out", "b") is not None
    with pytest.raises(ValueError, match="确认前不能重发"):
        tracker.begin_tracking("out", route, "a")
    # 无 driver_id 的回执在多驱动状态下不猜测归属。
    tracker.finish_tracking(DeliveryReceipt("out", route, DeliveryStatus.SENT))
    assert tracker.get_uncertain("out", "a") is unknown
    now[0] = 111.0
    assert tracker.get_uncertain("out", "a") is None
    assert tracker.get_pending("out", "b") is None
    tracker.begin_tracking("out", route, "a")
    tracker.finish_tracking(unknown)
    tracker.clear()
    assert tracker.get_uncertain("out", "a") is None
    assert not tracker._uncertain_expire_heap


def test_ack_before_cancel_result_does_not_reopen_unknown():
    tracker = OutboundTracker()
    route = RouteKey("test")
    tracker.begin_tracking("out", route, "driver")
    sent = DeliveryReceipt("out", route, DeliveryStatus.SENT, driver_id="driver", external_message_id="ack")
    tracker.finish_tracking(sent)
    tracker.finish_tracking(DeliveryReceipt("out", route, DeliveryStatus.UNKNOWN, driver_id="driver"))
    assert tracker.get_pending("out", "driver") is None
    assert tracker.get_uncertain("out", "driver") is None
    assert tracker.get_receipt_by_external_id("ack") is sent


@pytest.mark.asyncio
async def test_cancel_persists_unknown_and_late_ack_confirms_without_resend(monkeypatch):
    from src.decision_replay import recorder

    monkeypatch.setattr(recorder, "enabled", lambda: True)
    manager = PlatformIOManager()
    route = RouteKey("test", "bot")
    message = SimpleNamespace(message_id="out", session_id="group", platform="test",
                              message_info=SimpleNamespace(additional_config={}, group_info=None),
                              raw_message=MessageSequence([TextComponent("hello")]))
    entered = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    driver = SimpleNamespace(driver_id="driver", descriptor=SimpleNamespace(kind=DriverKind.PLUGIN),
                             send_message=AsyncMock(side_effect=send))
    monkeypatch.setattr(manager, "resolve_drivers", lambda route: [driver])
    task = asyncio.create_task(manager.send_message(message, route))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.outbound_tracker.get_pending("out", "driver") is None
        assert recorder.store.outbound_state("group", "out")["status"] == "unknown"
        retry = await manager.send_message(message, route)
        assert retry.receipts[0].status == DeliveryStatus.UNKNOWN
        driver.send_message.assert_awaited_once()
        reconciled = asyncio.Event()
        original = replies.reconcile_receipt

        async def reconcile(receipt):
            await original(receipt)
            reconciled.set()

        monkeypatch.setattr(replies, "reconcile_receipt", reconcile)
        sent = DeliveryReceipt("out", route, DeliveryStatus.SENT, driver_id="driver",
                               driver_kind=DriverKind.PLUGIN, external_message_id="late-id")
        manager.outbound_tracker.finish_tracking(sent)
        await asyncio.wait_for(reconciled.wait(), 2)
        assert manager.outbound_tracker.get_uncertain("out", "driver") is None
        assert recorder.store.outbound_state("group", "out")["status"] == "sent"
        confirmed = await manager.send_message(message, route)
        assert confirmed.receipts[0].status == DeliveryStatus.SENT
        driver.send_message.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
