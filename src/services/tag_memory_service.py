"""标签索引的异步接入及持久化周期清理。"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio

from .tag_memory_store import TagCandidate, TagMemoryStore


class TagMemoryService:
    def __init__(self) -> None:
        self._store: Optional[TagMemoryStore] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_io_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        from src.config.config import global_config

        async with self._start_lock:
            if self._store is not None or not global_config.chat.tag_memory.enabled:
                return
            root = Path(__file__).resolve().parents[2]
            data_dir = Path(global_config.chat.tag_memory.data_dir)
            if not data_dir.is_absolute():
                data_dir = root / data_dir
            store = TagMemoryStore(data_dir / "memory_index.db")
            await asyncio.to_thread(store.initialize)
            self._store = store
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="tag_memory_cleanup")

    async def shutdown(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        # 取消 asyncio 任务不会停止已进入线程的 SQLite 事务；关闭时等待它释放连接。
        if self._cleanup_io_task is not None:
            await asyncio.gather(self._cleanup_io_task, return_exceptions=True)
            self._cleanup_io_task = None
        self._store = None

    async def _cleanup_loop(self) -> None:
        from src.common.logger import get_logger
        from src.config.config import global_config

        logger = get_logger("tag_memory")
        while True:
            try:
                config = global_config.chat.tag_memory
                if config.enabled and self._store is not None:
                    self._cleanup_io_task = asyncio.create_task(asyncio.to_thread(
                        self._store.cleanup_if_due, retention_days=config.retention_days,
                        interval_days=config.cleanup_interval_days,
                    ))
                    result = await asyncio.shield(self._cleanup_io_task)
                    self._cleanup_io_task = None
                    if result["ran"]:
                        logger.info(
                            f"标签定期清理完成: 删除标签={result['deleted_tags']} "
                            f"删除非活跃话题={result['deleted_topics']} "
                            f"清理批次摘要={result['archived_batches']}；保留活跃及长期标签"
                        )
                # 每分钟检查 24 小时手动保护是否到期；满 14 天（默认）才实际执行统一清理。
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._cleanup_io_task = None
                logger.exception("标签定期清理失败，清理进度未推进")
                await asyncio.sleep(60)

    async def _get_store(self) -> TagMemoryStore:
        from src.config.config import global_config

        if not global_config.chat.tag_memory.enabled:
            raise RuntimeError("标签记忆未启用")
        await self.start()
        if self._store is None:
            raise RuntimeError("标签数据库尚未初始化")
        return self._store

    @staticmethod
    def _resolve_scope(session_id: str) -> Tuple[str, str]:
        from src.chat.message_receive.chat_manager import chat_manager

        session = chat_manager.get_existing_session_by_session_id(session_id)
        if session is None:
            raise ValueError("无法解析已存在的真实聊天流，拒绝写入或读取标签")
        name = chat_manager.get_session_name(session.session_id)
        if not name:
            raise ValueError("真实聊天流没有可显示的名称")
        return session.session_id, name

    @staticmethod
    def _verify_sources(session_id: str, message_ids: List[str]) -> None:
        from src.common.message_repository import find_messages

        for message_id in dict.fromkeys(message_ids):
            if not find_messages(session_id=session_id, message_id=message_id, limit=1):
                raise ValueError(f"当前会话找不到来源消息: {message_id}")

    async def record_batch(
        self, *, session_id: str, external_id: str, summary: str,
        message_ids: List[str], tags: List[TagCandidate],
    ) -> Dict[str, Any]:
        store = await self._get_store()
        scope, name = await asyncio.to_thread(self._resolve_scope, session_id)
        await asyncio.to_thread(self._verify_sources, scope, message_ids)
        return await asyncio.to_thread(
            store.record_batch, session_id=scope, session_name=name, external_id=external_id,
            summary=summary, message_ids=message_ids, tags=tags,
        )

    async def get_tree(self, session_id: str) -> Dict[str, Any]:
        store = await self._get_store()
        scope, name = await asyncio.to_thread(self._resolve_scope, session_id)
        await asyncio.to_thread(store.ensure_scope, scope, name)
        return await asyncio.to_thread(store.get_tree, scope)

    async def get_batch_context(self, session_id: str, batch_id: str) -> List[Dict[str, Any]]:
        store = await self._get_store()
        scope, _ = await asyncio.to_thread(self._resolve_scope, session_id)
        return await asyncio.to_thread(store.get_batch_context, scope, batch_id)

    async def get_topics(self, session_id: str) -> List[Dict[str, Any]]:
        store = await self._get_store()
        scope, _ = await asyncio.to_thread(self._resolve_scope, session_id)
        return await asyncio.to_thread(store.get_topics, scope)

    async def get_conflicts(self, session_id: str) -> List[Dict[str, Any]]:
        """读取当前真实会话的冲突裁决及双方证据，不跨群查询。"""
        store = await self._get_store()
        scope, _ = await asyncio.to_thread(self._resolve_scope, session_id)
        return await asyncio.to_thread(store.get_conflicts, scope)

    async def get_person_profile(
        self, session_id: str, *, person_id: str = "", person_keyword: str = "", limit: int = 10,
    ) -> Dict[str, Any]:
        """会话内画像只由本群有效人物标签组成，不读取全局画像快照或人工覆盖。"""
        from src.chat.message_receive.chat_manager import chat_manager
        from src.person_info.person_info import resolve_person_id_for_memory

        store = await self._get_store()
        scope, _ = await asyncio.to_thread(self._resolve_scope, session_id)

        def query() -> Dict[str, Any]:
            session = chat_manager.get_existing_session_by_session_id(scope)
            if session is None:
                raise ValueError("真实聊天流已不存在")
            pid = person_id
            if not pid and person_keyword:
                pid = resolve_person_id_for_memory(person_name=person_keyword)
            users = store.list_person_user_ids(scope)
            matched = {user for user in users if resolve_person_id_for_memory(
                platform=session.platform, user_id=user) == pid}
            # 在数据库中过滤人物并限制条数，避免高频群每次画像查询加载整群证据。
            selected = store.list_current_tags(scope, person_users=list(matched), limit=max(1, min(limit, 20)))
            text = "\n".join(f"- {item['name']}：{item['summary']}" for item in selected)
            return {"success": True, "person_id": pid, "profile_text": text, "summary": text,
                    "profile_source": "session_tags", "chat_id": scope, "has_manual_override": False,
                    "uncertain_candidates": [], "evidence": [source for item in selected for source in item["evidence"]]}

        return await asyncio.to_thread(query)

    async def update_tag(
        self, session_id: str, tag_id: str, *, is_active: Optional[bool] = None,
        requires_long_term: Optional[bool] = None,
    ) -> None:
        store = await self._get_store()
        scope, _ = await asyncio.to_thread(self._resolve_scope, session_id)
        await asyncio.to_thread(
            store.update_tag, scope, tag_id, is_active=is_active, requires_long_term=requires_long_term,
        )


tag_memory_service = TagMemoryService()
