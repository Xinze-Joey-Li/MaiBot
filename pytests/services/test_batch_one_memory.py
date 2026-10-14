"""第一批回复隔离、有限重试和冲突预算的回归与持久化集成验证。"""

from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio
import copy
import json
import sqlite3

import pytest

from src.services import tag_memory_recall
from src.services.tag_conflict_service import ConflictBudgetExceeded, decide_tag_conflicts
from src.services.tag_extraction_service import InvalidTagOutput, TagExtractionService
from src.services.tag_memory_service import tag_memory_service
from src.services.tag_memory_store import TagCandidate, TagMemoryStore


def make_store(tmp_path):
    store = TagMemoryStore(tmp_path / "memory.db")
    store.initialize()
    store.initialize_scanner(0)
    return store


def enqueue(store, number=1):
    store.enqueue_page([{
        "session_id": "group", "message_id": f"m{number}", "user_id": "user", "user_name": "群友",
        "timestamp": 0, "text": "喜欢爬山", "reply_to": None, "reply_context": [],
    }], number)
    return store.next_extraction_job(batch_size=1, max_wait_seconds=1)


def valid_output(number=1):
    return json.dumps({"summary": "讨论兴趣", "tags": [{
        "name": "爬山", "summary": "喜欢爬山", "category_path": ["群公共", "群活动"],
        "evidence": [{"message_id": f"m{number}", "quote": "爬山"}],
    }]})


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [sqlite3.OperationalError("database is locked"), ValueError("session missing")])
async def test_optional_recall_failures_do_not_block_chat(monkeypatch, error):
    monkeypatch.setattr(tag_memory_recall, "_build_tag_memory_reference", AsyncMock(side_effect=error))
    assert await tag_memory_recall.build_tag_memory_reference("group") == ""


@pytest.mark.asyncio
async def test_optional_recall_timeout_and_cancellation(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(tag_memory_recall, "RECALL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(tag_memory_recall, "_build_tag_memory_reference", slow)
    assert await tag_memory_recall.build_tag_memory_reference("group") == ""
    assert cancelled.is_set()
    task = asyncio.create_task(tag_memory_recall.build_tag_memory_reference("group"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_finite_backoff_quarantine_same_group_progress_and_manual_recovery(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    now = [100.0]
    monkeypatch.setattr("src.services.tag_memory_store.time.time", lambda: now[0])
    first = enqueue(store)
    for attempt in range(1, 6):
        store.fail_extraction_job(first["id"], "invalid model output", invalid_output=True)
        row = store.list_extraction_jobs("group")[0]
        assert row["attempts"] == attempt and row["retry_at"] == now[0] + 60 * 2 ** (attempt - 1)
        assert store.next_extraction_job(1, 1) is None
        now[0] = row["retry_at"]
    assert row["status"] == "failed"
    restarted = TagMemoryStore(store.db_path)
    restarted.initialize()
    second = enqueue(restarted, 2)
    assert json.loads(second["payload"])["messages"][0]["message_id"] == "m2"
    restarted.record_batch(session_id="group", session_name="群", external_id="second", summary="后续批次",
                           message_ids=["m2"], tags=[], extraction_job_id=second["id"])
    with pytest.raises(ValueError):
        restarted.retry_extraction_job("another-group", first["id"])
    restarted.retry_extraction_job("group", first["id"])
    recovered = restarted.next_extraction_job(1, 1)
    assert recovered["id"] == first["id"] and recovered["attempts"] == 0
    restarted.record_batch(session_id="group", session_name="群", external_id="first", summary="人工恢复",
                           message_ids=["m1"], tags=[], extraction_job_id=first["id"])
    with closing(sqlite3.connect(store.db_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM tag_inbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 2


def test_failure_ledger_survives_database_write_lock_and_restart(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    job = enqueue(store)
    original = store._connect

    def locked(*, write=False):
        if write:
            raise sqlite3.OperationalError("database is locked")
        return original(write=write)

    monkeypatch.setattr(store, "_connect", locked)
    with pytest.raises(sqlite3.OperationalError):
        store.fail_extraction_job(job["id"], "locked", invalid_output=False)
    restarted = TagMemoryStore(store.db_path)
    restarted.initialize()
    assert restarted.next_extraction_job(1, 1) is None
    assert restarted.list_extraction_jobs("group")[0]["attempts"] == 1


@pytest.fixture
def extraction_environment(monkeypatch):
    client = SimpleNamespace(generate_response=AsyncMock(return_value=SimpleNamespace(response=valid_output(), prompt_tokens=0, completion_tokens=0, total_tokens=0)))
    monkeypatch.setattr("src.services.llm_service.LLMServiceClient", lambda **kwargs: client)
    monkeypatch.setattr("src.common.prompt_i18n.load_prompt", lambda name: "{input_json}")
    monkeypatch.setattr(tag_memory_service, "_resolve_scope", lambda session_id: (session_id, "群"))
    monkeypatch.setattr(tag_memory_service, "_verify_sources", lambda *args: None)
    return client


@pytest.mark.asyncio
async def test_cached_output_survives_cache_commit_lock_and_restart(tmp_path, monkeypatch, extraction_environment):
    store = make_store(tmp_path)
    job = enqueue(store)
    service = TagExtractionService()
    service._store = store
    # 真实 WAL 写锁：模型已成功，但结果写入 SQLite 被拒绝。
    with closing(sqlite3.connect(store.db_path)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                await service._process_job(job)
        finally:
            lock.rollback()
    service = TagExtractionService()
    service._store = TagMemoryStore(store.db_path)
    service._store.initialize()
    await service._process_job(service._store.next_extraction_job(1, 1))
    extraction_environment.generate_response.assert_awaited_once()
    assert service._store.next_extraction_job(1, 1) is None


@pytest.mark.asyncio
async def test_persistent_bad_model_is_only_called_five_times(tmp_path, monkeypatch, extraction_environment):
    store = make_store(tmp_path)
    enqueue(store)
    now = [100.0]
    monkeypatch.setattr("src.services.tag_memory_store.time.time", lambda: now[0])
    extraction_environment.generate_response.return_value = SimpleNamespace(response="invalid json")
    service = TagExtractionService()
    service._store = store
    for _ in range(5):
        job = store.next_extraction_job(1, 1)
        with pytest.raises(InvalidTagOutput):
            await service._process_job(job)
        store.fail_extraction_job(job["id"], "invalid json", invalid_output=True)
        now[0] = store.list_extraction_jobs("group")[0]["retry_at"]
    assert store.next_extraction_job(1, 1) is None
    assert extraction_environment.generate_response.await_count == 5
    with closing(sqlite3.connect(store.db_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM tag_inbox").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_large_history_budget_resume_has_no_missing_checks(monkeypatch):
    new = [TagCandidate(name=f"new{index}", summary="new", category_path=("群公共", "偏好")) for index in range(10)]
    old = [{"id": f"old{index}", "name": "old", "summary": "old", "revision": 0,
            "category_path": ["群公共", "偏好"]} for index in range(200)]
    client = SimpleNamespace(generate_response=AsyncMock(return_value=SimpleNamespace(
        response='{"decisions":[]}', prompt_tokens=100, completion_tokens=10, total_tokens=110)))
    monkeypatch.setattr("src.services.llm_service.LLMServiceClient", lambda **kwargs: client)
    monkeypatch.setattr("src.common.prompt_i18n.load_prompt", lambda name: "{input_json}")
    saved = None

    async def checkpoint(state):
        nonlocal saved
        saved = copy.deepcopy(state)

    for turn in range(25):
        before = client.generate_response.await_count
        try:
            result = await decide_tag_conflicts("group", new, old, progress=saved, checkpoint=checkpoint)
        except ConflictBudgetExceeded:
            assert turn < 24
        else:
            assert result == [] and turn == 24
        assert client.generate_response.await_count - before <= 4
    assert client.generate_response.await_count == 100
    assert saved["total_tokens"] == 11000
    inspected = [json.loads(call.args[0]) for call in client.generate_response.call_args_list]
    pairs = {(item["new_tag"]["name"], old_tag["id"]) for item in inspected for old_tag in item["old_tags"]}
    assert len(pairs) == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["database", "timeout"])
async def test_planner_and_replyer_continue_without_optional_tag_reference(monkeypatch, failure):
    from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator, MaisakaReplyContext
    from src.common.data_models.llm_service_data_models import LLMResponseResult
    from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
    from src.maisaka import reasoning_engine

    async def unavailable(*args):
        if failure == "database":
            raise sqlite3.OperationalError("database is locked")
        await asyncio.Event().wait()

    monkeypatch.setattr(tag_memory_recall, "_build_tag_memory_reference", unavailable)
    monkeypatch.setattr(tag_memory_recall, "RECALL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(reasoning_engine.heuristic_memory_injector, "build_injection_message", AsyncMock(return_value=""))
    monkeypatch.setattr(reasoning_engine.image_memory_injector, "build_injection_message", AsyncMock(return_value=""))
    monkeypatch.setattr(reasoning_engine, "build_person_profile_injection_messages", AsyncMock(return_value=[]))
    planner = object.__new__(reasoning_engine.MaisakaReasoningEngine)
    planner._runtime = SimpleNamespace(session_id="group", log_prefix="test")
    injected = await planner._build_planner_injected_user_messages(
        profile_message=None, source_messages=[], deferred_tools_reminder="planner still works",
    )
    assert injected == ["planner still works"]
    generator = object.__new__(BaseMaisakaReplyGenerator)
    generator._build_reply_context = AsyncMock(return_value=MaisakaReplyContext())
    generator._resolve_session_id = lambda stream: "group"
    generator._build_request_messages = Mock(return_value=[])
    generator._persist_reply_preview = Mock()
    generator._get_runtime_manager = lambda: SimpleNamespace(invoke_hook=AsyncMock(return_value=SimpleNamespace(kwargs={})))
    item = ContextItemBuilder().set_role(RoleType.Assistant).add_text_content("正常聊天回复").build()
    generator.express_model = SimpleNamespace(task_name="replyer", generate_response_with_context=AsyncMock(
        return_value=LLMResponseResult(output_items=(item,), model_name="mock")))
    generator.request_type = "maisaka.replyer"
    success, result = await generator.generate_reply_with_context(stream_id="group", chat_history=[], log_reply=False)
    assert success and result.completion.response_text == "正常聊天回复"
    generator.express_model.generate_response_with_context.assert_awaited_once()
    generator.express_model.generate_response_with_context.side_effect = RuntimeError("main reply error")
    success, result = await generator.generate_reply_with_context(stream_id="group", chat_history=[], log_reply=False)
    assert not success and "main reply error" in result.error_message
