"""Planner 与 Replyer 共用的按群标签检索和一次性参考构造。"""

from typing import List, Optional

import asyncio
import json

from .tag_memory_service import tag_memory_service


async def build_tag_memory_reference(session_id: str, text: str = "", user_ids: Optional[List[str]] = None) -> str:
    from src.common.message_repository import find_messages
    from src.common.prompt_i18n import load_prompt
    from src.config.config import global_config

    if not global_config.chat.tag_memory.enabled:
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
        return ""
    # 不把整棵树和相邻批次全文塞入提示词，最多召回八个有证据的标签。
    data = [{"id": item["id"], "category_path": item["category_path"], "name": item["name"],
             "summary": item["summary"], "is_active": bool(item["is_active"] or item["auto_active"]),
             "created_at": item["created_at"], "evidence": [
                 {"message_id": source["message_id"], "quote": source["quote"][:240]}
                 for source in item["evidence"][:3]]} for item in hits]
    template = await asyncio.to_thread(load_prompt, "tag_memory_reference")
    return template.replace("{memory_json}", json.dumps(data, ensure_ascii=False))
