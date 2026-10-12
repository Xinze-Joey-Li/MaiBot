"""用真实临时 SQLite 验证标签树、事务、作用域及持久化清理。"""

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import sqlite3

from src.services.tag_memory_store import DAY_SECONDS, TagCandidate, TagMemoryStore


class TagMemoryStoreTest(TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "memory_index.db"
        self.store = TagMemoryStore(self.path)
        self.store.initialize()

    def batch(self, external_id="batch-1", *, scope="group-a", tags=None, now=0):
        return self.store.record_batch(
            session_id=scope, session_name="测试群", external_id=external_id, summary="讨论周末活动",
            message_ids=["m1", "m2"], tags=tags if tags is not None else [self.tag()], now=now,
        )

    @staticmethod
    def tag(**values):
        params = dict(name="周末活动", summary="周末一起爬山", category_path=("群公共", "群活动"),
                      source_message_ids=["m1"])
        params.update(values)
        return TagCandidate(**params)

    @staticmethod
    def flatten(tree):
        if not tree:
            return []
        return tree["tags"] + [tag for child in tree["children"] for tag in TagMemoryStoreTest.flatten(child)]

    def test_tree_has_scope_name_categories_summary_and_evidence(self) -> None:
        self.batch()
        tree = self.store.get_tree("group-a")
        self.assertEqual(tree["name"], "测试群")
        self.assertEqual({node["name"] for node in tree["children"]}, {"人物", "群公共"})
        tags = self.flatten(tree)
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0]["summary"], "周末一起爬山")
        self.assertEqual(tags[0]["source_message_ids"], ["m1"])

    def test_nested_person_category(self) -> None:
        self.batch(tags=[self.tag(category_path=("人物", "user-1", "偏好"))])
        person = next(node for node in self.store.get_tree("group-a")["children"] if node["name"] == "人物")
        user = next(node for node in person["children"] if node["name"] == "user-1")
        self.assertEqual(user["children"][0]["tags"][0]["name"], "周末活动")

    def test_scope_isolation_and_same_external_id_in_different_groups(self) -> None:
        first = self.batch()
        second = self.batch(scope="group-b")
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(self.store.get_tree("unknown"), {})
        self.assertEqual(self.store.get_batch_context("group-b", first["batch_id"]), [])

    def test_idempotent_replay_and_conflicting_payload(self) -> None:
        first = self.batch()
        replay = self.batch(now=100)
        self.assertEqual(first["batch_id"], replay["batch_id"])
        self.assertFalse(replay["created"])
        self.assertEqual(len(self.flatten(self.store.get_tree("group-a"))), 1)
        with self.assertRaisesRegex(ValueError, "输入发生变化"):
            self.batch(tags=[self.tag(summary="不同摘要")])

    def test_previous_and_future_three_batches_are_linked(self) -> None:
        ids = [self.batch(f"batch-{index}", now=index)["batch_id"] for index in range(8)]
        context = self.store.get_batch_context("group-a", ids[3])
        self.assertEqual([row["id"] for row in context], ids[:7])
        self.assertEqual([row["id"] for row in self.store.get_batch_context("group-a", ids[-1])], ids[4:])

    def test_invalid_evidence_is_rejected(self) -> None:
        for sources in ([], ["not-in-batch"]):
            with self.assertRaisesRegex(ValueError, "证据"):
                self.batch(tags=[self.tag(source_message_ids=sources)])
        self.assertEqual(self.store.get_tree("group-a"), {})

    def test_failed_batch_is_rolled_back(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.batch(tags=[self.tag(), self.tag()])
        self.assertEqual(self.store.get_tree("group-a"), {})
        self.assertTrue(self.batch()["created"])

    def test_cleanup_preserves_active_and_long_term_and_recent_tags(self) -> None:
        self.batch("old", tags=[self.tag(name="普通"), self.tag(name="活跃"),
                                self.tag(name="长期", requires_long_term=True)], now=0)
        with self.store._connect(write=True) as connection:
            connection.execute("UPDATE tags SET auto_active=1 WHERE name='活跃'")
        self.batch("recent", tags=[self.tag(name="近期")], now=13 * DAY_SECONDS)
        result = self.store.cleanup_if_due(now=14 * DAY_SECONDS)
        self.assertEqual(result["deleted_tags"], 1)
        self.assertEqual(result["archived_batches"], 0)
        self.assertEqual({tag["name"] for tag in self.flatten(self.store.get_tree("group-a"))}, {"活跃", "长期", "近期"})

    def test_cleanup_schedule_survives_restart(self) -> None:
        self.store.cleanup_if_due(now=0)
        self.batch(now=0)
        restarted = TagMemoryStore(self.path)
        restarted.initialize()
        self.assertFalse(restarted.cleanup_if_due(now=13 * DAY_SECONDS)["ran"])
        self.assertTrue(restarted.cleanup_if_due(now=14 * DAY_SECONDS)["ran"])
        self.assertEqual(self.flatten(restarted.get_tree("group-a")), [])

    def test_touch_extends_retention(self) -> None:
        self.batch(now=0)
        tag_id = self.flatten(self.store.get_tree("group-a"))[0]["id"]
        self.store.update_tag("group-a", tag_id, now=10 * DAY_SECONDS)
        result = self.store.cleanup_if_due(now=14 * DAY_SECONDS)
        self.assertEqual(result["deleted_tags"], 0)

    def test_protection_flags_can_be_updated_without_cross_scope_access(self) -> None:
        self.batch(now=0)
        tag_id = self.flatten(self.store.get_tree("group-a"))[0]["id"]
        with self.assertRaises(ValueError):
            self.store.update_tag("group-b", tag_id, is_active=True)
        self.store.update_tag("group-a", tag_id, requires_long_term=True, now=0)
        self.assertEqual(self.store.cleanup_if_due(now=100 * DAY_SECONDS)["deleted_tags"], 0)
        self.store.update_tag("group-a", tag_id, requires_long_term=False, now=100 * DAY_SECONDS)
        self.assertEqual(self.store.cleanup_if_due(now=114 * DAY_SECONDS)["deleted_tags"], 1)

    def test_cleanup_scrubs_batch_content_but_keeps_idempotency_identity(self) -> None:
        batch = self.batch(now=0)
        result = self.store.cleanup_if_due(now=14 * DAY_SECONDS)
        self.assertEqual(result["archived_batches"], 1)
        self.assertEqual(self.store.get_batch_context("group-a", batch["batch_id"]), [])
        self.assertFalse(self.batch(now=20 * DAY_SECONDS)["created"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT summary FROM batches").fetchone()[0], "")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tag_sources").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM batch_sources").fetchone()[0], 0)

    def test_empty_tag_batch_is_allowed_and_later_cleaned(self) -> None:
        self.batch(tags=[])
        self.assertEqual(self.store.cleanup_if_due(now=14 * DAY_SECONDS)["archived_batches"], 1)

    def test_foreign_keys_prevent_cross_scope_relations(self) -> None:
        a = self.batch(scope="group-a")["batch_id"]
        b = self.batch(scope="group-b")["batch_id"]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO batch_links VALUES(?,?,?)", ("group-a", a, b))
