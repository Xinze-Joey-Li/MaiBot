"""冲突裁决证据、事务回滚、检索隔离和模型独立调用验证。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

import json

from src.services.tag_conflict_service import decide_tag_conflicts, parse_conflict_decisions
from src.services.tag_memory_store import TagCandidate, TagMemoryStore
from src.services.tag_memory_recall import build_tag_memory_reference
from src.services.tag_memory_service import tag_memory_service


class ConflictStoreTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TagMemoryStore(Path(directory.name) / "memory.db")
        self.store.initialize()
        self.old = self.record("old", "周六爬山")

    def record(self, external, text, **kwargs):
        return self.store.record_batch(session_id="group", session_name="群", external_id=external,
                                       summary=text, message_ids=[external], tags=[TagCandidate(
                                           name="周末计划", summary=text, category_path=("群公共", "群活动"),
                                           source_message_ids=[external], evidence_quotes={external: text})], **kwargs)

    def decide(self, action, old_id=None):
        return [{"new_index": 0, "old_tag_id": old_id or self.store.list_current_tags("group")[0]["id"],
                 "decision": action, "reason": "本人明确纠正原计划"}]

    def test_replacement_is_audited_and_only_new_fact_is_recalled_after_restart(self):
        self.record("new", "取消爬山，周日看电影", conflict_decisions=self.decide("replace_old"))
        restarted = TagMemoryStore(self.store.db_path)
        restarted.initialize()
        hits = restarted.search_tags("group", "周末计划", [])
        self.assertEqual([item["summary"] for item in hits], ["取消爬山，周日看电影"])
        log = restarted.get_conflicts("group")[0]
        evidence = json.loads(log["evidence"])
        self.assertEqual(evidence["old"]["sources"][0]["quote"], "周六爬山")
        self.assertEqual(evidence["new"]["evidence_quotes"]["new"], "取消爬山，周日看电影")

    def test_keep_old_discards_new_claim_from_recall(self):
        self.record("new", "周日看电影", conflict_decisions=self.decide("keep_old"))
        self.assertEqual([item["summary"] for item in self.store.search_tags("group", "周末", [])], ["周六爬山"])

    def test_uncertain_excludes_both_from_certain_memory(self):
        self.record("new", "周日看电影", conflict_decisions=self.decide("uncertain"))
        self.assertEqual(self.store.search_tags("group", "周末计划", []), [])
        self.assertEqual({item["status"] for item in self.store.list_current_tags("group")}, {"disputed"})

    def test_unknown_or_cross_group_old_tag_rolls_back_new_batch(self):
        with self.assertRaises(ValueError):
            self.record("new", "改期", conflict_decisions=self.decide("replace_old", "unknown"))
        self.assertEqual(len(self.store.list_current_tags("group")), 1)
        self.assertEqual(self.store.get_conflicts("group"), [])
        self.assertEqual(self.store.search_tags("other", "周末", []), [])

    def test_person_tags_are_only_recalled_for_participants(self):
        self.store.record_batch(session_id="group", session_name="群", external_id="person", summary="个人偏好",
                                message_ids=["person"], tags=[TagCandidate(name="口味", summary="喜欢咖啡",
                                    category_path=("人物", "alice", "偏好"), source_message_ids=["person"])])
        self.assertEqual(self.store.search_tags("group", "咖啡", ["bob"]), [])
        self.assertEqual(len(self.store.search_tags("group", "咖啡", ["alice"])), 1)

    def test_conflict_decision_cache_survives_restart(self):
        self.store.initialize_scanner(0)
        self.store.enqueue_page([{"session_id": "group", "message_id": "queued", "timestamp": 0}], 1)
        job = self.store.next_extraction_job()
        decisions = self.decide("replace_old")
        self.store.cache_conflict_output(job["id"], decisions)
        self.assertEqual(json.loads(TagMemoryStore(self.store.db_path).next_extraction_job()["conflict_output"])["decisions"], decisions)


class ConflictModelTest(IsolatedAsyncioTestCase):
    async def test_separate_memory_call_checks_same_category_even_with_different_names(self):
        client = SimpleNamespace(generate_response=AsyncMock(return_value=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0, response=json.dumps(
            {"decisions": [{"old_tag_id": "old", "decision": "replace_old", "reason": "明确改期"}]}))))
        factory = Mock(return_value=client)
        modules = {"src.services.llm_service": SimpleNamespace(LLMServiceClient=factory),
                   "src.common.prompt_i18n": SimpleNamespace(load_prompt=lambda name: "{input_json}")}
        new = TagCandidate("新的活动", "周日看电影", ("群公共", "群活动"))
        with patch.dict("sys.modules", modules):
            decisions = await decide_tag_conflicts("group", [new], [
                {"id": "old", "category_path": ["群公共", "群活动"], "name": "爬山"},
                {"id": "private", "category_path": ["人物", "alice", "偏好"]}])
        self.assertEqual(factory.call_args.kwargs["request_type"], "qq_agent.tag_conflict")
        self.assertEqual(factory.call_args.kwargs["task_name"], "memory")
        self.assertEqual(decisions[0]["new_index"], 0)
        self.assertNotIn("private", client.generate_response.call_args.args[0])
        prompt = client.generate_response.call_args.args[0]
        parsed_prompt = json.loads(prompt)
        self.assertEqual(list(parsed_prompt), ["old_tags", "new_tag"])
        self.assertEqual(parsed_prompt["new_tag"]["summary"], "周日看电影")
        self.assertEqual(prompt, json.dumps(parsed_prompt, ensure_ascii=False, separators=(",", ":")))

    async def test_no_history_needs_no_extra_model_request(self):
        self.assertEqual(await decide_tag_conflicts("group", [], []), [])

    async def test_completed_chunks_are_reused_after_partial_failure(self):
        cache = {}
        client = SimpleNamespace(generate_response=AsyncMock(side_effect=[
            SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0, response='{"decisions":[]}'), RuntimeError("rate limited"),
            SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0, response='{"decisions":[]}')]))
        modules = {"src.services.llm_service": SimpleNamespace(LLMServiceClient=Mock(return_value=client)),
                   "src.common.prompt_i18n": SimpleNamespace(load_prompt=lambda name: "{input_json}")}
        tags = [{"id": f"old{i}", "revision": 0, "category_path": ["群公共", "群活动"]} for i in range(21)]
        new = [TagCandidate("新计划", "爬山", ("群公共", "群活动"))]

        async def read(key):
            return cache.get(key)

        async def write(key, output):
            cache[key] = output

        with patch.dict("sys.modules", modules):
            with self.assertRaises(RuntimeError):
                await decide_tag_conflicts("group", new, tags, read_cache=read, write_cache=write)
            self.assertEqual(len(cache), 1)
            self.assertEqual(await decide_tag_conflicts("group", new, tags, read_cache=read, write_cache=write), [])
        self.assertEqual(client.generate_response.await_count, 3)

    async def test_invalid_ids_actions_and_missing_reasons_are_rejected(self):
        for item in [{"old_tag_id": "foreign", "decision": "replace_old", "reason": "x"},
                     {"old_tag_id": "old", "decision": "delete", "reason": "x"},
                     {"old_tag_id": "old", "decision": "uncertain", "reason": ""}]:
            with self.assertRaises(ValueError):
                parse_conflict_decisions(json.dumps({"decisions": [item]}), 0, [{"id": "old"}])

    async def test_recall_reference_uses_real_scope_and_source_evidence(self):
        store = SimpleNamespace(search_tags=Mock(return_value=[{
            "id": "tag", "name": "活动", "summary": "周末爬山", "category_path": ["群公共", "群活动"],
            "is_active": 0, "auto_active": 1, "created_at": 1,
            "evidence": [{"message_id": "m", "quote": "爬山"}]}]))
        message = SimpleNamespace(message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="alice")),
                                  processed_plain_text="周末什么计划")
        modules = {
            "src.config.config": SimpleNamespace(global_config=SimpleNamespace(chat=SimpleNamespace(
                tag_memory=SimpleNamespace(enabled=True)))),
            "src.common.message_repository": SimpleNamespace(find_messages=Mock(return_value=[message])),
            "src.common.prompt_i18n": SimpleNamespace(load_prompt=lambda name: "reference:{memory_json}"),
        }
        with patch.dict("sys.modules", modules), patch.object(tag_memory_service, "_get_store", AsyncMock(return_value=store)), patch.object(
            tag_memory_service, "_resolve_scope", return_value=("real-group", "真实群")
        ):
            reference = await build_tag_memory_reference("real-group")
        self.assertEqual(store.search_tags.call_args.args[0], "real-group")
        self.assertIn("alice", store.search_tags.call_args.args[2])
        recalled = json.loads(reference.removeprefix("reference:"))
        self.assertEqual(recalled[0]["evidence"][0]["message_id"], "m")
        self.assertIn("周末爬山", reference)
