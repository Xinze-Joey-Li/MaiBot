"""独立 @ 队列的并发上限、密集突发、去重与关闭。"""

from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import asyncio

import pytest

from src.services.priority_at_reply_service import PriorityAtReplyService


def message(index, group="group"):
    return SimpleNamespace(session_id=group, message_id=str(index), platform="qq",
                           message_info=SimpleNamespace(additional_config={"account_id": "bot"}))


async def drain(service):
    async def waiting():
        while service._tasks or service._pending:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(waiting(), 3)


@pytest.mark.asyncio
async def test_dense_normal_flow_respects_both_limits_without_loss(monkeypatch):
    service = PriorityAtReplyService(max_concurrent=4, max_per_session=2, max_pending=64, max_pending_per_session=16)
    active = Counter()
    peaks = Counter()
    completed = []

    async def reply(msg, runtime, history):
        active[msg.session_id] += 1
        active["total"] += 1
        peaks[msg.session_id] = max(peaks[msg.session_id], active[msg.session_id])
        peaks["total"] = max(peaks["total"], active["total"])
        await asyncio.sleep(0.002)
        active[msg.session_id] -= 1
        active["total"] -= 1
        completed.append((msg.session_id, msg.message_id))

    monkeypatch.setattr(service, "_reply", reply)
    runtime = SimpleNamespace(_chat_history=["snapshot"])
    for index in range(40):
        assert service.start_reply(message(index, f"g{index % 5}"), runtime)
    assert len(service._tasks) <= 4
    assert len(service._pending) <= 64
    await drain(service)
    assert len(set(completed)) == 40
    assert peaks["total"] == 4
    assert max(value for group, value in peaks.items() if group != "total") <= 2
    await service.shutdown()


@pytest.mark.asyncio
async def test_full_group_does_not_block_another_group_and_queue_deduplicates(monkeypatch):
    service = PriorityAtReplyService(max_concurrent=2, max_per_session=1)
    release = asyncio.Event()
    started = []

    async def reply(msg, runtime, history):
        started.append((msg.session_id, msg.message_id, history))
        await release.wait()

    monkeypatch.setattr(service, "_reply", reply)
    runtime = SimpleNamespace(_chat_history=["original"])
    assert service.start_reply(message(1, "a"), runtime)
    assert service.start_reply(message(2, "a"), runtime)
    assert not service.start_reply(message(2, "a"), runtime)
    assert service.start_reply(message(1, "b"), runtime)
    runtime._chat_history.append("later")
    await asyncio.sleep(0)
    assert {group for group, _, _ in started} == {"a", "b"}
    release.set()
    await drain(service)
    assert len(started) == 3
    assert all(history == ["original"] for _, _, history in started)
    assert not service.start_reply(message(2, "a"), runtime)
    await service.shutdown()


@pytest.mark.asyncio
async def test_overload_is_bounded_logged_and_rejected_message_can_retry(monkeypatch):
    service = PriorityAtReplyService(max_concurrent=2, max_per_session=1, max_pending=3, max_pending_per_session=2)
    release = asyncio.Event()
    logger = Mock()
    monkeypatch.setattr("src.common.logger.get_logger", lambda name: logger)

    async def reply(*args):
        await release.wait()

    monkeypatch.setattr(service, "_reply", reply)
    runtime = SimpleNamespace(_chat_history=[])
    for group, index in (("a", 1), ("b", 1), ("a", 2), ("a", 3), ("b", 2)):
        assert service.start_reply(message(index, group), runtime)
    for index in range(100):
        assert not service.start_reply(message(f"overflow{index}", "a"), runtime)
    assert len(service._tasks) == 2 and len(service._pending) == 3
    assert logger.warning.call_count == 100
    release.set()
    await drain(service)
    assert service.start_reply(message("overflow0", "a"), runtime)
    await drain(service)
    await service.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_active_and_discards_pending_without_starting_them(monkeypatch):
    service = PriorityAtReplyService(max_concurrent=1, max_per_session=1)
    started = []

    async def reply(msg, *args):
        started.append(msg.message_id)
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_reply", reply)
    runtime = SimpleNamespace(_chat_history=[])
    for index in range(4):
        assert service.start_reply(message(index), runtime)
    await asyncio.sleep(0)
    tasks = list(service._tasks.values())
    await service.shutdown()
    assert all(task.cancelled() for task in tasks)
    assert started == ["0"]
    assert not service._tasks and not service._pending and not service._pending_keys
    assert not service._seen and not service._running_per_session
    assert not service.start_reply(message(5), runtime)


@pytest.mark.asyncio
async def test_failed_task_releases_slot_and_next_queued_task_runs(monkeypatch):
    service = PriorityAtReplyService(max_concurrent=1, max_per_session=1)
    logger = Mock()
    monkeypatch.setattr("src.common.logger.get_logger", lambda name: logger)
    completed = []

    async def reply(msg, *args):
        if msg.message_id == "first":
            raise RuntimeError("main reply failure")
        completed.append(msg.message_id)

    monkeypatch.setattr(service, "_reply", reply)
    runtime = SimpleNamespace(_chat_history=[])
    service.start_reply(message("first"), runtime)
    service.start_reply(message("second"), runtime)
    await drain(service)
    assert completed == ["second"]
    logger.error.assert_called_once()
    await service.shutdown()
