"""标签异步接入的真实会话校验、来源校验及生命周期验证。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import asyncio

from src.services.tag_memory_service import TagMemoryService
from src.services.tag_memory_store import TagCandidate


class TagMemoryServiceTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = TagMemoryService()
        self.config = SimpleNamespace(chat=SimpleNamespace(tag_memory=SimpleNamespace(
            enabled=True, data_dir=self.directory.name, retention_days=14, cleanup_interval_days=14,
        )))
        self.resolve = Mock(return_value=SimpleNamespace(session_id="actual-group"))
        self.find = Mock(return_value=[object()])
        self.modules = {
            "src.config.config": SimpleNamespace(global_config=self.config),
            "src.common.logger": SimpleNamespace(get_logger=lambda name: Mock()),
            "src.chat.message_receive.chat_manager": SimpleNamespace(chat_manager=SimpleNamespace(
                get_existing_session_by_session_id=self.resolve, get_session_name=lambda scope: "真实群名",
            )),
            "src.common.message_repository": SimpleNamespace(find_messages=self.find),
        }

    async def record(self):
        return await self.service.record_batch(
            session_id="requested-group", external_id="batch", summary="讨论活动", message_ids=["m1"],
            tags=[TagCandidate(name="活动", summary="一起爬山", category_path=("群公共", "群活动"),
                               source_message_ids=["m1"])],
        )

    async def test_real_scope_is_used_and_cleanup_task_is_managed(self) -> None:
        with patch.dict("sys.modules", self.modules):
            try:
                result = await self.record()
                context = await self.service.get_batch_context("requested-group", result["batch_id"])
                self.assertEqual(context[0]["session_id"], "actual-group")
                self.assertEqual(context[0]["tags"][0]["summary"], "一起爬山")
                self.assertEqual((await self.service.get_tree("requested-group"))["name"], "真实群名")
                self.assertTrue(Path(self.directory.name, "memory_index.db").exists())
                self.assertIsNotNone(self.service._cleanup_task)
            finally:
                await self.service.shutdown()
        self.find.assert_called_with(session_id="actual-group", message_id="m1", limit=1)
        self.assertIsNone(self.service._cleanup_task)

    async def test_unknown_session_is_rejected(self) -> None:
        self.resolve.return_value = None
        with patch.dict("sys.modules", self.modules):
            try:
                with self.assertRaisesRegex(ValueError, "真实聊天流"):
                    await self.record()
                self.find.assert_not_called()
            finally:
                await self.service.shutdown()

    async def test_missing_source_message_is_rejected(self) -> None:
        self.find.return_value = []
        with patch.dict("sys.modules", self.modules):
            try:
                with self.assertRaisesRegex(ValueError, "来源消息"):
                    await self.record()
                tree = await self.service.get_tree("requested-group")
                self.assertEqual(tree["name"], "真实群名")
                self.assertTrue(all(not child["tags"] for child in tree["children"]))
            finally:
                await self.service.shutdown()

    async def test_parallel_start_creates_one_worker(self) -> None:
        with patch.dict("sys.modules", self.modules):
            try:
                await asyncio.gather(self.service.start(), self.service.start())
                worker = self.service._cleanup_task
                await self.service.start()
                self.assertIs(self.service._cleanup_task, worker)
            finally:
                await self.service.shutdown()
