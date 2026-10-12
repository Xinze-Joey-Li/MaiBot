"""人工修改审计、软删除、跨群边界和模型裁决并发保护。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import json
import time

from src.services.tag_memory_store import StaleTagConflict, TagCandidate, TagMemoryStore


class TagManagementTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TagMemoryStore(Path(directory.name) / "memory.db")
        self.store.initialize()
        self.record("first", "周六爬山")
        self.old = self.store.list_current_tags("group")[0]

    def record(self, external, summary, **kwargs):
        return self.store.record_batch(session_id="group", session_name="测试群", external_id=external,
            summary=summary, message_ids=[external], tags=[TagCandidate("周末计划", summary,
                ("群公共", "群活动"), [external], evidence_quotes={external: summary})], **kwargs)

    def edit(self, **kwargs):
        defaults = dict(revision=self.old["revision"], reason="本人改期", name="周末计划", summary="周日看电影",
                        is_active=False, requires_long_term=True)
        defaults.update(kwargs)
        self.store.edit_tag("group", self.old["id"], **defaults)

    def test_edit_retains_quotes_and_records_before_after_content(self):
        self.edit()
        detail = self.store.tag_detail("group", self.old["id"])
        self.assertEqual(detail["summary"], "周日看电影")
        self.assertEqual(detail["evidence"][0]["quote"], "周六爬山")
        self.assertEqual(json.loads(detail["revisions"][0]["before_json"])["summary"], "周六爬山")
        self.assertEqual(detail["revision"], 1)

    def test_stale_edit_rejected_without_overwriting_newer_revision(self):
        self.edit()
        with self.assertRaises(StaleTagConflict):
            self.edit(summary="旧网页覆盖")
        self.assertEqual(self.store.tag_detail("group", self.old["id"])["summary"], "周日看电影")

    def test_soft_delete_immediately_removes_recall_and_tree(self):
        self.store.delete_tag("group", self.old["id"], 0, "记录有误")
        self.assertEqual(self.store.search_tags("group", "周末", []), [])
        self.assertEqual(self.store.list_current_tags("group"), [])
        with self.assertRaises(ValueError):
            self.store.tag_detail("group", self.old["id"])

    def test_group_boundary_and_required_reason(self):
        with self.assertRaises(ValueError):
            self.store.delete_tag("other", self.old["id"], 0, "误删")
        with self.assertRaises(ValueError):
            self.edit(reason="   ")
        self.assertEqual(self.store.tag_detail("group", self.old["id"])["revision"], 0)

    def test_old_llm_decision_cannot_overwrite_manual_edit(self):
        decision = [{"new_index": 0, "old_tag_id": self.old["id"], "old_revision": 0,
                     "decision": "replace_old", "reason": "模型建议"}]
        self.edit()
        with self.assertRaises(StaleTagConflict):
            self.record("second", "取消活动", conflict_decisions=decision)
        self.assertEqual(len(self.store.list_current_tags("group")), 1)
        self.assertEqual(self.store.get_conflicts("group"), [])

    def test_no_conflict_result_is_also_invalidated_after_manual_edit(self):
        self.edit()
        with self.assertRaises(StaleTagConflict):
            self.record("second", "新的消息", conflict_decisions=[], conflict_versions={self.old["id"]: 0})
        self.assertEqual(len(self.store.list_current_tags("group")), 1)

    def test_manual_conflict_resolution_replaces_model_result(self):
        self.record("second", "周日看电影", conflict_decisions=[{
            "new_index": 0, "old_tag_id": self.old["id"], "old_revision": 0, "decision": "uncertain", "reason": "证据不足"}])
        conflict = self.store.get_conflicts("group")[0]
        self.store.resolve_conflict("group", conflict["id"], "keep_old", "本人确认原计划")
        log = self.store.get_conflicts("group")[0]
        self.assertEqual(log["decision"], "keep_old")
        self.assertEqual(log["reason"], "本人确认原计划")
        self.assertEqual(log["manual_decision"], "keep_old")
        self.assertEqual(self.store.search_tags("group", "周末", [])[0]["id"], self.old["id"])
        with self.assertRaises(StaleTagConflict):
            self.store.resolve_conflict("group", conflict["id"], "replace_old", "旧网页覆盖")

    def test_older_conflict_is_removed_and_cannot_restore_obsolete_fact(self):
        self.record("second", "周日看电影", conflict_decisions=[{
            "new_index": 0, "old_tag_id": self.old["id"], "old_revision": 0, "decision": "replace_old", "reason": "改期"}])
        first_conflict = self.store.get_conflicts("group")[0]
        second = self.store.list_current_tags("group")[0]
        self.record("third", "取消周末活动", conflict_decisions=[{
            "new_index": 0, "old_tag_id": second["id"], "old_revision": second["revision"], "decision": "replace_old", "reason": "取消"}])
        self.assertEqual(len(self.store.get_conflicts("group")), 1)
        with self.assertRaises(ValueError):
            self.store.resolve_conflict("group", first_conflict["id"], "keep_old", "旧页面恢复")
        self.assertEqual([tag["summary"] for tag in self.store.search_tags("group", "周末", [])], ["取消周末活动"])

    def topic(self):
        with self.store._connect(write=True) as db:
            db.execute("INSERT INTO topics VALUES(?,?,?,?,?,?)", ("topic", "group", "周末计划", "活动", 0, time.time()))
            db.execute("INSERT INTO topic_tags VALUES(?,?,?)", ("group", "topic", self.old["id"]))

    def test_manual_topic_expires_after_one_day_and_can_be_cleaned(self):
        self.topic()
        start = time.time()
        with patch("src.services.tag_memory_store.time.time", return_value=start):
            self.store.set_topic_active("group", "topic", True)
        with self.store._connect(write=True) as db:
            self.store._update_topics(db, "group", None, [], start + 60)
        self.assertEqual(self.store.get_topics("group")[0]["is_active"], 1)
        self.assertEqual(self.store.expire_manual_activity(now=start + 86400 - 1), 0)
        self.assertEqual(self.store.expire_manual_activity(now=start + 86400), 1)
        self.assertEqual(self.store.get_topics("group")[0]["is_active"], 0)
        tag = self.store.tag_detail("group", self.old["id"])
        self.assertEqual(tag["is_active"], 0)
        self.assertEqual(tag["auto_active"], 0)
        self.assertEqual(self.store.cleanup_if_due(now=start + 15 * 86400)["deleted_tags"], 1)

    def test_expiration_preserves_automatic_activity_and_long_term_flags(self):
        self.topic()
        start = time.time()
        with self.store._connect(write=True) as db:
            db.execute("UPDATE tags SET auto_active=1 WHERE id=?", (self.old["id"],))
        self.store.set_topic_active("group", "topic", True)
        self.store.expire_manual_activity(now=start + 2 * 86400)
        tag = self.store.tag_detail("group", self.old["id"])
        self.assertEqual(tag["is_active"], 0)
        self.assertEqual(tag["auto_active"], 1)
        self.assertEqual(self.store.cleanup_if_due(now=start + 30 * 86400)["deleted_tags"], 0)
        self.store.set_topic_active("group", "topic", False)
        self.store.update_tag("group", self.old["id"], requires_long_term=True)
        self.assertEqual(self.store.cleanup_if_due(now=start + 60 * 86400)["deleted_tags"], 0)

    def test_restart_and_summary_edit_do_not_reset_manual_activation_deadline(self):
        self.topic()
        self.store.set_topic_active("group", "topic", True)
        before = self.store.tag_detail("group", self.old["id"])
        restarted = TagMemoryStore(self.store.db_path)
        restarted.initialize()
        restarted.edit_tag("group", self.old["id"], revision=before["revision"], reason="补充说明", name="周末活动",
                           summary="讨论周末安排", is_active=True, requires_long_term=False)
        self.assertEqual(restarted.tag_detail("group", self.old["id"])["manual_active_until"], before["manual_active_until"])
        restarted.expire_manual_activity(now=before["manual_active_until"])
        self.assertEqual(restarted.tag_detail("group", self.old["id"])["is_active"], 0)

    def test_version_five_conflict_chain_migrates_without_reviving_older_facts(self):
        self.record("second", "周日看电影", conflict_decisions=[{
            "new_index": 0, "old_tag_id": self.old["id"], "old_revision": 0, "decision": "replace_old", "reason": "改期"}])
        earlier = self.store.get_conflicts("group")[0]
        second = self.store.list_current_tags("group")[0]
        self.record("third", "取消周末活动", conflict_decisions=[{
            "new_index": 0, "old_tag_id": second["id"], "old_revision": second["revision"], "decision": "replace_old", "reason": "取消"}])
        with self.store._connect(write=True) as db:
            db.execute("INSERT INTO tag_conflicts(rowid,id,session_id,batch_id,old_tag_id,new_tag_id,decision,reason,evidence,created_at) "
                       "VALUES(0,?,?,?,?,?,?,?,?,?)", tuple(earlier[key] for key in (
                           "id", "session_id", "batch_id", "old_tag_id", "new_tag_id", "decision", "reason", "evidence", "created_at")))
            db.execute("UPDATE tags SET status='valid' WHERE id=?", (self.old["id"],))
            db.execute("PRAGMA user_version=5")
        restarted = TagMemoryStore(self.store.db_path)
        restarted.initialize()
        self.assertEqual(len(restarted.get_conflicts("group")), 1)
        self.assertEqual([tag["summary"] for tag in restarted.search_tags("group", "周末", [])], ["取消周末活动"])

    def test_legacy_manual_topic_migration_uses_original_activation_time(self):
        self.topic()
        start = time.time()
        with patch("src.services.tag_memory_store.time.time", return_value=start):
            self.store.set_topic_active("group", "topic", True)
        with self.store._connect(write=True) as db:
            db.execute("UPDATE tags SET auto_active=1 WHERE id=?", (self.old["id"],))
            db.execute("DROP INDEX IF EXISTS ix_tags_manual_deadline")
            db.execute("ALTER TABLE tags DROP COLUMN manual_active_until")
            db.execute("PRAGMA user_version=5")
        restarted = TagMemoryStore(self.store.db_path)
        with patch("src.services.tag_memory_store.time.time", return_value=start + 2 * 86400):
            restarted.initialize()
            tag = restarted.tag_detail("group", self.old["id"])
            self.assertEqual(tag["is_active"], 0)
            self.assertEqual(tag["auto_active"], 0)
            self.assertEqual(restarted.get_topics("group")[0]["is_active"], 0)

    def test_stale_conflict_cache_is_rejected_and_can_be_invalidated_independently(self):
        self.store.initialize_scanner(0)
        self.store.enqueue_page([{"session_id": "group", "message_id": "queued", "timestamp": 0}], 1)
        job = self.store.next_extraction_job()
        self.store.cache_extraction_output(job["id"], "validated extraction")
        self.edit()
        with self.assertRaises(StaleTagConflict):
            self.store.cache_conflict_output(job["id"], [{"old_tag_id": self.old["id"], "old_revision": 0}])
        self.store.fail_extraction_job(job["id"], "stale", invalid_output=False, invalid_conflict=True)
        with self.store._connect() as connection:
            row = connection.execute("SELECT model_output,conflict_output FROM extraction_jobs").fetchone()
        self.assertEqual(row[0], "validated extraction")
        self.assertIsNone(row[1])

    def test_cleanup_scrubs_deleted_side_of_retained_conflict_evidence(self):
        self.store.record_batch(session_id="group", session_name="测试群", external_id="second", summary="新计划",
            message_ids=["second"], tags=[TagCandidate("新计划", "长期讨论电影", ("群公共", "群活动"), ["second"],
                requires_long_term=True, evidence_quotes={"second": "长期讨论电影"})], conflict_decisions=[{
                    "new_index": 0, "old_tag_id": self.old["id"], "old_revision": 0, "decision": "uncertain", "reason": "待确认"}])
        self.store.delete_tag("group", self.old["id"], 1, "原记录不应保留")
        self.store.cleanup_if_due(now=time.time() + 15 * 86400)
        evidence = json.loads(self.store.get_conflicts("group")[0]["evidence"])
        self.assertEqual(evidence["old"]["sources"], [])
        self.assertNotIn("周六爬山", json.dumps(evidence, ensure_ascii=False))
        self.assertEqual(evidence["new"]["summary"], "长期讨论电影")
