"""将同分类历史标签与新候选独立送给记忆模型裁决，严格校验引用。"""

from dataclasses import asdict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import asyncio
import hashlib
import json
import re

from src.common.logger import get_logger

from .tag_memory_store import TagCandidate, validate_conflict_pairs

logger = get_logger("tag_conflict")
MAX_CONFLICT_REQUESTS = 4


class ConflictBudgetExceeded(Exception):
    """本轮预算已用完，保留进度等待下一轮，不算模型失败。"""



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


def _terms(text: str) -> Set[str]:
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    return words | {chinese[index:index + 2] for index in range(len(chinese) - 1)}


class InvalidConflictOutput(ValueError):
    """完成检查后的标签对重复或存在多方矛盾，需要清除冲突检查点。"""


async def decide_tag_conflicts(
    session_id: str, new_tags: List[TagCandidate], old_tags: List[Dict[str, Any]], *,
    progress: Optional[Dict[str, Any]] = None,
    checkpoint: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    max_requests: int = MAX_CONFLICT_REQUESTS,
    read_cache: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
    write_cache: Optional[Callable[[str, str], Awaitable[None]]] = None,
    on_request: Optional[Callable[[], Awaitable[None]]] = None,
) -> List[Dict[str, Any]]:
    if max_requests < 1:
        raise ValueError("冲突请求预算必须为正数")
    if not new_tags or not old_tags:
        return []
    from src.common.prompt_i18n import load_prompt
    from src.services.llm_service import LLMServiceClient

    # 本地按分类预筛选，再用名称/摘要词汇交集优先检查相关项。
    # 低相似项仍留在后续分块中，避免改名或同义表达导致必要检查被遗漏。
    chunks = []
    for index, candidate in enumerate(new_tags):
        related = [item for item in old_tags if item["category_path"] == list(candidate.category_path)]
        terms = _terms(candidate.name + candidate.summary)
        related.sort(key=lambda item: (-len(terms & _terms(item.get("name", "") + item.get("summary", ""))), item["id"]))
        chunks.extend((index, related[offset:offset + 20]) for offset in range(0, len(related), 20))
    signature = hashlib.sha256(json.dumps(
        {"new": [asdict(tag) for tag in new_tags], "chunks": chunks}, sort_keys=True, ensure_ascii=False
    ).encode()).hexdigest()
    state = progress if progress is not None and progress.get("signature") == signature else {
        "signature": signature, "cursor": 0, "decisions": [], "requests": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }
    decisions = list(state["decisions"])
    template = None
    client = None
    used = 0
    while state["cursor"] < len(chunks):
        index, chunk = chunks[state["cursor"]]
        if template is None:
            template = await asyncio.to_thread(load_prompt, "tag_conflict")
            client = LLMServiceClient(task_name="memory", request_type="qq_agent.tag_conflict", session_id=session_id)
        data = {"new_tag": asdict(new_tags[index]), "old_tags": chunk}
        # 已收到的原始输出也先写入检查点，数据库提交失败不重复调用模型。
        input_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
        cache_key = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
        raw = state.get("raw_output")
        if raw is None and read_cache is not None:
            raw = await read_cache(cache_key)
        if raw is None:
            if used >= max_requests:
                logger.info(f"冲突检查延迟: session_id={session_id} 本轮请求={used} 进度={state['cursor']}/{len(chunks)}")
                raise ConflictBudgetExceeded("冲突检查预算已用完")
            if on_request is not None:
                await on_request()
            # 同一历史分块置于前缀，新标签放在末尾，便于跨新标签复用供应商前缀缓存。
            # 缓存键仍沿用原格式，已有持久化检查点无需重新请求模型。
            prompt_data = {"old_tags": chunk, "new_tag": data["new_tag"]}
            response = await client.generate_response(template.replace(
                "{input_json}", json.dumps(prompt_data, ensure_ascii=False, separators=(",", ":"))
            ))
            used += 1
            state["requests"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                state[key] += {
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "total_tokens": response.total_tokens,
                }[key]
            logger.info(
                f"冲突检查用量: session_id={session_id} requests={state['requests']} "
                f"prompt_tokens={state['prompt_tokens']} completion_tokens={state['completion_tokens']} "
                f"total_tokens={state['total_tokens']} progress={state['cursor']}/{len(chunks)}"
            )
            state["raw_output"] = response.response
            if checkpoint is not None:
                await checkpoint(state)
            raw = response.response
        try:
            decisions.extend(parse_conflict_decisions(raw, index, chunk))
        except (ValueError, TypeError):
            state.pop("raw_output", None)
            if checkpoint is not None:
                await checkpoint(state)
            raise
        if write_cache is not None:
            await write_cache(cache_key, raw)
        state["cursor"] += 1
        state["decisions"] = decisions
        state.pop("raw_output", None)
        if checkpoint is not None:
            await checkpoint(state)
    # 动作可以因旧标签而异；重复对与多个有效替代者仍必须拒绝。
    try:
        validate_conflict_pairs(decisions, len(new_tags))
    except ValueError as error:
        raise InvalidConflictOutput(str(error)) from error
    return decisions
