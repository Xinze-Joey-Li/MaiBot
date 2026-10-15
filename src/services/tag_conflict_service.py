"""将同分类历史标签与新候选独立送给记忆模型裁决，严格校验引用。"""

from dataclasses import asdict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import asyncio
import hashlib
import json
import re
import time
import uuid

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
    """Invalid pair coverage, format, or aggregate decisions; never mark unchecked items successful."""

    def __init__(self, message: str, *, reset_progress: bool = False) -> None:
        super().__init__(message)
        self.reset_progress = reset_progress


CONFLICT_RULE_VERSION = "pair-batch-v3"
MAX_PAIRS_PER_REQUEST = 40
MAX_PROMPT_DATA_CHARS = 24000


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _compact_old(tag: Dict[str, Any]) -> Dict[str, Any]:
    # Keep semantic content, evidence, identity, time and revision; exclude activity/retention bookkeeping.
    return {key: tag[key] for key in ("id", "revision", "status", "name", "summary", "category_path", "created_at", "evidence") if key in tag}


def plan_conflict_pairs(
    session_id: str, new_tags: List[TagCandidate], old_tags: List[Dict[str, Any]],
    source_times: Optional[Dict[str, float]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    # 本地按分类预筛选，再用名称/摘要词汇交集优先检查相关项。
    # 低相似项仍保留检查，避免改名或同义表达导致必要冲突被遗漏。
    pairs = []
    local = 0
    seen_old: Dict[str, Dict[str, Any]] = {}
    for old in old_tags:
        if old.get("session_id", session_id) != session_id:
            raise ValueError("冲突候选跨会话")
        if old["id"] in seen_old and seen_old[old["id"]] != old:
            raise ValueError("同一旧标签 ID 有不同内容")
        seen_old[old["id"]] = old
    for index, candidate in enumerate(new_tags):
        new = {key: value for key, value in asdict(candidate).items()
               if key in {"name", "summary", "category_path", "source_message_ids", "evidence_quotes", "previous_tag_id"}}
        if source_times is not None:
            new["source_times"] = {key: source_times[key] for key in candidate.source_message_ids}
        terms = _terms(candidate.name + candidate.summary)
        related = [old for old in seen_old.values() if old["category_path"] == list(candidate.category_path)]
        related.sort(key=lambda old: (-len(terms & _terms(old.get("name", "") + old.get("summary", ""))), old["id"]))
        for old in related:
            # Exact content AND identical provenance is a duplicate, not a semantic contradiction.
            # No containment, synonyms, dates or preferences are resolved locally.
            quotes = {item["message_id"]: item["quote"] for item in old.get("evidence", [])}
            def normalize(value: str) -> str:
                return " ".join(value.split())
            if (old.get("status") == "valid" and quotes and
                    set(candidate.source_message_ids) == set(quotes) and candidate.evidence_quotes == quotes and
                    normalize(candidate.name) == normalize(old.get("name", "")) and
                    normalize(candidate.summary) == normalize(old.get("summary", ""))):
                local += 1
                continue
            pairs.append({"new_index": index, "new": new, "old": _compact_old(old)})
    return pairs, local


def batch_payload(pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    old = {pair["old"]["id"]: pair["old"] for pair in pairs}
    new = {pair["new_index"]: pair["new"] for pair in pairs}
    # 同一历史定义只在前部出现一次，新标签与候选组合放在后部，保留稳定规则前缀。
    return {"old_tags": list(old.values()), "new_tags": [{"new_index": index, **tag} for index, tag in new.items()],
            "pairs": [{"candidate_id": pair["candidate_id"], "new_index": pair["new_index"],
                       "old_tag_id": pair["old"]["id"]} for pair in pairs]}


def parse_pair_results(output: str, pairs: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    payload = json.loads(output)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise InvalidConflictOutput("冲突批次必须返回 results，空 decisions 不证明已检查")
    allowed = {pair["candidate_id"] for pair in pairs}
    results = {}
    for item in payload["results"]:
        if not isinstance(item, dict):
            raise InvalidConflictOutput("冲突结果必须为对象")
        key, action, reason = item.get("candidate_id"), item.get("decision"), item.get("reason")
        if not isinstance(key, str) or key not in allowed or key in results:
            raise InvalidConflictOutput("冲突结果候选 ID 未知或重复")
        if action not in {"coexist", "replace_old", "keep_old", "uncertain"}:
            raise InvalidConflictOutput("冲突结果动作无效")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise InvalidConflictOutput("冲突结果缺少有效理由")
        results[key] = {"decision": action, "reason": reason.strip()}
    return results


async def decide_tag_conflicts(
    session_id: str, new_tags: List[TagCandidate], old_tags: List[Dict[str, Any]], *,
    progress: Optional[Dict[str, Any]] = None,
    checkpoint: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    max_requests: int = MAX_CONFLICT_REQUESTS,
    read_cache: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
    write_cache: Optional[Callable[[str, str], Awaitable[None]]] = None,
    on_request: Optional[Callable[[], Awaitable[None]]] = None,
    on_usage: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    on_started: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    on_stats: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    source_times: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    if max_requests < 1:
        raise ValueError("冲突请求预算必须为正数")
    if not new_tags or not old_tags:
        return []
    from src.common.prompt_i18n import load_prompt
    from src.services.llm_service import LLMServiceClient

    template = await asyncio.to_thread(load_prompt, "tag_conflict")
    pairs, local = plan_conflict_pairs(session_id, new_tags, old_tags, source_times)
    # Directional keys: replacing old with new is not a symmetric judgement.
    for index, pair in enumerate(pairs):
        pair["candidate_id"] = f"p{index}"
        pair["cache_key"] = _digest({"session_id": session_id, "rule": CONFLICT_RULE_VERSION,
                                        "template": template, "new": pair["new"], "old": pair["old"]})
    signature = _digest([pair["cache_key"] for pair in pairs])
    state = progress if progress is not None and progress.get("signature") == signature else {
        "signature": signature, "results": {}, "requests": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }
    results = state["results"]
    cached = 0
    checkpoint_hits = len(results)
    for pair in pairs:
        key = pair["candidate_id"]
        if key not in results and read_cache is not None:
            raw = await read_cache(pair["cache_key"])
            if raw is not None:
                cached_result = json.loads(raw)
                if not isinstance(cached_result, dict) or set(cached_result) != {"decision", "reason"}:
                    raise InvalidConflictOutput("冲突缓存格式无效")
                parsed = parse_pair_results(json.dumps({"results": [{"candidate_id": key, **cached_result}]}), [pair])
                if set(parsed) != {key}:
                    raise InvalidConflictOutput("缓存未包含预期标签对")
                results.update(parsed)
                cached += 1
    used = 0
    checked_pairs = 0
    client = None
    try:
        while True:
            pending = [pair for pair in pairs if pair["candidate_id"] not in results]
            if not pending:
                break
            if used >= max_requests:
                raise ConflictBudgetExceeded("冲突请求预算已用完，未完成项保留")
            batch: List[Dict[str, Any]] = []
            for pair in pending:
                proposed = batch + [pair]
                if len(proposed) > MAX_PAIRS_PER_REQUEST or len(json.dumps(batch_payload(proposed), ensure_ascii=False)) > MAX_PROMPT_DATA_CHARS:
                    if not batch:
                        raise InvalidConflictOutput("单个标签对超过冲突输入上限，需要人工检查来源长度")
                    break
                batch = proposed
            saved = state.get("pending_response")
            if saved is None:
                if client is None:
                    client = LLMServiceClient(task_name="memory", request_type="qq_agent.tag_conflict", session_id=session_id)
                if on_request is not None:
                    await on_request()
                request_id = uuid.uuid4().hex
                if on_started is not None:
                    await on_started({"request_id": request_id, "stage": "conflict", "pair_count": len(batch)})
                used += 1
                checked_pairs += len(batch)
                started = time.monotonic()
                prompt = template.replace("{input_json}", json.dumps(batch_payload(batch), ensure_ascii=False, separators=(",", ":")))
                if state.get("validation_error"):
                    prompt += "\nCorrection: " + state["validation_error"] + "; return every listed candidate_id exactly once."
                response = await client.generate_response(prompt)
                usage = {"request_id": request_id, "stage": "conflict", "prompt_tokens": response.prompt_tokens,
                         "completion_tokens": response.completion_tokens, "total_tokens": response.total_tokens,
                         "pair_count": len(batch), "usage_scope": "returned_response",
                         "provider_attempts": [{"id": attempt.attempt_id, "status": attempt.status,
                                                "duration_ms": attempt.duration_ms} for attempt in response.generation_attempts],
                         "elapsed_seconds": time.monotonic() - started}
                state["requests"] += 1
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    state[key] += usage[key]
                # 已收到的原始输出也先写入检查点，数据库提交失败不重复调用模型。
                state["pending_response"] = {"candidate_ids": [pair["candidate_id"] for pair in batch],
                                             "output": response.response, "usage": usage}
                if checkpoint is not None:
                    await checkpoint(state)

            else:
                saved_ids = set(saved["candidate_ids"])
                batch = [pair for pair in pairs if pair["candidate_id"] in saved_ids]
            saved = state["pending_response"]
            if on_usage is not None:
                await on_usage(saved["usage"])
            logger.info(f"冲突单次用量: session_id={session_id} usage={json.dumps(saved['usage'])}")
            try:
                partial = parse_pair_results(saved["output"], batch)
            except (ValueError, TypeError, KeyError) as error:
                state["validation_error"] = str(error)
                state.pop("pending_response", None)
                if checkpoint is not None:
                    await checkpoint(state)
                raise InvalidConflictOutput(str(error)) from error
            for key, result in partial.items():
                if write_cache is not None:
                    cache_key = next(pair["cache_key"] for pair in batch if pair["candidate_id"] == key)
                    await write_cache(cache_key, json.dumps(result, ensure_ascii=False))
                results[key] = result
            missing = [pair["candidate_id"] for pair in batch if pair["candidate_id"] not in partial]
            if missing:
                state["validation_error"] = "Missing candidate IDs: " + ",".join(missing)
            else:
                state.pop("validation_error", None)
            state.pop("pending_response", None)
            if checkpoint is not None:
                await checkpoint(state)
            if missing:
                raise InvalidConflictOutput(state["validation_error"])
        decisions = [{"new_index": pair["new_index"], "old_tag_id": pair["old"]["id"],
                      "old_revision": pair["old"].get("revision", 0), **results[pair["candidate_id"]]}
                     for pair in pairs if results[pair["candidate_id"]]["decision"] != "coexist"]
        # 动作可以因旧标签而异；重复对与多个有效替代者仍必须拒绝。
        try:
            validate_conflict_pairs(decisions, len(new_tags))
        except ValueError as error:
            raise InvalidConflictOutput(str(error), reset_progress=True) from error
        return decisions
    finally:
        if on_stats is not None:
            await on_stats({"candidate_pairs": len(pairs) + local, "local_duplicates": local,
                            "cache_hits": cached, "checkpoint_hits": checkpoint_hits,
                            "model_requests_this_attempt": used, "model_pairs_this_attempt": checked_pairs,
                            "completed_pairs": len(results), "remaining_pairs": len(pairs) - len(results)})
