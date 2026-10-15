from types import SimpleNamespace
from unittest.mock import Mock
from datetime import datetime

import asyncio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.decision_replay import recorder
from src.decision_replay.store import ReplayStore
from src.webui.routers import decision_replay as api
from src.webui.dependencies import require_auth


@pytest.fixture
def capture(tmp_path, monkeypatch):
    store = ReplayStore(tmp_path / "trace.db")
    monkeypatch.setattr(recorder, "enabled", lambda: True)
    monkeypatch.setattr(recorder, "store", store)
    yield store
    store.flush()


@pytest.mark.parametrize("probability,passed", [(0.01, False), (0.99, True)])
def test_real_scheduler_gate_has_correlated_structured_trace(capture, monkeypatch, probability, passed):
    from src.maisaka.turn_trigger import gates, scheduler
    from src.maisaka.turn_trigger.reply_likelihood import ReplyLikelihoodScore
    from src.maisaka import planner_lifecycle

    monkeypatch.setattr(gates, "is_bot_self", lambda *_: False)
    monkeypatch.setattr(scheduler.focus_mode_manager, "can_decide", lambda *a, **kw: True)
    monkeypatch.setattr(scheduler, "is_dynamic_reply_trigger_enabled", lambda: True)
    monkeypatch.setattr(planner_lifecycle, "lifecycle_mode", lambda: "optimized")
    monkeypatch.setattr(
        gates, "score_reply_likelihood", lambda _: ReplyLikelihoodScore(probability, 1, {"intercept": 1})
    )
    message = SimpleNamespace(
        message_id="m1",
        processed_plain_text="test",
        is_at=False,
        is_mentioned=False,
        timestamp=datetime.now(),
        platform="test",
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="u")),
    )
    runtime = SimpleNamespace(
        session_id="test-group",
        session_name="测试群",
        log_prefix="test",
        chat_stream=SimpleNamespace(is_group_session=True),
        _agent_state="stop",
        _STATE_RUNNING="running",
        _STATE_WAIT="wait",
        _message_turn_scheduled=False,
        message_cache=[message],
        _last_processed_index=0,
        _chat_history=[],
        _decision_replay_turn_id=None,
        _get_pending_message_count=lambda: 1,
        _get_effective_reply_frequency=lambda: 0.5,
        _is_reply_frequency_silent=lambda: False,
        _has_forced_turn_trigger=lambda: False,
        _idle_backoff=SimpleNamespace(should_delay=lambda _: False),
        _enqueue_message_turn=Mock(),
        _defer_message_turn_check=Mock(),
    )
    controller = scheduler.MessageTurnScheduler(runtime)
    controller.schedule_message_turn()
    controller.schedule_message_turn()  # repeated checks do not emit duplicate records
    assert runtime._enqueue_message_turn.called == passed
    assert capture.flush()
    rows = capture.query()["items"]
    assert len(rows) == 1
    detail = capture.detail(rows[0]["logical_turn_id"], "test-group")
    gate = next(e for e in detail["events"] if e["stage"] == "gate")
    assert gate["detail"]["probability"] == probability
    assert gate["detail"]["contribution_unit"] == "logit"
    assert gate["message_ids"] == ["m1"]
    if passed:
        assert runtime._decision_replay_turn_id == detail["logical_turn_id"]
    else:
        assert runtime._decision_replay_turn_id is None
        assert detail["status"] == "complete"


def test_actual_model_attempt_records_once_and_preserves_unknowns(capture):
    trace = SimpleNamespace(request_id="req", attempt=1, current_attempt_started_at=None, session_id="g1")
    request = SimpleNamespace(trace_context=trace, model_info=SimpleNamespace(name="test-model"))
    with recorder.bind(recorder.new_trace("g1", turn_id="turn")):
        recorder.model_attempt(request, SimpleNamespace(usage=None), task="planner", session_id="g1")
        recorder.model_attempt(request, SimpleNamespace(usage=None), task="planner", session_id="g1")
    assert capture.flush()
    result = capture.query()
    assert result["summary"]["model_calls"] == 1
    assert result["summary"]["input_tokens"] is None
    assert capture.detail("turn", "g1")["requests"][0]["request_id"] == "req:1"


@pytest.mark.asyncio
async def test_memory_timeout_and_exact_injection_ids(capture, monkeypatch):
    from src.services import tag_memory_recall as recall

    async def build(*_):
        recall.last_reference_ids.set(("tag-1",))
        return "reference"

    monkeypatch.setattr(recall, "_build_tag_memory_reference", build)
    with recorder.bind(recorder.new_trace("g1", turn_id="memory")):
        assert await recall.build_tag_memory_reference("g1") == "reference"
        recall.record_tag_injection("g1", "planner")

        async def slow(*_):
            await asyncio.sleep(10)

        monkeypatch.setattr(recall, "_build_tag_memory_reference", slow)
        monkeypatch.setattr(recall, "RECALL_TIMEOUT_SECONDS", 0.001)
        assert await recall.build_tag_memory_reference("g1") == ""
    assert capture.flush()
    events = capture.detail("memory", "g1")["events"]
    assert events[0]["detail"]["tag_ids"] == ["tag-1"]
    assert events[1]["reason_code"] == "recall_timeout"


@pytest.mark.asyncio
async def test_real_emoji_probability_miss(capture, monkeypatch):
    from src.emoji_system import reply_attachment

    monkeypatch.setattr(reply_attachment.random, "uniform", lambda *_: 0.4)
    monkeypatch.setattr(reply_attachment.random, "random", lambda: 0.8)
    with recorder.bind(recorder.new_trace("g1", turn_id="emoji")):
        await reply_attachment.send_after_text("g1", "开心")
    assert capture.flush()
    assert capture.detail("emoji", "g1")["events"][0]["reason_code"] == "probability_miss"


def test_api_auth_and_session_bound_queries(capture, monkeypatch):
    monkeypatch.setattr(api, "store", capture)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        assert client.get("/api/webui/decision-replay").status_code in (401, 403)
    app.dependency_overrides[require_auth] = lambda: "test"
    with TestClient(app) as client:
        assert client.get("/api/webui/decision-replay?page_size=101").status_code == 422
        assert client.get("/api/webui/decision-replay?start=100&end=10").status_code == 422
        assert client.get("/api/webui/decision-replay/missing?session_id=g1").status_code == 404
        response = client.get("/api/webui/decision-replay")
        assert response.status_code == 200
        assert response.json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_real_local_emoji_scoring_then_platform_result(capture, monkeypatch, tmp_path, valid):
    from src.emoji_system import local_selection as selection
    from src.common.data_models.message_component_data_model import EmojiComponent, MessageSequence
    from src.services import send_service
    import json

    monkeypatch.setattr(selection, "HISTORY_PATH", tmp_path / "history.db")
    monkeypatch.setattr(selection, "METADATA_PATH", tmp_path / "metadata.json")
    monkeypatch.setattr(selection, "BUNDLED_METADATA_PATH", tmp_path / "no-bundle.json")
    selection.METADATA_PATH.write_text(
        json.dumps({key: {"keywords": ["game"], "useWhen": ["win"], "quality": 1} for key in ("a", "b")})
    )
    candidates = [SimpleNamespace(file_hash=key, description="开心") for key in ("a", "b")]
    with recorder.bind(recorder.new_trace("g1", turn_id="scoring")):
        emoji = selection.select_candidate("g1", "开心", candidates, "game win" if valid else "rain")
        if valid:
            assert emoji.file_hash == "a"
            message = SimpleNamespace(
                session_id="g1",
                message_id="sent-1",
                message_info=SimpleNamespace(additional_config={}, group_info=SimpleNamespace(group_id="g1")),
                raw_message=MessageSequence([EmojiComponent(binary_hash="a", binary_data=b"test")]),
            )
            async def confirmed(message, **kwargs):
                from src.decision_replay.replies import receipt
                from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverKind, RouteKey
                route = RouteKey("test")
                await receipt(message, route, DeliveryReceipt(message.message_id, route, DeliveryStatus.SENT,
                              driver_kind=DriverKind.PLUGIN, external_message_id="mock-platform-id"), "attempt")
                return message
            monkeypatch.setattr(send_service, "_send_via_platform_io", confirmed)
            assert await send_service.send_session_message_with_message(message) is message
        else:
            assert emoji is None
    assert capture.flush()
    result = capture.detail("scoring", "g1")
    assert result["emoji_result"] == ("sent" if valid else "score_below_threshold")


@pytest.mark.asyncio
async def test_real_cooldown_rejection_keeps_precise_reason(capture, monkeypatch):
    from src.common.data_models.message_component_data_model import EmojiComponent, MessageSequence
    from src.services import send_service
    from src.services.group_emoji_gate import group_emoji_gate
    from unittest.mock import AsyncMock

    message = SimpleNamespace(
        session_id="g1",
        message_id="attempt-1",
        platform="test",
        message_info=SimpleNamespace(additional_config={}, group_info=SimpleNamespace(group_id="group")),
        raw_message=MessageSequence([EmojiComponent(binary_hash="a", binary_data=b"test")]),
    )
    monkeypatch.setattr(
        send_service, "_invoke_send_hook", AsyncMock(return_value=(SimpleNamespace(aborted=False, kwargs={}), message))
    )
    monkeypatch.setattr(send_service, "_prepare_message_for_platform_io", AsyncMock())
    manager = SimpleNamespace(
        ensure_send_pipeline_ready=AsyncMock(), build_route_key_from_message=lambda _: "route", send_message=AsyncMock()
    )
    monkeypatch.setattr(send_service, "get_platform_io_manager", lambda: manager)
    monkeypatch.setattr(group_emoji_gate, "acquire", lambda _: False)
    with recorder.bind(recorder.new_trace("g1", turn_id="cooldown")):
        assert await send_service.send_session_message_with_message(message) is None
    manager.send_message.assert_not_awaited()
    assert capture.flush()
    assert capture.detail("cooldown", "g1")["emoji_result"] == "cooldown_blocked"


@pytest.mark.asyncio
async def test_replyer_failure_stage_is_separate_from_send_failure(capture):
    from src.common.data_models.reply_generation_data_models import ReplyGenerationResult

    async def generate(**_):
        return False, ReplyGenerationResult()

    with recorder.bind(recorder.new_trace("g1", turn_id="replyer")):
        success, _ = await recorder.traced_replyer(generate)(stream_id="g1")
        assert not success
    assert capture.flush()
    events = capture.detail("replyer", "g1")["events"]
    assert events[-1]["reason_code"] == "generation_failed"
    assert not any(e["stage"] == "send" for e in events)


def test_tag_ids_only_recorded_when_present_after_request_hooks(capture):
    from src.services import tag_memory_recall as recall
    from src.llm_models.model_client.base_client import ResponseRequest
    from src.llm_models.payload_content.context_item import ContextItemBuilder

    ids = recall.last_reference_ids.set(("tag-1",))
    text = recall.last_reference_text.set("reference")
    scope = recall.last_reference_session.set("g1")
    try:
        request = ResponseRequest(
            model_info=SimpleNamespace(name="model"),
            context_items=[ContextItemBuilder().add_text_content("reference").build()],
        )
        with recorder.bind(recorder.new_trace("g1", turn_id="final-tags")):
            recall.record_final_request_tags(request, "g1", "planner", "request1")
            request.context_items = []
            recall.record_final_request_tags(request, "g1", "planner", "request2")
        assert capture.flush()
        events = capture.detail("final-tags", "g1")["events"]
        assert events[0]["detail"]["tag_ids"] == ["tag-1"]
        assert events[0]["request_id"] == "request1"
        assert events[1]["decision"] == "not_injected"
    finally:
        recall.last_reference_ids.reset(ids)
        recall.last_reference_text.reset(text)
        recall.last_reference_session.reset(scope)
