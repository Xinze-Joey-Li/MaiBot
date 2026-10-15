"""Planner 与 Replyer 共用的按群标签检索和一次性参考构造。"""

from contextvars import ContextVar
from typing import List, Optional, Tuple

import asyncio
import json

from src.decision_replay.recorder import record
from src.common.logger import get_logger

from .tag_memory_service import tag_memory_service

logger = get_logger("tag_memory_recall")
RECALL_TIMEOUT_SECONDS = 2.0
last_reference_ids: ContextVar[Tuple[str, ...]] = ContextVar("tag_reference_ids", default=())
last_reference_text: ContextVar[str] = ContextVar("tag_reference_text", default="")
last_reference_session: ContextVar[str] = ContextVar("tag_reference_session", default="")


def record_tag_injection(session_id: str, target: str, request_id: Optional[str] = None) -> None:
    record("memory", "memory.injected", "injected", "context_reference", session_id=session_id,
           detail={"tag_ids": last_reference_ids.get(), "target": target}, request_id=request_id)


async def build_tag_memory_reference(session_id: str, text: str = "", user_ids: Optional[List[str]] = None) -> str:
    """仅隔离可选召回，取消信号和主回复错误由调用方处理。"""
    last_reference_ids.set(())
    last_reference_text.set("")
    last_reference_session.set(session_id)
    try:
        async def recall():
            reference = await _build_tag_memory_reference(session_id, text, user_ids)
            return reference, last_reference_ids.get()
        reference, ids = await asyncio.wait_for(recall(), timeout=RECALL_TIMEOUT_SECONDS)
        last_reference_ids.set(ids)
        last_reference_text.set(reference)
        return reference
    except TimeoutError:
        record("memory", "memory.recall", "failed", "recall_timeout", session_id=session_id)
        logger.warning(f"标签召回超时，继续无标签回复: session_id={session_id} timeout={RECALL_TIMEOUT_SECONDS}s")
    except Exception:
        record("memory", "memory.recall", "failed", "recall_error", session_id=session_id)
        logger.exception(f"标签召回失败，继续无标签回复: session_id={session_id}")
    return ""


async def _build_tag_memory_reference(session_id: str, text: str, user_ids: Optional[List[str]]) -> str:
    from src.common.message_repository import find_messages
    from src.common.prompt_i18n import load_prompt
    from src.config.config import global_config

    if not global_config.chat.tag_memory.enabled:
        record("memory", "memory.recall", "disabled", "feature_disabled", session_id=session_id)
        return ""
    scope, _ = await asyncio.to_thread(tag_memory_service._resolve_scope, session_id)
    # 主动发言没有明确回复对象时也以最近真实聊天为检索条件。
    messages = await asyncio.to_thread(find_messages, session_id=scope, limit=20, limit_mode="latest", filter_command=True)
    participants = list(user_ids or [])
    query = [text]
    for message in messages:
        participants.append(str(message.message_info.user_info.user_id))
        if message.processed_plain_text is not None:
            query.append(message.processed_plain_text)
    store = await tag_memory_service._get_store()
    hits = await asyncio.to_thread(store.search_tags, scope, "\n".join(query), list(set(participants)))
    if not hits:
        record("memory", "memory.recall", "empty", "no_hits", session_id=session_id)
        return ""
    # 不把整棵树和相邻批次全文塞入提示词，最多召回八个有证据的标签。
    data = [{"id": item["id"], "category_path": item["category_path"], "name": item["name"],
             "summary": item["summary"], "is_active": bool(item["is_active"] or item["auto_active"]),
             "created_at": item["created_at"], "evidence": [
                 {"message_id": source["message_id"], "quote": source["quote"][:240]}
                 for source in item["evidence"][:3]]} for item in hits]
    template = await asyncio.to_thread(load_prompt, "tag_memory_reference")
    last_reference_ids.set(tuple(item['id'] for item in data))
    record('memory', 'memory.reference_built', 'available', 'reference_built', session_id=session_id,
           detail={'tag_ids':[item['id'] for item in data], 'hit_count':len(data),
                   'injection_status':'reference_returned_to_caller', 'interest_weights':'not_integrated'})
    return template.replace("{memory_json}", json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def record_final_request_tags(request, session_id: str, task: str, request_id: str) -> None:
    """Check the final provider input after hooks, without persisting its text."""
    from src.llm_models.model_client.base_client import ResponseRequest
    from src.llm_models.payload_content.context_item import get_item_text
    reference = last_reference_text.get()
    if not reference or session_id != last_reference_session.get() or not isinstance(request, ResponseRequest):
        return
    if any(reference in get_item_text(item) for item in request.context_items):
        record_tag_injection(session_id, task, request_id)
    else:
        record('memory', 'memory.reference_not_in_request', 'not_injected', 'reference_absent_from_final_request',
               session_id=session_id, request_id=request_id, detail={'target':task})
