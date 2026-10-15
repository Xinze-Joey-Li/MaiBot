from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import time
import uuid

import pytest

from src.decision_replay import recorder
from src.decision_replay.store import ReplayStore


def event(turn="a", session="g1", **overrides):
    values = dict(
        event_id=uuid.uuid4().hex,
        timestamp=time.time(),
        session_id=session,
        logical_turn_id=turn,
        source="message",
        cycle_id=None,
        request_id=None,
        message_ids=["m1"],
        stage="scheduler",
        event_type="turn.started",
        decision="started",
        reason_code="external",
        detail={},
        duration_ms=None,
        schema_version=1,
    )
    values.update(overrides)
    return values


@pytest.fixture
def store(tmp_path):
    return ReplayStore(tmp_path / "replay.db")


def test_persistence_filters_pagination_and_request_dedup(store):
    events = [event(str(i), "g1" if i % 2 else "g2") for i in range(250)]
    request = event(
        "1",
        stage="model",
        event_type="model.request",
        request_id="request1",
        decision="succeeded",
        detail={"input_tokens": 100, "output_tokens": 10, "task": "planner", "cost": None},
    )
    store.write_batch(events + [request, request, {**request, "event_id": uuid.uuid4().hex}])
    restored = ReplayStore(store.path)
    result = restored.query(session_id="g1", page=2, page_size=20)
    assert result["total"] == 125
    assert len(result["items"]) == 20
    assert result["summary"]["model_calls"] == 1
    assert result["summary"]["input_tokens"] == 100
    assert result["summary"]["cost"] is None
    assert restored.detail("1", "g2") is None
    assert restored.detail("1", "g1")["status"] == "incomplete_or_running"
    assert restored.detail("1", "g1", limit=1)["has_more"]
    assert restored.query(session_id="g2")["summary"]["model_calls"] == 0


def test_cross_group_id_rejected(store):
    store.write_batch([event()])
    with pytest.raises(ValueError, match="Cross-session"):
        store.write_batch([event(session="g2")])
    assert store.query()["total"] == 1


def test_cleanup_and_completion(store):
    old = time.time() - 9 * 86400
    store.write_batch([event("old", timestamp=old)])
    assert store.query()["total"] == 0
    store.write_batch([event(), event(event_type="turn.ended", reason_code="local_actions_completed")])
    assert store.detail("a", "g1")["status"] == "complete"


def test_recorder_disabled_and_failed_storage_do_not_raise(store, monkeypatch):
    monkeypatch.setattr(recorder, "store", store)
    monkeypatch.setattr(recorder, "enabled", lambda: False)
    recorder.record("gate", "gate.decided", trace=recorder.new_trace("g1"))
    assert store.queue.empty()
    monkeypatch.setattr(recorder, "enabled", lambda: True)
    monkeypatch.setattr(store, "submit", lambda _: (_ for _ in ()).throw(RuntimeError("locked")))
    recorder.record("gate", "gate.decided", trace=recorder.new_trace("g1"))


@pytest.mark.asyncio
async def test_concurrent_context_isolation_and_threads(store, monkeypatch):
    monkeypatch.setattr(recorder, "store", store)
    monkeypatch.setattr(recorder, "enabled", lambda: True)

    async def work(session):
        with recorder.bind(recorder.new_trace(session, turn_id=session)):
            await asyncio.sleep(0)
            await asyncio.to_thread(recorder.record, "emoji", "emoji.selection", "skipped", "score_below_threshold")

    await asyncio.gather(work("g1"), work("g2"))
    assert await asyncio.to_thread(store.flush)
    for session in ("g1", "g2"):
        assert store.detail(session, session)["events"][0]["session_id"] == session
    assert recorder.current_trace.get() is None


def test_trace_sanitizes_private_fields(store, monkeypatch):
    captured = []
    monkeypatch.setattr(recorder, "enabled", lambda: True)
    monkeypatch.setattr(recorder.store, "submit", captured.append)
    with recorder.bind(recorder.new_trace("g1")):
        recorder.record(
            "memory",
            "memory.injected",
            detail={"tag_ids": ["tag1"], "content": "private", "nested": {"api_key": "secret"}},
        )
    assert captured[0]["detail"] == {"tag_ids": ["tag1"], "nested": {}, "trigger_source": "message"}


@pytest.mark.parametrize(
    "reason", ["probability_miss", "score_below_threshold", "cooldown_blocked", "sent", "no_tag", "send_failed"]
)
def test_emoji_reason_filters(store, reason):
    store.write_batch([event(stage="emoji", event_type="emoji.selection", reason_code=reason)])
    assert store.query(emoji=reason)["total"] == 1


def test_planner_monitor_states_and_unknown_usage(store, monkeypatch):
    captured = []
    monkeypatch.setattr(recorder, "enabled", lambda: True)
    monkeypatch.setattr(recorder.store, "submit", captured.append)
    with recorder.bind(recorder.new_trace("g1")):
        for tool in ([], [{"tool_name": "wait", "success": True}], [{"tool_name": "reply", "success": False}]):
            recorder.monitor_event(
                "planner.finalized", {"session_id": "g1", "tools": tool, "final_state": {"end_reason": "test"}}
            )
    assert [e["decision"] for e in captured] == ["no_reply", "wait", "failed"]
    store.write_batch(captured)
    assert store.query()["summary"]["input_tokens"] is None


@pytest.mark.asyncio
async def test_send_wrapper_preserves_failure_and_cancel(monkeypatch):
    captured = []
    monkeypatch.setattr(recorder, "enabled", lambda: True)
    monkeypatch.setattr(recorder.store, "submit", captured.append)
    message = SimpleNamespace(
        session_id="g1", message_id="mock", message_info=SimpleNamespace(additional_config={}, group_info=None), raw_message=SimpleNamespace(components=[])
    )
    original = AsyncMock(return_value=None)
    assert await recorder.traced_send(original)(message) is None
    assert any(e["reason_code"] == "skipped" for e in captured)
    original.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await recorder.traced_send(original)(message)
    assert recorder.current_trace.get() is None


def test_worker_disk_failure_is_visible_and_flushes(store, monkeypatch):
    monkeypatch.setattr(store, "write_batch", lambda _: (_ for _ in ()).throw(OSError("locked")))
    store.submit(event())
    assert store.flush()
    assert store.errors == 1 and store.dropped == 1


def test_started_and_completed_model_request_upsert_not_double_counted(store):
    started = event(
        stage="model", event_type="model.request", request_id="r", decision="started", detail={"task": "planner"}
    )
    done = event(
        stage="model",
        event_type="model.request",
        request_id="r",
        decision="succeeded",
        detail={"task": "planner", "input_tokens": 123, "output_tokens": 5},
    )
    store.write_batch([started, done])
    summary = store.query()["summary"]
    assert summary["model_calls"] == 1 and summary["input_tokens"] == 123
    assert store.detail("a", "g1")["requests"][0]["status"] == "succeeded"


def test_reply_then_silent_continuation_is_not_classified_as_whole_turn_silence(store):
    store.write_batch(
        [
            event(stage="planner", event_type="planner.finalized", decision="reply"),
            event(stage="planner", event_type="planner.finalized", decision="no_reply"),
        ]
    )
    assert store.query()["items"][0]["planner_result"] == "reply"
    assert store.query()["summary"]["planner_no_reply"] == 0


def test_send_message_ids_do_not_inflate_trigger_message_count(store):
    store.write_batch(
        [
            event(),
            event(
                stage="send",
                event_type="send.completed",
                decision="sent",
                message_ids=["bot-output"],
                detail={"has_text": True},
            ),
        ]
    )
    assert store.query()["items"][0]["message_ids"] == ["m1"]
    assert store.query(spoken=True)["total"] == 1


def test_bounded_queue_overload_never_blocks(store):
    store.queue = __import__("queue").Queue(maxsize=1)
    store.worker = SimpleNamespace(is_alive=lambda: True)
    store.submit(event())
    store.submit(event())
    assert store.dropped == 1


def test_wait_resume_reuses_logical_turn_and_marks_reopened(store):
    store.write_batch([event(event_type="turn.ended", reason_code="wait")])
    store.write_batch([event(stage="planner", event_type="planner.started", decision="started")])
    assert store.query()["total"] == 1
    assert store.detail("a", "g1")["status"] == "incomplete_or_running"


def test_documented_synthetic_example_replays_with_exact_usage(store):
    from pathlib import Path
    import json

    data = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/qq-agent-decision-replay-example.json").read_text(encoding="utf-8")
    )
    events = data["events"]
    base = events[0]["timestamp"]
    for item in events:
        item["timestamp"] = time.time() - 10 + item["timestamp"] - base
    store.write_batch(events)
    summary = store.query()["summary"]
    assert summary["model_calls"] == 2 and summary["input_tokens"] == 1200 and summary["output_tokens"] == 80
    assert store.query(spoken=True, emoji="sent")["total"] == 1


def test_retention_resolver_respects_increased_retention_after_restart(store):
    old = time.time() - 9 * 86400
    store.retention_resolver = lambda: 30
    store.write_batch([event("old", timestamp=old)])
    assert store.query()["total"] == 1
    store.retention_resolver = lambda: 7
    store.cleanup()
    assert store.query()["total"] == 0
