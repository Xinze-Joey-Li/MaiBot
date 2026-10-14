"""按 25 条有效群文本提取标签，连续两批延续同一话题时自动标记活跃。"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import asyncio
import json
import time

from .tag_memory_service import tag_memory_service
from .tag_memory_store import StaleTagConflict, TagCandidate, TagMemoryStore
from .tag_conflict_service import ConflictBudgetExceeded, InvalidConflictOutput, decide_tag_conflicts


@dataclass
class TagExtractionResult:
    summary: str
    tags: List[TagCandidate]


class InvalidTagOutput(ValueError):
    """结构、证据或相邻引用不合格，需要重新提取。"""


def parse_tag_extraction(
    output: str, messages: List[Dict[str, Any]], previous_tags: List[Dict[str, Any]],
) -> TagExtractionResult:
    """严格校验结构、来源引文和相邻批次引用，不采用模糊匹配或无证据兜底。"""
    payload = json.loads(output)
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str):
        raise ValueError("标签提取必须返回含摘要的 JSON 对象")
    summary = payload["summary"].strip()
    items = payload.get("tags")
    if not summary or len(summary) > 1200 or not isinstance(items, list) or len(items) > 10:
        raise ValueError("批次摘要或标签列表无效")
    sources = {item["message_id"]: item for item in messages}
    previous = {item["id"]: item for item in previous_tags}
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("标签必须是对象")
        name, text, path = item.get("name"), item.get("summary"), item.get("category_path")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("标签名称无效")
        if not isinstance(text, str) or not text.strip() or len(text) > 600:
            raise ValueError("标签摘要无效")
        categories = {"偏好", "事件", "与机器人的互动", "群活动"}
        if not isinstance(path, list) or not all(isinstance(part, str) and part.strip() for part in path):
            raise ValueError("分类路径无效")
        if not ((len(path) == 2 and path[0] == "群公共" and path[1] in categories) or
                (len(path) == 3 and path[0] == "人物" and path[2] in categories)):
            raise ValueError("分类路径不属于约定的分类树")
        key = (tuple(path), name.strip())
        if key in seen:
            raise ValueError("同一批次包含重复标签")
        seen.add(key)
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("标签缺少原始发言证据")
        quotes = {}
        for entry in evidence:
            if not isinstance(entry, dict):
                raise ValueError("标签证据格式无效")
            message_id, quote = entry.get("message_id"), entry.get("quote")
            if not isinstance(message_id, str) or message_id not in sources:
                raise ValueError("证据消息不属于本批次")
            if not isinstance(quote, str) or not quote.strip() or quote not in sources[message_id]["text"]:
                raise ValueError("证据引文与原始文本不符")
            if message_id in quotes:
                raise ValueError("同一标签证据消息不能重复")
            quotes[message_id] = quote
        if path[0] == "人物" and not any(sources[key]["user_id"] == path[1] for key in quotes):
            raise ValueError("人物标签必须有本人原始发言支持")
        previous_id = item.get("previous_tag_id")
        if previous_id is not None:
            if not isinstance(previous_id, str) or previous_id not in previous:
                raise ValueError("延续引用不属于上一次提取")
            if previous[previous_id]["category_path"] != path:
                raise ValueError("延续话题的分类或人物不一致")
        long_term = item.get("requires_long_term", False)
        if not isinstance(long_term, bool):
            raise ValueError("长期记忆标记必须为布尔值")
        result.append(TagCandidate(name=name.strip(), summary=text.strip(), category_path=tuple(path),
                                   source_message_ids=list(quotes), requires_long_term=long_term,
                                   previous_tag_id=previous_id, evidence_quotes=quotes))
    return TagExtractionResult(summary=summary, tags=result)


class TagExtractionService:
    def __init__(self) -> None:
        self._store: Optional[TagMemoryStore] = None
        self._worker: Optional[asyncio.Task] = None
        self._jobs: Set[asyncio.Task] = set()
        self._io_tasks: Set[asyncio.Task] = set()
        self._wake = asyncio.Event()

    async def _io(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._io_tasks.discard(task)

    @staticmethod
    def _latest_message_id() -> int:
        from sqlmodel import select
        from src.common.database.database import get_db_session
        from src.common.database.database_model import Messages

        with get_db_session(auto_commit=False) as session:
            row = session.exec(select(Messages.id).order_by(Messages.id.desc()).limit(1)).first()
            return row if row is not None else 0

    @staticmethod
    def _read_page(last_id: int) -> Tuple[List[Dict[str, Any]], int, bool]:
        from sqlmodel import select
        from src.chat.message_receive.message import SessionMessage
        from src.common.data_models.message_component_data_model import ReplyComponent, TextComponent
        from src.common.database.database import get_db_session
        from src.common.database.database_model import Messages
        from src.services.bot_account_service import is_bot_self

        candidates = []
        with get_db_session(auto_commit=False) as session:
            rows = session.exec(select(Messages).where(Messages.id > last_id).order_by(Messages.id).limit(200)).all()
            for row in rows:
                if not row.group_id or row.is_command or row.is_notify or is_bot_self(row.platform, row.user_id):
                    continue
                message = SessionMessage.from_db_instance(row)
                # 只取真实文本组件，不能将图片描述、表情描述或附件占位当作用户发言。
                text = "".join(
                    part.text for part in message.raw_message.components if isinstance(part, TextComponent)
                ).strip()
                if not text:
                    continue
                candidates.append({"session_id": row.session_id, "message_id": row.message_id,
                                   "user_id": row.user_id, "user_name": row.user_cardname or row.user_nickname,
                                   "timestamp": row.timestamp.timestamp(), "text": text, "reply_to": row.reply_to,
                                   "reply_context": [part.target_message_content
                                                     for part in message.raw_message.components
                                                     if isinstance(part, ReplyComponent)]})
            return candidates, rows[-1].id if rows else last_id, len(rows) == 200

    async def start(self) -> None:
        from src.config.config import global_config

        if self._worker is not None or not global_config.chat.tag_memory.enabled:
            return
        self._store = await tag_memory_service._get_store()
        # 只在首次启用建立当前位置，已有扫描进度不会被重启覆盖。
        latest_id = await self._io(self._latest_message_id)
        await self._io(self._store.initialize_scanner, latest_id)
        if await self._io(self._store.scanner_position) > latest_id:
            raise RuntimeError("标签扫描游标超过主消息库最大 ID；请成套恢复消息库与标签库，拒绝静默跳过新消息")
        self._worker = asyncio.create_task(self._worker_loop(), name="tag_extraction")

    def notify_message_saved(self) -> None:
        self._wake.set()

    @staticmethod
    def batch_sizes_for_sessions(rules: List[Any]) -> Dict[str, int]:
        if not rules:
            return {}
        from src.common.utils.utils_config import ChatConfigUtils

        result = {}
        for rule in rules:
            target = {"platform": rule.platform, "item_id": rule.item_id, "type": "group"}
            for session_id in ChatConfigUtils.get_target_session_ids(target):
                result[session_id] = rule.batch_size
        return result

    async def _run_job(self, job: Dict[str, Any]) -> None:
        from src.common.logger import get_logger

        logger = get_logger("tag_extraction")
        started = time.monotonic()
        success = False
        cancelled = False
        deferred = False
        try:
            # 限制单群故障占用槽位的时间，另一个群仍可独立完成提取。
            await asyncio.wait_for(self._process_job(job), timeout=180)
            success = True
            logger.info(f"批量标签提取完成: session_id={job['session_id']} job_id={job['id']}")
        except ConflictBudgetExceeded:
            deferred = True
            if self._store is not None:
                await self._io(self._store.defer_extraction_job, job["id"])
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as error:
            logger.exception(f"批量标签提取失败: session_id={job['session_id']} job_id={job['id']}；消息保留待重试")
            if self._store is not None:
                try:
                    await self._io(self._store.fail_extraction_job, job["id"], str(error) or type(error).__name__,
                                   invalid_output=isinstance(error, InvalidTagOutput),
                                   invalid_conflict=isinstance(error, (StaleTagConflict, InvalidConflictOutput)))
                except Exception:
                    logger.exception("标签提取失败状态写入数据库失败")
        finally:
            if self._store is not None and not cancelled and not deferred:
                try:
                    await self._io(self._store.record_extraction_metrics, job["session_id"],
                                   time.monotonic() - started, success)
                except Exception:
                    logger.exception("标签提取耗时统计写入失败")
            self._wake.set()

    async def _process_job(self, job: Dict[str, Any]) -> None:
        from src.common.prompt_i18n import load_prompt
        from src.services.llm_service import LLMServiceClient

        store = self._store
        if store is None:
            raise RuntimeError("标签提取数据库尚未初始化")
        payload = json.loads(job["payload"])
        context = []
        if payload["previous_batch_id"] is not None:
            context = await self._io(store.get_batch_context, job["session_id"], payload["previous_batch_id"])
        previous_tags = next((batch["tags"] for batch in context if batch["id"] == payload["previous_batch_id"]), [])
        output = job["model_output"]
        if output is None:
            output = await self._io(store.read_job_cache, job["id"], "output")
        if output is None:
            # 相邻任务共享的前文放在消息之前；只压缩 JSON 结构空白，不裁掉证据。
            prompt_data = {"previous_batches": context[-3:], "previous_extraction_tags": previous_tags,
                           "messages": payload["messages"]}
            template = await self._io(load_prompt, "tag_extraction")
            prompt = template.replace("{input_json}", json.dumps(prompt_data, ensure_ascii=False, separators=(",", ":")))
            client = LLMServiceClient(
                task_name="memory", request_type="qq_agent.tag_extraction", session_id=job["session_id"]
            )
            await self._io(store.record_model_request, job["session_id"])
            response = await client.generate_response(prompt)
            output = response.response
        try:
            parsed = parse_tag_extraction(output, payload["messages"], previous_tags)
        except (ValueError, TypeError) as error:
            raise InvalidTagOutput(str(error)) from error
        # 已校验输出先持久化；数据库提交失败时重用结果，不重复支付模型调用。
        await self._io(store.write_job_cache, job["id"], "output", output)
        await self._io(store.cache_extraction_output, job["id"], output)
        scope, name = await self._io(tag_memory_service._resolve_scope, job["session_id"])
        await self._io(tag_memory_service._verify_sources, scope, [item["message_id"] for item in payload["messages"]])
        # 冲突检查是独立模型请求；裁决缓存让数据库重试不重复调用模型。
        if job.get("conflict_output") is None:
            old_tags = await self._io(store.list_current_tags, scope)

            async def read_chunk(key: str) -> Optional[str]:
                return await self._io(store.get_conflict_chunk, job["id"], key)

            async def write_chunk(key: str, output: str) -> None:
                await self._io(store.cache_conflict_chunk, job["id"], key, output)

            async def count_request() -> None:
                await self._io(store.record_model_request, scope, conflict=True)

            progress = await self._io(store.read_job_cache, job["id"], "conflicts")

            async def checkpoint(state: Dict[str, Any]) -> None:
                await self._io(store.write_job_cache, job["id"], "conflicts", state)

            decisions = await decide_tag_conflicts(scope, parsed.tags, old_tags, read_cache=read_chunk,
                                                  write_cache=write_chunk, on_request=count_request,
                                                  progress=progress, checkpoint=checkpoint)
            checked = {tag["id"]: tag["revision"] for tag in old_tags if any(
                tag["category_path"] == list(candidate.category_path) for candidate in parsed.tags)}
            await self._io(store.cache_conflict_output, job["id"], decisions, checked)
        else:
            cached = json.loads(job["conflict_output"])
            if not isinstance(cached, dict):
                raise StaleTagConflict("旧版冲突缓存需要重新检查标签修订版本")
            decisions, checked = cached["decisions"], cached["checked_tags"]
        await self._io(store.record_batch, session_id=scope, session_name=name, external_id=f"extract:{job['id']}",
                       summary=parsed.summary, message_ids=[item["message_id"] for item in payload["messages"]],
                       tags=parsed.tags, extraction_job_id=job["id"], conflict_decisions=decisions, conflict_versions=checked)
        for kind in ("output", "conflicts", "failures"):
            await self._io(store.remove_job_cache, job["id"], kind)

    async def _worker_loop(self) -> None:
        from src.common.logger import get_logger
        from src.config.config import global_config

        logger = get_logger("tag_extraction")
        while True:
            try:
                self._wake.clear()
                config = global_config.chat.tag_memory
                if config.enabled:
                    store = self._store
                    if store is None:
                        raise RuntimeError("标签提取数据库尚未初始化")
                    position = await self._io(store.scanner_position)
                    candidates, last_id, more = await self._io(self._read_page, position)
                    await self._io(store.enqueue_page, candidates, last_id)
                    finished = {task for task in self._jobs if task.done()}
                    if finished:
                        await asyncio.gather(*finished)
                        self._jobs.difference_update(finished)
                    batch_sizes = await self._io(self.batch_sizes_for_sessions, config.batch_rules)
                    # 扫描不等待模型；总并发固定为二，同一群由持久化租约保证串行。
                    while len(self._jobs) < 2:
                        job = await self._io(store.next_extraction_job, config.batch_size,
                                             config.max_wait_seconds, claim=True, batch_sizes=batch_sizes)
                        if job is None:
                            break
                        self._jobs.add(asyncio.create_task(self._run_job(job), name=f"tag_extract:{job['session_id']}"))
                    if more:
                        await asyncio.sleep(0)
                        continue
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("批量标签提取失败，消息和任务保留待重试")
                await asyncio.sleep(60)

    async def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        for task in self._jobs:
            task.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)
            self._jobs.clear()
        if self._io_tasks:
            await asyncio.gather(*self._io_tasks, return_exceptions=True)
            self._io_tasks.clear()
        self._store = None


tag_extraction_service = TagExtractionService()
