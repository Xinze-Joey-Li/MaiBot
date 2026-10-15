"""Frozen references, bounded repair, explicit pair coverage and offline recovery."""
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import json
import sqlite3
import sys

import pytest

from pytests.services.test_tag_extraction import messages, output
from pytests.tag_helpers import pair_response
from src.services.tag_conflict_service import (
    ConflictBudgetExceeded, InvalidConflictOutput, decide_tag_conflicts, plan_conflict_pairs,
)
from src.services.tag_extraction_service import TagExtractionService, parse_tag_extraction
from src.services.tag_memory_service import tag_memory_service
from src.services.tag_memory_store import TagCandidate, TagMemoryStore


@pytest.fixture
def store(tmp_path):
    value = TagMemoryStore(tmp_path / "memory.db")
    value.initialize()
    value.initialize_scanner(0)
    return value


@pytest.fixture
def pipeline(store, monkeypatch):
    service = TagExtractionService()
    service._store = store
    extraction = SimpleNamespace(generate_response=AsyncMock())
    conflict = SimpleNamespace(generate_response=AsyncMock(side_effect=pair_response))
    factory = Mock(side_effect=lambda **kw: conflict if kw["request_type"].endswith("tag_conflict") else extraction)
    monkeypatch.setitem(sys.modules, "src.services.llm_service", SimpleNamespace(LLMServiceClient=factory))
    monkeypatch.setitem(sys.modules, "src.common.prompt_i18n", SimpleNamespace(load_prompt=lambda name: "{input_json}"))
    monkeypatch.setattr(tag_memory_service, "_resolve_scope", lambda scope: (scope, "测试群"))
    monkeypatch.setattr(tag_memory_service, "_verify_sources", Mock())
    return service, extraction, conflict


def response(text):
    return SimpleNamespace(generation_attempts=(), response=text, prompt_tokens=100, completion_tokens=10, total_tokens=110)


def old_batch(store):
    batch = store.record_batch(session_id="group", session_name="群", external_id="previous", summary="原始批次",
                               message_ids=["old"], tags=[TagCandidate("周末活动", "原始标签", ("群公共", "群活动"), ["old"])])
    with closing(sqlite3.connect(store.db_path)) as c, c:
        c.execute("INSERT INTO extraction_state VALUES(?,?,0)", ("group", batch["batch_id"]))
    return store.list_current_tags("group")[0]


def enqueue(store):
    store.enqueue_page(messages(), 25)
    return store.next_extraction_job(claim=True)


def test_snapshot_is_frozen_at_creation_and_serializes_session(store):
    old = old_batch(store)
    job = enqueue(store)
    before = json.loads(job["payload"])
    store.edit_tag("group", old["id"], revision=0, reason="changed", name="modified", summary="changed",
                   is_active=False, requires_long_term=False)
    assert store.next_extraction_job(claim=True) is None
    frozen = json.loads(store.freeze_extraction_job(job["id"])["payload"])
    assert frozen == before
    assert frozen["reference_snapshot"]["previous_extraction_tags"][0]["summary"] == "原始标签"
    restarted = TagMemoryStore(store.db_path)
    restarted.initialize()
    assert restarted.freeze_extraction_job(job["id"])["payload"] == store.freeze_extraction_job(job["id"])["payload"]


@pytest.mark.asyncio
async def test_bounded_targeted_reference_correction_uses_same_snapshot(store, pipeline):
    service, client, _ = pipeline
    old = old_batch(store)
    job = enqueue(store)
    client.generate_response.side_effect = [response(output(previous="foreign")), response(output(previous=old["id"]))]
    await service._process_job(job)
    assert client.generate_response.await_count == 2
    first, second = [call.args[0] for call in client.generate_response.call_args_list]
    assert second.startswith(first)
    assert "foreign" in second and old["id"] in second and "Correction" in second
    trace = store.job_trace("group", job["id"])
    assert trace["messages_consumed"] and trace["requests_with_usage"] == 3  # 2 extraction + 1 conflict
    assert trace["usage"]["total_tokens"] == 222
    assert len([event for event in trace["events"] if event["event"] == "committed"]) == 1
    with pytest.raises(ValueError):
        store.job_trace("other", job["id"])


@pytest.mark.asyncio
async def test_exhaustion_is_terminal_queryable_and_manual_retry_preserves_messages(store, pipeline):
    service, client, _ = pipeline
    job = enqueue(store)
    snapshot = json.loads(job["payload"])
    client.generate_response.return_value = response(output(previous="not-a-tag-id"))
    await service._run_job(job)
    assert client.generate_response.await_count == 3
    failed = store.list_extraction_jobs("group")[0]
    assert failed["status"] == "failed" and "not-a-tag-id" in failed["error"]
    assert store.extraction_status("group")["pending_messages"] == 25
    assert not store.job_trace("group", job["id"])["messages_consumed"]
    assert store.next_extraction_job() is None
    store.retry_extraction_job("group", job["id"])
    recovered = store.next_extraction_job(claim=True)
    assert json.loads(recovered["payload"]) == snapshot
    client.generate_response.return_value = response(output())
    await service._run_job(recovered)
    assert store.job_trace("group", job["id"])["messages_consumed"]
    assert store.extraction_status("group")["pending_messages"] == 0
    with closing(sqlite3.connect(store.db_path)) as c:
        assert c.execute("SELECT COUNT(*) FROM batches WHERE external_id=?", ("extract:" + job["id"],)).fetchone()[0] == 1


def tags(count=2):
    return [{"id": f"old{i}", "revision": 0, "status": "valid", "name": "偏好", "summary": f"旧事实{i}",
             "category_path": ["人物", "alice", "偏好"], "evidence": [{"message_id": f"m{i}", "quote": "旧事实"}]}
            for i in range(count)]


@pytest.fixture
def conflict_model(monkeypatch):
    client = SimpleNamespace(generate_response=AsyncMock(side_effect=pair_response))
    monkeypatch.setitem(sys.modules, "src.services.llm_service", SimpleNamespace(LLMServiceClient=lambda **kw: client))
    monkeypatch.setitem(sys.modules, "src.common.prompt_i18n", SimpleNamespace(load_prompt=lambda name: "{input_json}"))
    return client


@pytest.mark.asyncio
async def test_cache_scope_content_revision_rule_and_source_time_invalidation(store, conflict_model, monkeypatch):
    new = [TagCandidate("口味", "喜欢茶", ("人物", "alice", "偏好"), ["n"], evidence_quotes={"n": "茶"})]
    old = tags(1)

    async def run(scope="group", times=None):
        return await decide_tag_conflicts(scope, new, old, source_times=times,
            read_cache=lambda key: async_value(store.get_pair_cache(scope, key)),
            write_cache=lambda key, raw: async_value(store.cache_pair_result(scope, key, raw)))

    assert await run() == []
    assert await run() == []
    assert conflict_model.generate_response.await_count == 1
    old[0]["summary"] = "changed without revision"
    await run()
    old[0]["revision"] = 1
    await run()
    await run("other")
    await run(times={"n": 100})
    await run(times={"n": 200})
    monkeypatch.setattr("src.services.tag_conflict_service.CONFLICT_RULE_VERSION", "changed")
    await run()
    assert conflict_model.generate_response.await_count == 7


async def async_value(value):
    return value


@pytest.mark.asyncio
async def test_missing_batch_item_cached_valid_item_only_and_retry_checks_missing(conflict_model):
    cache, state = {}, {}
    new = [TagCandidate("偏好", "喝茶", ("人物", "alice", "偏好"))]

    def partial(prompt):
        value = pair_response(prompt)
        data = json.loads(value.response)
        data["results"] = data["results"][:1]
        value.response = json.dumps(data)
        return value

    conflict_model.generate_response.side_effect = partial
    async def checkpoint(value):
        saved = json.loads(json.dumps(value))
        state.clear()
        state.update(saved)
    kwargs = {"read_cache": lambda key: async_value(cache.get(key)),
              "write_cache": lambda key, raw: async_value(cache.update({key: raw})), "checkpoint": checkpoint}
    with pytest.raises(InvalidConflictOutput, match="Missing"):
        await decide_tag_conflicts("group", new, tags(), **kwargs)
    assert len(cache) == 1
    conflict_model.generate_response.side_effect = pair_response
    assert await decide_tag_conflicts("group", new, tags(), progress=state, **kwargs) == []
    retry = conflict_model.generate_response.call_args.args[0]
    assert len(json.loads(retry.split("\nCorrection:")[0])["pairs"]) == 1


@pytest.mark.asyncio
async def test_empty_or_invalid_batch_not_marked_completed(conflict_model):
    new = [TagCandidate("偏好", "喝茶", ("人物", "alice", "偏好"))]
    for raw in ('{"decisions":[]}', '{"results":[]}', '{"results":[{"candidate_id":"foreign","decision":"coexist","reason":"x"}]}'):
        conflict_model.generate_response.side_effect = None
        conflict_model.generate_response.return_value = response(raw)
        with pytest.raises(InvalidConflictOutput):
            await decide_tag_conflicts("group", new, tags())


def test_only_identical_content_and_source_are_local_duplicates():
    old = tags(1)
    candidate = TagCandidate("偏好", old[0]["summary"], ("人物", "alice", "偏好"), ["m0"], evidence_quotes={"m0": "旧事实"})
    assert plan_conflict_pairs("group", [candidate], old)[1] == 1
    candidate.evidence_quotes = {"m0": "旧"}
    assert len(plan_conflict_pairs("group", [candidate], old)[0]) == 1
    candidate.category_path = ("人物", "bob", "偏好")
    assert plan_conflict_pairs("group", [candidate], old) == ([], 0)
    old[0]["session_id"] = "other"
    with pytest.raises(ValueError, match="跨会话"):
        plan_conflict_pairs("group", [candidate], old)


@pytest.mark.asyncio
async def test_semantic_dates_context_preferences_still_go_to_model(conflict_model):
    new = [TagCandidate("昨天工作时喝茶", "今天休假喝咖啡", ("人物", "alice", "偏好"))]
    old = tags(3)
    old[0]["summary"] = "去年喜欢茶"
    old[1]["summary"] = "工作时喝茶"
    old[2]["summary"] = "同义表达但词汇无交集"
    assert await decide_tag_conflicts("group", new, old) == []
    data = json.loads(conflict_model.generate_response.call_args.args[0])
    assert len(data["pairs"]) == 3
    assert len(data["new_tags"]) == 1


@pytest.mark.asyncio
async def test_budget_checkpoint_does_not_repeat_completed_pairs(conflict_model):
    state = {}
    async def checkpoint(value):
        saved = json.loads(json.dumps(value))
        state.clear()
        state.update(saved)
    new = [TagCandidate("偏好", "喝茶", ("人物", "alice", "偏好"))]
    with pytest.raises(ConflictBudgetExceeded):
        await decide_tag_conflicts("group", new, tags(41), max_requests=1, checkpoint=checkpoint)
    assert len(state["results"]) == 40
    assert await decide_tag_conflicts("group", new, tags(41), progress=state, checkpoint=checkpoint) == []
    assert conflict_model.generate_response.await_count == 2


def test_identical_model_tags_deduplicated_but_changed_claim_rejected():
    payload = json.loads(output())
    payload["tags"] *= 2
    assert len(parse_tag_extraction(json.dumps(payload), messages(), []).tags) == 1
    payload["tags"][1] = {**payload["tags"][1], "summary": "different"}
    with pytest.raises(ValueError, match="重复"):
        parse_tag_extraction(json.dumps(payload), messages(), [])


@pytest.mark.asyncio
async def test_received_conflict_response_survives_usage_database_failure(conflict_model):
    saved = {}
    async def checkpoint(value):
        snapshot = json.loads(json.dumps(value))
        saved.clear()
        saved.update(snapshot)
    async def locked_usage(detail):
        raise sqlite3.OperationalError("database is locked")
    new = [TagCandidate("偏好", "喝茶", ("人物", "alice", "偏好"))]
    with pytest.raises(sqlite3.OperationalError):
        await decide_tag_conflicts("group", new, tags(), checkpoint=checkpoint, on_usage=locked_usage)
    assert saved["pending_response"]["output"]
    assert await decide_tag_conflicts("group", new, tags(), progress=saved, checkpoint=checkpoint) == []
    assert conflict_model.generate_response.await_count == 1


def test_usage_ledger_deduplicates_requests_and_marks_missing_usage(store):
    job = enqueue(store)
    store.record_job_event("group", job["id"], "model_request", {"request_id": "r"})
    assert store.job_trace("group", job["id"])["unknown_usage_requests"] == 1
    item = {"request_id": "r", "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    store.record_job_event("group", job["id"], "model_usage", item)
    store.record_job_event("group", job["id"], "model_usage", item)
    trace = store.job_trace("group", job["id"])
    assert trace["usage"]["total_tokens"] == 3
    assert trace["requests_with_usage"] == 1 and trace["unknown_usage_requests"] == 0


def test_legacy_job_gets_one_snapshot_not_a_claim_about_original_prompt(store):
    old_batch(store)
    job = enqueue(store)
    payload = json.loads(job["payload"])
    for key in ("reference_snapshot", "snapshot_id", "snapshot_origin"):
        payload.pop(key)
    with closing(sqlite3.connect(store.db_path)) as c, c:
        c.execute("UPDATE extraction_jobs SET payload=? WHERE id=?", (json.dumps(payload), job["id"]))
    recovered = json.loads(store.freeze_extraction_job(job["id"])["payload"])
    assert recovered["snapshot_origin"] == "legacy_first_resume"
    assert recovered == json.loads(store.freeze_extraction_job(job["id"])["payload"])


@pytest.mark.asyncio
async def test_new_same_category_tag_during_check_rolls_back_and_rechecks_only_new_pair(store, pipeline):
    service, client, conflict = pipeline
    old_batch(store)
    job = enqueue(store)
    client.generate_response.return_value = response(output())
    def concurrent_edit(prompt):
        store.record_batch(session_id="group", session_name="群", external_id="concurrent", summary="新增事实",
                           message_ids=["manual"], tags=[TagCandidate("其它计划", "新增事实", ("群公共", "群活动"), ["manual"])])
        return pair_response(prompt)
    conflict.generate_response.side_effect = concurrent_edit
    await service._run_job(job)
    assert store.extraction_status("group")["pending_messages"] == 25
    assert not store.job_trace("group", job["id"])["messages_consumed"]
    store.retry_extraction_job("group", job["id"])
    conflict.generate_response.side_effect = pair_response
    await service._run_job(store.next_extraction_job(claim=True))
    assert store.job_trace("group", job["id"])["messages_consumed"]
    assert client.generate_response.await_count == 1
    last = json.loads(conflict.generate_response.call_args.args[0])
    assert len(last["pairs"]) == 1  # The unchanged original pair is reusable.


@pytest.mark.asyncio
async def test_pair_cache_remaps_short_ids_when_candidate_order_changes(store, conflict_model):
    new = [TagCandidate("茶", "喝茶", ("人物", "alice", "偏好")), TagCandidate("咖啡", "喝咖啡", ("人物", "alice", "偏好"))]
    kwargs = {"read_cache": lambda key: async_value(store.get_pair_cache("group", key)),
              "write_cache": lambda key, raw: async_value(store.cache_pair_result("group", key, raw))}
    assert await decide_tag_conflicts("group", new, tags(), **kwargs) == []
    assert await decide_tag_conflicts("group", list(reversed(new)), tags(), **kwargs) == []
    assert conflict_model.generate_response.await_count == 1
