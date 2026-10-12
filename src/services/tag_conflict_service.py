"""将同分类历史标签与新候选独立送给记忆模型裁决，严格校验引用。"""

from dataclasses import asdict
from typing import Any, Dict, List

import asyncio
import json

from .tag_memory_store import TagCandidate


def parse_conflict_decisions(output: str, new_index: int, old_tags: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    payload = json.loads(output)
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        raise ValueError("冲突裁决必须返回 decisions 列表")
    allowed = {item["id"] for item in old_tags}
    seen = set()
    result = []
    for item in payload["decisions"]:
        if not isinstance(item, dict):
            raise ValueError("冲突裁决项必须是对象")
        old_id, action, reason = item.get("old_tag_id"), item.get("decision"), item.get("reason")
        if not isinstance(old_id, str) or old_id not in allowed or old_id in seen:
            raise ValueError("冲突裁决引用未知或重复的旧标签")
        if action not in {"replace_old", "keep_old", "uncertain"}:
            raise ValueError("冲突裁决动作无效")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("冲突裁决缺少有效理由")
        seen.add(old_id)
        old = next(tag for tag in old_tags if tag["id"] == old_id)
        result.append({"new_index": new_index, "old_tag_id": old_id, "old_revision": old.get("revision", 0),
                       "decision": action, "reason": reason.strip()})
    return result


async def decide_tag_conflicts(
    session_id: str, new_tags: List[TagCandidate], old_tags: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not new_tags or not old_tags:
        return []
    from src.common.prompt_i18n import load_prompt
    from src.services.llm_service import LLMServiceClient

    decisions = []
    template = None
    client = None
    for index, candidate in enumerate(new_tags):
        related = [item for item in old_tags if item["category_path"] == list(candidate.category_path)]
        # 全部同分类历史分块检查，不能因标签改名或措辞变化而跳过潜在冲突。
        for offset in range(0, len(related), 20):
            chunk = related[offset:offset + 20]
            if template is None:
                template = await asyncio.to_thread(load_prompt, "tag_conflict")
                client = LLMServiceClient(task_name="memory", request_type="qq_agent.tag_conflict", session_id=session_id)
            data = {"new_tag": asdict(candidate), "old_tags": chunk}
            response = await client.generate_response(template.replace("{input_json}", json.dumps(data, ensure_ascii=False)))
            decisions.extend(parse_conflict_decisions(response.response, index, chunk))
    # 单个新事实不能同时被保留和作废，多个新事实也不能同时改写同一旧事实。
    actions = {}
    old_ids = set()
    for item in decisions:
        if item["old_tag_id"] in old_ids:
            raise ValueError("同一旧标签收到多个新标签的冲突裁决，需要重新判断")
        old_ids.add(item["old_tag_id"])
        previous = actions.setdefault(item["new_index"], item["decision"])
        if previous != item["decision"]:
            raise ValueError("同一新标签的冲突裁决互相矛盾，需要重新判断")
    return decisions
