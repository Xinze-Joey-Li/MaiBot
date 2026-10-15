"""批量阈值、模型证据、相邻提取活跃话题及持久化重试验证。"""

from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

import asyncio
import json
import sqlite3
import time

from pytests.tag_helpers import pair_response

from src.services.tag_extraction_service import InvalidTagOutput, TagExtractionService, parse_tag_extraction
from src.services.tag_memory_service import tag_memory_service
from src.services.tag_memory_store import TagMemoryStore


def messages(count=25, start=1, scope="group", timestamp=None):
    return [{"session_id": scope, "message_id": f"m{index}", "user_id": "user", "user_name": "群友",
             "text": "周末一起爬山", "timestamp": time.time() if timestamp is None else timestamp,
             "reply_to": None, "reply_context": []} for index in range(start, start + count)]


def output(message_id="m1", previous=None):
    return json.dumps({"summary": "讨论周末活动", "tags": [{"name": "周末活动", "summary": "计划一起爬山",
                      "category_path": ["群公共", "群活动"], "evidence": [{"message_id": message_id, "quote": "爬山"}],
                      "previous_tag_id": previous, "requires_long_term": False}]}, ensure_ascii=False)


class ExtractionParsingTest(TestCase):
    def test_evidence_and_previous_reference_are_preserved(self):
        parsed = parse_tag_extraction(output(previous="old"), messages(),
                                      [{"id": "old", "category_path": ["群公共", "群活动"]}])
        self.assertEqual(parsed.tags[0].previous_tag_id, "old")
        self.assertEqual(parsed.tags[0].evidence_quotes, {"m1": "爬山"})
        self.assertFalse(parsed.tags[0].is_active)

    def test_empty_tag_list_is_valid(self):
        self.assertEqual(parse_tag_extraction('{"summary":"普通闲聊","tags":[]}', messages(), []).tags, [])

    def test_wrong_source_quote_and_old_reference_are_rejected(self):
        valid = json.loads(output())
        for field, value in (("message_id", "unknown"), ("quote", "根本没有这句话")):
            candidate = json.loads(output())
            candidate["tags"][0]["evidence"][0][field] = value
            with self.assertRaises(ValueError):
                parse_tag_extraction(json.dumps(candidate), messages(), [])
        valid["tags"][0]["previous_tag_id"] = "earlier-than-previous"
        with self.assertRaises(ValueError):
            parse_tag_extraction(json.dumps(valid), messages(), [])

    def test_person_tag_requires_own_evidence_and_same_category(self):
        candidate = json.loads(output())
        candidate["tags"][0]["category_path"] = ["人物", "other-user", "偏好"]
        with self.assertRaisesRegex(ValueError, "本人"):
            parse_tag_extraction(json.dumps(candidate), messages(), [])
        with self.assertRaisesRegex(ValueError, "分类"):
            parse_tag_extraction(output(previous="old"), messages(),
                                 [{"id": "old", "category_path": ["群公共", "事件"]}])


class ExtractionQueueTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TagMemoryStore(Path(directory.name) / "index.db")
        self.store.initialize()
        self.store.initialize_scanner(0)

    def test_twenty_four_wait_and_twenty_five_trigger(self):
        self.store.enqueue_page(messages(24), 24)
        self.assertIsNone(self.store.next_extraction_job())
        self.store.enqueue_page(messages(1, start=25), 25)
        job = self.store.next_extraction_job()
        self.assertEqual(len(json.loads(job["payload"])["messages"]), 25)
        self.assertEqual(self.store.scanner_position(), 25)

    def test_high_traffic_group_uses_fifty_without_changing_other_group(self):
        self.store.enqueue_page(messages(49, scope="busy") + messages(25), 74)
        job = self.store.next_extraction_job(batch_sizes={"busy": 50}, claim=True)
        self.assertEqual(job["session_id"], "group")
        self.assertIsNone(self.store.next_extraction_job(batch_sizes={"busy": 50}, claim=True))
        self.store.enqueue_page(messages(1, scope="busy", start=50), 75)
        job = self.store.next_extraction_job(batch_sizes={"busy": 50}, claim=True)
        self.assertEqual(job["session_id"], "busy")
        self.assertEqual(len(json.loads(job["payload"])["messages"]), 50)

    def test_failed_group_does_not_block_ready_group_even_after_retry_is_due(self):
        self.store.enqueue_page(messages(), 25)
        with patch("src.services.tag_memory_store.time.time", return_value=100):
            job = self.store.next_extraction_job(claim=True)
            self.store.fail_extraction_job(job["id"], "model failed", invalid_output=True)
        self.store.enqueue_page(messages(scope="other"), 50)
        with patch("src.services.tag_memory_store.time.time", return_value=200):
            other = self.store.next_extraction_job(claim=True)
        self.assertEqual(other["session_id"], "other")
        self.assertEqual(self.store.extraction_status("group")["pending_messages"], 25)

    def test_lease_survives_restart_and_foreign_retry_is_rejected(self):
        self.store.enqueue_page(messages(), 25)
        with patch("src.services.tag_memory_store.time.time", return_value=100):
            job = self.store.next_extraction_job(claim=True)
            restarted = TagMemoryStore(self.store.db_path)
            restarted.initialize()
            self.assertIsNone(restarted.next_extraction_job(claim=True))
            with self.assertRaises(ValueError):
                restarted.retry_extraction_job("other", job["id"])
            with self.assertRaises(ValueError):
                restarted.retry_extraction_job("group", job["id"])
        with patch("src.services.tag_memory_store.time.time", return_value=341):
            recovered = restarted.next_extraction_job(claim=True)
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["payload"], job["payload"])

    def test_two_group_high_volume_is_fair_and_keeps_messages_after_restart(self):
        self.store.enqueue_page(messages(1000, scope="busy") + messages(25), 1025)
        first = self.store.next_extraction_job(batch_sizes={"busy": 50}, claim=True)
        second = self.store.next_extraction_job(batch_sizes={"busy": 50}, claim=True)
        self.assertEqual({first["session_id"], second["session_id"]}, {"busy", "group"})
        self.assertIsNone(self.store.next_extraction_job(batch_sizes={"busy": 50}, claim=True))
        restarted = TagMemoryStore(self.store.db_path)
        restarted.initialize()
        self.assertEqual(restarted.extraction_status("busy")["pending_messages"], 1000)
        self.assertEqual(restarted.extraction_status("group")["pending_messages"], 25)

    def test_half_hour_trigger_for_first_small_batch(self):
        self.store.enqueue_page(messages(1, timestamp=time.time() - 1801), 1)
        self.assertEqual(len(json.loads(self.store.next_extraction_job()["payload"])["messages"]), 1)

    def submit(self, job, now):
        payload = json.loads(job["payload"])
        return self.store.record_batch(
            session_id="group", session_name="测试群", external_id=f"extract:{job['id']}", summary="本批摘要",
            message_ids=[item["message_id"] for item in payload["messages"]], tags=[],
            extraction_job_id=job["id"], now=now,
        )

    def test_count_submission_resets_timer_even_when_remaining_messages_are_old(self):
        self.store.enqueue_page(messages(26, timestamp=0), 26)
        with patch("src.services.tag_memory_store.time.time", return_value=100):
            self.submit(self.store.next_extraction_job(), now=100)
        with patch("src.services.tag_memory_store.time.time", return_value=1899):
            self.assertIsNone(self.store.next_extraction_job())
        with patch("src.services.tag_memory_store.time.time", return_value=1900):
            self.assertEqual(len(json.loads(self.store.next_extraction_job()["payload"])["messages"]), 1)

    def test_every_count_submission_restarts_full_half_hour(self):
        self.store.enqueue_page(messages(25, timestamp=0), 25)
        with patch("src.services.tag_memory_store.time.time", return_value=100):
            self.submit(self.store.next_extraction_job(), now=100)
        self.store.enqueue_page(messages(26, start=26, timestamp=0), 51)
        with patch("src.services.tag_memory_store.time.time", return_value=500):
            self.submit(self.store.next_extraction_job(), now=500)
        with patch("src.services.tag_memory_store.time.time", return_value=1900):
            self.assertIsNone(self.store.next_extraction_job())
        with patch("src.services.tag_memory_store.time.time", return_value=2300):
            self.assertIsNotNone(self.store.next_extraction_job())

    def test_restart_preserves_submission_time_and_empty_queue_never_triggers(self):
        self.store.enqueue_page(messages(25, timestamp=0), 25)
        with patch("src.services.tag_memory_store.time.time", return_value=100):
            self.submit(self.store.next_extraction_job(), now=100)
        restarted = TagMemoryStore(self.store.db_path)
        restarted.initialize()
        with patch("src.services.tag_memory_store.time.time", return_value=3000):
            self.assertIsNone(restarted.next_extraction_job())
        restarted.enqueue_page(messages(1, start=26, timestamp=3000), 26)
        with patch("src.services.tag_memory_store.time.time", return_value=3000):
            self.assertIsNotNone(restarted.next_extraction_job())

    def test_failed_submission_does_not_reset_timer(self):
        self.store.enqueue_page(messages(25, timestamp=0), 25)
        with patch("src.services.tag_memory_store.time.time", return_value=100):
            self.submit(self.store.next_extraction_job(), now=100)
        self.store.enqueue_page(messages(25, start=26, timestamp=0), 50)
        with patch("src.services.tag_memory_store.time.time", return_value=500):
            job = self.store.next_extraction_job()
            self.store.fail_extraction_job(job["id"], "model failed", invalid_output=True)
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT last_submitted_at FROM extraction_state").fetchone()[0], 100)

    def test_version_two_upgrade_recovers_time_from_last_committed_batch(self):
        self.store.enqueue_page(messages(26, timestamp=0), 26)
        with patch("src.services.tag_memory_store.time.time", return_value=100):
            self.submit(self.store.next_extraction_job(), now=100)
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            connection.executescript("""
                ALTER TABLE extraction_state RENAME TO old_extraction_state;
                CREATE TABLE extraction_state(session_id TEXT PRIMARY KEY,
                                              last_batch_id TEXT NOT NULL REFERENCES batches(id));
                INSERT INTO extraction_state SELECT session_id,last_batch_id FROM old_extraction_state;
                DROP TABLE old_extraction_state;
                PRAGMA user_version=2;
            """)
        self.store.initialize()
        with patch("src.services.tag_memory_store.time.time", return_value=1899):
            self.assertIsNone(self.store.next_extraction_job())
        with patch("src.services.tag_memory_store.time.time", return_value=1900):
            self.assertIsNotNone(self.store.next_extraction_job())

    def test_restart_keeps_checkpoint_frozen_job_and_cached_result(self):
        self.store.enqueue_page(messages(), 25)
        job = self.store.next_extraction_job()
        self.store.cache_extraction_output(job["id"], output())
        restarted = TagMemoryStore(self.store.db_path)
        restarted.initialize()
        restarted.initialize_scanner(1000)
        self.assertEqual(restarted.scanner_position(), 25)
        recovered = restarted.next_extraction_job()
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["model_output"], output())

    def test_duplicate_delivery_does_not_fill_batch_and_sessions_do_not_mix(self):
        self.store.enqueue_page(messages(24), 24)
        self.store.enqueue_page(messages(24), 24)
        self.store.enqueue_page(messages(1, scope="other"), 25)
        self.assertIsNone(self.store.next_extraction_job())

    def test_failure_preserves_messages_and_clears_only_invalid_cached_output(self):
        self.store.enqueue_page(messages(), 25)
        job = self.store.next_extraction_job()
        self.store.cache_extraction_output(job["id"], output())
        self.store.fail_extraction_job(job["id"], "invalid", invalid_output=True)
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tag_inbox").fetchone()[0], 25)
            self.assertIsNone(connection.execute("SELECT model_output FROM extraction_jobs").fetchone()[0])

    def test_schema_upgrade_adds_fields_to_basic_version(self):
        legacy_path = self.store.db_path.parent / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE tags(id TEXT PRIMARY KEY, session_id TEXT, batch_id TEXT, category_id TEXT,
                                  name TEXT, summary TEXT, created_at REAL, last_active_at REAL,
                                  is_active INTEGER, requires_long_term INTEGER);
                CREATE TABLE tag_sources(tag_id TEXT, message_id TEXT, PRIMARY KEY(tag_id,message_id));
                PRAGMA user_version=1;
            """)
        store = TagMemoryStore(legacy_path)
        store.initialize()
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            self.assertIn("auto_active", {row[1] for row in connection.execute("PRAGMA table_info(tags)")})
            self.assertIn("quote", {row[1] for row in connection.execute("PRAGMA table_info(tag_sources)")})
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_scanner_keeps_short_and_mixed_text_but_excludes_bot_media_commands_and_private(self):
        class Text:
            def __init__(self, text):
                self.text = text

        class Column:
            def __gt__(self, value):
                return value

        rows = []
        for index, (user, parts, command, group) in enumerate([
            ("bot", [Text("机器人自己")], False, "group"),
            ("user", [object()], False, "group"),
            ("user", [Text("好"), object()], False, "group"),
            ("user", [Text("/命令")], True, "group"),
            ("user", [Text("私聊")], False, None),
            ("user", [Text("周末一起爬山")], False, "group"),
        ], 1):
            rows.append(SimpleNamespace(id=index, group_id=group, is_command=command, is_notify=False,
                                        platform="qq", user_id=user, parts=parts, user_cardname=None,
                                        user_nickname="群友", timestamp=datetime.now(), session_id="group",
                                        message_id=f"m{index}", reply_to=None))

        @contextmanager
        def session_factory(**kwargs):
            yield SimpleNamespace(exec=lambda statement: SimpleNamespace(all=lambda: rows))

        modules = {
            "sqlmodel": SimpleNamespace(select=Mock()),
            "src.chat.message_receive.message": SimpleNamespace(SessionMessage=SimpleNamespace(
                from_db_instance=lambda row: SimpleNamespace(raw_message=SimpleNamespace(components=row.parts))
            )),
            "src.common.data_models.message_component_data_model": SimpleNamespace(TextComponent=Text, ReplyComponent=type("Reply", (), {})),
            "src.common.database.database": SimpleNamespace(get_db_session=session_factory),
            "src.common.database.database_model": SimpleNamespace(Messages=SimpleNamespace(id=Column())),
            "src.services.bot_account_service": SimpleNamespace(is_bot_self=lambda platform, user: user == "bot"),
        }
        with patch.dict("sys.modules", modules):
            candidates, checkpoint, more = TagExtractionService._read_page(0)
        self.assertEqual([item["text"] for item in candidates], ["好", "周末一起爬山"])
        self.assertEqual(checkpoint, 6)
        self.assertFalse(more)


class ExtractionIntegrationTest(IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TagMemoryStore(Path(directory.name) / "index.db")
        self.store.initialize()
        self.store.initialize_scanner(0)
        self.service = TagExtractionService()
        self.service._store = self.store
        self.client = SimpleNamespace(generate_response=AsyncMock())
        self.conflict_client = SimpleNamespace(generate_response=AsyncMock(
            return_value=SimpleNamespace(generation_attempts=(), prompt_tokens=0, completion_tokens=0, total_tokens=0, response='{"decisions":[]}')))
        self.factory = Mock(side_effect=lambda **kwargs: self.conflict_client
                            if kwargs["request_type"] == "qq_agent.tag_conflict" else self.client)
        self.modules = {
            "src.services.llm_service": SimpleNamespace(LLMServiceClient=self.factory),
            "src.common.prompt_i18n": SimpleNamespace(load_prompt=lambda name: "{input_json}"),
        }

    def next_job(self, start=1):
        self.store.enqueue_page(messages(start=start), start + 24)
        return self.store.next_extraction_job()

    async def process(self, job, text):
        self.client.generate_response.return_value = SimpleNamespace(generation_attempts=(), prompt_tokens=0, completion_tokens=0, total_tokens=0, response=text)
        self.conflict_client.generate_response.side_effect = lambda prompt: pair_response(
            prompt, json.loads(self.conflict_client.generate_response.return_value.response)["decisions"])
        with patch.dict("sys.modules", self.modules), patch.object(
            tag_memory_service, "_resolve_scope", return_value=("group", "测试群")
        ), patch.object(tag_memory_service, "_verify_sources"):
            await self.service._process_job(job)
        if job["model_output"] is None:
            self.assertEqual(self.factory.call_args.kwargs["task_name"], "memory")

    async def test_two_consecutive_batches_create_active_topic_and_absence_deactivates(self):
        first = self.next_job()
        await self.process(first, output())
        self.assertEqual(self.store.get_topics("group"), [])
        first_context = self.store.get_batch_context("group", self._last_batch())
        previous_id = first_context[-1]["tags"][0]["id"]
        second = self.next_job(start=26)
        await self.process(second, output("m26", previous_id))
        topic = self.store.get_topics("group")[0]
        self.assertEqual(topic["is_active"], 1)
        self.assertEqual(len(topic["tag_ids"]), 2)
        self.assertEqual(len(self.store.get_topics("other")), 0)
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tags WHERE auto_active=1").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tag_inbox").fetchone()[0], 0)
        third = self.next_job(start=51)
        await self.process(third, '{"summary":"普通闲聊","tags":[]}')
        self.assertEqual(self.store.get_topics("group")[0]["is_active"], 0)

    def _last_batch(self):
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            return connection.execute("SELECT last_batch_id FROM extraction_state").fetchone()[0]

    async def test_invalid_output_does_not_consume_messages(self):
        job = self.next_job()
        with self.assertRaises(InvalidTagOutput):
            await self.process(job, output("unknown"))
        with closing(sqlite3.connect(self.store.db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tag_inbox").fetchone()[0], 25)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)

    async def test_commit_failure_reuses_cached_output_without_second_model_call(self):
        job = self.next_job()
        with patch.object(self.store, "record_batch", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                await self.process(job, output())
        recovered = self.store.next_extraction_job()
        await self.process(recovered, "this output should never be used")
        self.assertEqual(self.client.generate_response.await_count, 1)
        self.assertIsNone(self.store.next_extraction_job())

    async def test_active_topic_is_protected_then_cleanup_runs_after_deactivation(self):
        await self.process(self.next_job(), output())
        previous = self.store.get_batch_context("group", self._last_batch())[-1]["tags"][0]["id"]
        await self.process(self.next_job(start=26), output("m26", previous))
        self.assertEqual(self.store.cleanup_if_due(now=time.time() + 15 * 86400)["deleted_tags"], 0)
        await self.process(self.next_job(start=51), '{"summary":"已换话题","tags":[]}')
        cleaned = self.store.cleanup_if_due(now=time.time() + 30 * 86400)
        self.assertEqual(cleaned["deleted_topics"], 1)
        self.assertEqual(cleaned["deleted_tags"], 2)

    async def test_conflict_decision_is_separate_cached_and_committed_atomically(self):
        await self.process(self.next_job(), output())
        old_id = self.store.list_current_tags("group")[0]["id"]
        self.conflict_client.generate_response.return_value = SimpleNamespace(generation_attempts=(), prompt_tokens=0, completion_tokens=0, total_tokens=0, response=json.dumps({"decisions": [
            {"old_tag_id": old_id, "decision": "replace_old", "reason": "本人明确调整计划"}]}))
        job = self.next_job(start=26)
        with patch.object(self.store, "record_batch", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                await self.process(job, output("m26"))
        self.assertEqual(self.store.get_conflicts("group"), [])
        self.assertEqual(len(self.store.list_current_tags("group")), 1)
        cached = self.store.next_extraction_job()
        self.assertIsNotNone(cached["conflict_output"])
        await self.process(cached, "must not call extraction again")
        self.assertEqual(self.conflict_client.generate_response.await_count, 1)
        self.assertEqual(len(self.store.get_conflicts("group")), 1)
        self.assertNotEqual(self.store.list_current_tags("group")[0]["id"], old_id)
        self.assertIsNone(self.store.next_extraction_job())

    async def test_invalid_conflict_result_preserves_job_and_old_memory(self):
        await self.process(self.next_job(), output())
        self.conflict_client.generate_response.return_value = SimpleNamespace(generation_attempts=(), prompt_tokens=0, completion_tokens=0, total_tokens=0, response='{"decisions":[{"old_tag_id":"foreign"}]}')
        job = self.next_job(start=26)
        with self.assertRaises(ValueError):
            await self.process(job, output("m26"))
        self.assertEqual(len(self.store.list_current_tags("group")), 1)
        self.assertIsNotNone(self.store.next_extraction_job())
        self.assertEqual(self.store.get_conflicts("group"), [])

    async def test_slow_group_does_not_stop_scanning_or_second_group(self):
        started, finished, blocked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pages = [(messages(scope="slow"), 25, False), (messages(scope="other"), 50, False)]

        async def process(job):
            if job["session_id"] == "slow":
                started.set()
                await blocked.wait()
            else:
                finished.set()

        modules = {
            "src.config.config": SimpleNamespace(global_config=SimpleNamespace(chat=SimpleNamespace(
                tag_memory=SimpleNamespace(enabled=True, batch_size=25, max_wait_seconds=1800, batch_rules=[])))),
            "src.common.logger": SimpleNamespace(get_logger=lambda name: Mock()),
        }
        with patch.dict("sys.modules", modules), patch.object(self.service, "_process_job", side_effect=process), patch.object(
            self.service, "_read_page", side_effect=lambda position: pages.pop(0) if pages else ([], 50, False)
        ):
            self.service._worker = asyncio.create_task(self.service._worker_loop())
            try:
                await asyncio.wait_for(started.wait(), 3)
                self.service.notify_message_saved()
                await asyncio.wait_for(finished.wait(), 3)
                self.assertEqual(self.store.scanner_position(), 50)
                self.assertFalse(blocked.is_set())
            finally:
                await self.service.shutdown()

    async def test_background_worker_automatically_extracts_twenty_five_messages(self):
        done = Event()
        original = self.store.record_batch

        def record(**kwargs):
            result = original(**kwargs)
            done.set()
            return result

        self.client.generate_response.return_value = SimpleNamespace(generation_attempts=(), prompt_tokens=0, completion_tokens=0, total_tokens=0, response=output())
        self.modules.update({
            "src.config.config": SimpleNamespace(global_config=SimpleNamespace(chat=SimpleNamespace(
                tag_memory=SimpleNamespace(enabled=True, batch_size=25, max_wait_seconds=1800, batch_rules=[])
            ))),
            "src.common.logger": SimpleNamespace(get_logger=lambda name: Mock()),
        })
        with patch.dict("sys.modules", self.modules), patch.object(
            tag_memory_service, "_get_store", AsyncMock(return_value=self.store)
        ), patch.object(tag_memory_service, "_resolve_scope", return_value=("group", "测试群")), patch.object(
            tag_memory_service, "_verify_sources"
        ), patch.object(self.service, "_latest_message_id", return_value=0), patch.object(
            self.service, "_read_page", side_effect=[(messages(), 25, False), ([], 25, False)]
        ), patch.object(self.service, "batch_sizes_for_sessions", return_value={}), patch.object(self.store, "record_batch", side_effect=record):
            try:
                await self.service.start()
                self.service.notify_message_saved()
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(done.wait, 3), timeout=4))
            finally:
                await self.service.shutdown()
        self.assertEqual(self.client.generate_response.await_count, 1)
        self.assertEqual(self.store.scanner_position(), 25)
        self.assertIsNone(self.store.next_extraction_job())
