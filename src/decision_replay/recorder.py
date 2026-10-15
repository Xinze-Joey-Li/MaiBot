"""Context-local correlation and privacy-limited recording. Never raises into chat."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import atexit
import logging
import time
import uuid

from .store import ReplayStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Trace:
    session_id: str
    logical_turn_id: str
    source: str = "message"
    cycle_id: Optional[str] = None


current_trace: ContextVar[Optional[Trace]] = ContextVar("decision_replay_trace", default=None)
store = ReplayStore(Path(__file__).resolve().parents[2] / "data/decision_replay.db")
atexit.register(store.flush)


def enabled() -> bool:
    from src.config.config import global_config

    return global_config.debug.decision_replay_enabled


def new_trace(session_id: str, source: str = "message", turn_id: Optional[str] = None) -> Trace:
    return Trace(session_id, turn_id or uuid.uuid4().hex, source)


@contextmanager
def bind(trace: Trace) -> Iterator[Trace]:
    token = current_trace.set(trace)
    try:
        yield trace
    finally:
        current_trace.reset(token)


def cycle(cycle_id: int) -> None:
    trace = current_trace.get()
    if trace:
        current_trace.set(replace(trace, cycle_id=str(cycle_id)))


def record(
    stage: str,
    event_type: str,
    decision: str = "",
    reason_code: str = "",
    *,
    trace: Optional[Trace] = None,
    session_id: str = "",
    message_ids: Optional[List[str]] = None,
    detail: Optional[Dict[str, Any]] = None,
    duration_ms: Optional[float] = None,
    request_id: Optional[str] = None,
    event_id: Optional[str] = None,
    persist: bool = False,
) -> None:
    try:
        if not enabled():
            return
        active = trace or current_trace.get()
        if active is None or (session_id and active.session_id != session_id):
            # Never borrow another group's trace. Uncorrelated work remains explicitly independent.
            if not session_id:
                return
            active = new_trace(session_id, "independent")
        store.retention_days = retention_days()
        from .replies import correlation
        payload = {**correlation(), **(detail or {}), "trigger_source": active.source}
        # Call sites supply allowlisted fields only; reject obvious secrets and raw text recursively.
        payload = sanitize(payload)
        writer = store.write_batch if persist else lambda events: store.submit(events[0])
        writer([
            {
                "event_id": event_id or uuid.uuid4().hex,
                "timestamp": time.time(),
                "session_id": active.session_id,
                "logical_turn_id": active.logical_turn_id,
                "source": active.source,
                "cycle_id": active.cycle_id,
                "request_id": request_id,
                "message_ids": message_ids or [],
                "stage": stage,
                "event_type": event_type,
                "decision": decision,
                "reason_code": reason_code,
                "detail": payload,
                "duration_ms": duration_ms,
                "schema_version": 2 if event_type.startswith("reply_stats.") else 1,
            }
        ])
    except Exception:
        store.errors += 1
        store.dropped += 1
        logger.warning("Decision Replay recording failed; chat unaffected", exc_info=True)


def retention_days() -> int:
    from src.config.config import global_config

    return global_config.debug.decision_replay_retention_days


def sanitize(value: Any) -> Any:
    forbidden = {
        "prompt",
        "content",
        "text",
        "quote",
        "api_key",
        "authorization",
        "secret",
        "password",
        "access_token",
        "reasoning",
        "arguments",
        "raw_output",
    }
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items() if str(k).lower() not in forbidden}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value[:200]]
    if isinstance(value, str):
        return value[:1024]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def monitor_event(event: str, data: Dict[str, Any]) -> None:
    """Reuse actual monitor notifications without copying their prompts or message bodies."""
    if not enabled():
        return
    session_id = str(data.get("session_id", ""))
    if event == "planner.finalized":
        state = data.get("final_state") or {}
        tools = data.get("tools") or []
        names = [item.get("tool_name", item.get("name")) for item in tools]
        outcome = "wait" if "wait" in names else ("reply" if "reply" in names else "no_reply")
        if any(item.get("success") is False for item in tools):
            outcome = "failed"
        record(
            "planner",
            event,
            outcome,
            str(state.get("end_reason", "unknown")),
            session_id=session_id,
            detail={
                "cycle_id": data.get("cycle_id"),
                "tool_names": names,
                "interrupted": data.get("interrupted"),
                "agent_state": state.get("agent_state"),
            },
        )
    elif event in ("llm.retry", "llm.error"):
        record("model", event, "failed" if event.endswith("error") else "retry", event, session_id=session_id)


def probe(function):
    """Isolate observability adapters from production behavior, including malformed payloads."""

    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            logger.warning("Decision Replay probe failed; action unaffected", exc_info=False)
            return None

    return wrapper


@probe
def model_attempt(
    request, response, *, task: str, session_id: str, failed: bool = False, started: bool = False
) -> None:
    if not enabled():
        return
    trace_context = request.trace_context
    if trace_context is None:
        return
    target_session = trace_context.session_id or session_id
    active = current_trace.get()
    independent = active is None or (target_session and active.session_id != target_session)
    model_trace = (
        new_trace(target_session, "independent_model", "llm-" + trace_context.request_id)
        if independent and target_session
        else active
    )
    request_id = f"{trace_context.request_id}:{trace_context.attempt}"
    if started:
        try:
            from src.services.tag_memory_recall import record_final_request_tags

            if model_trace is not None:
                with bind(model_trace):
                    record_final_request_tags(request, target_session or model_trace.session_id, task, request_id)
        except Exception:
            logger.warning("Decision Replay final tag reference probe failed", exc_info=False)
    usage = response.usage if response is not None else None
    cost = None
    if usage:
        try:
            from datetime import datetime
            from src.llm_models.utils import llm_usage_recorder

            prices = llm_usage_recorder._resolve_prices(
                request.model_info, datetime.fromtimestamp(trace_context.current_attempt_started_at)
            )
            cost = round(
                llm_usage_recorder._calculate_input_cost(request.model_info, usage, prices)
                + usage.completion_tokens / 1000000 * prices.price_out,
                6,
            )
        except Exception:
            cost = None
    record(
        "model",
        "model.request",
        "started" if started else ("failed" if failed else "succeeded"),
        "request_started" if started else ("provider_error" if failed else "provider_response"),
        session_id=target_session,
        trace=model_trace,
        request_id=request_id,
        duration_ms=max(0, time.time() - trace_context.current_attempt_started_at) * 1000
        if trace_context.current_attempt_started_at
        else None,
        detail={
            "model": request.model_info.name,
            "task": task,
            "retry_attempt": trace_context.attempt,
            "input_tokens": usage.prompt_tokens if usage else None,
            "output_tokens": usage.completion_tokens if usage else None,
            "cache_hit_tokens": usage.prompt_cache_hit_tokens if usage and usage.prompt_cache_reported else None,
            "cache_miss_tokens": usage.prompt_cache_miss_tokens if usage and usage.prompt_cache_reported else None,
            "usage_source": "provider" if usage else "unknown",
            "cost": cost,
            "cost_source": "local_price_estimate" if cost is not None else "unknown",
            "request_group": trace_context.request_id,
        },
    )
    if independent and target_session and not started:
        record(
            "model",
            "turn.ended",
            "failed" if failed else "completed",
            "independent_model_attempt_finished",
            trace=model_trace,
        )


def traced_priority(function):
    @wraps(function)
    async def wrapper(self, message, *args, **kwargs):
        if not safe_enabled():
            return await function(self, message, *args, **kwargs)
        trace = self._replay_traces.get((message.session_id, message.message_id)) or new_trace(
            message.session_id, "priority_at"
        )
        with bind(trace):
            record("scheduler", "turn.started", "started", "priority_at", message_ids=[message.message_id])
            try:
                result = await function(self, message, *args, **kwargs)
            except BaseException as error:
                record("scheduler", "turn.ended", "failed", type(error).__name__)
                raise
            else:
                record("scheduler", "turn.ended", "completed", "priority_at_completed")
                return result

    return wrapper


def traced_send(function):
    @wraps(function)
    async def wrapper(message, *args, **kwargs):
        if not safe_enabled():
            return await function(message, *args, **kwargs)
        from . import replies
        reply = replies.message_reply(message, kwargs.get("maisaka_source_kind", "independent_send"))
        async with replies.action(reply.session_id, reply.source, reply.session_type,
                                  reply_id=reply.reply_id, system=reply.system):
            await replies.plan(message, optional=kwargs.get("maisaka_source_kind") == "reply_emoji_attachment")
            result = await function(message, *args, **kwargs)
            flags = message.message_info.additional_config
            if result is None and "statistics_send_result" not in flags:
                await replies.skip(message, "send_not_dispatched")
            # Compatibility timeline; counters use only reply_stats events, never this observation.
            status = flags.get("statistics_send_result", "unknown")
            record("send", "send.completed", status, status, session_id=message.session_id,
                   detail={"has_text": replies.classify(message) in {"text", "mixed"},
                           "has_emoji": replies.classify(message) in {"emoji", "mixed"}})
            return result
    return wrapper


def traced_replyer(function):
    @wraps(function)
    async def wrapper(*args, **kwargs):
        if not safe_enabled():
            return await function(*args, **kwargs)
        from . import replies
        session_id = kwargs.get("stream_id") or ""
        generation_id = uuid.uuid4().hex
        async with replies.generation_scope(session_id, generation_id, kwargs.get("reply_message")):
            record("replyer", "replyer.started", "started", "generation_requested", session_id=session_id,
                   message_ids=[kwargs["reply_message"].message_id] if kwargs.get("reply_message") else [])
            token = replies.current_generation.set(generation_id)
            started = time.monotonic()
            try:
                success, result = await function(*args, **kwargs)
            except BaseException as error:
                record("replyer", "replyer.completed", "failed", "generation_exception", session_id=session_id,
                       detail={"error_type": type(error).__name__})
                raise
            finally:
                replies.current_generation.reset(token)
            if success and any(fragment.strip() for fragment in result.text_fragments):
                await replies.emit("generation_success", generation_id, detail={"generation_group_id": generation_id})
            record("replyer", "replyer.completed", "succeeded" if success else "failed",
                   "generated" if success else "generation_failed", session_id=session_id,
                   duration_ms=(time.monotonic() - started) * 1000,
                   detail={"segment_count": len(result.text_fragments),
                           "has_valid_text": any(fragment.strip() for fragment in result.text_fragments),
                           "selected_emoji_tag": result.selected_emoji_tag, "model": result.completion.model_name,
                           "generation_group_id": generation_id})
            return success, result
    return wrapper


def traced_extraction(function):
    @wraps(function)
    async def wrapper(self, job, *args, **kwargs):
        if not safe_enabled():
            return await function(self, job, *args, **kwargs)
        trace = new_trace(job["session_id"], "tag_extraction")
        with bind(trace):
            record("memory", "turn.started", "started", "extraction_job", detail={"job_id": job["id"]})
            try:
                return await function(self, job, *args, **kwargs)
            finally:
                record("memory", "turn.ended", "completed", "extraction_attempt_finished", detail={"job_id": job["id"]})

    return wrapper


observe_monitor_event = probe(monitor_event)


@probe
def priority_queued(service, message, *, overloaded: bool = False) -> None:
    if not enabled():
        return
    trace = new_trace(message.session_id, "priority_at")
    if not overloaded:
        service._replay_traces[(message.session_id, message.message_id)] = trace
    record(
        "scheduler",
        "turn.started",
        "rejected" if overloaded else "queued",
        "priority_overload" if overloaded else "priority_queued",
        trace=trace,
        message_ids=[message.message_id],
        detail={"running": len(service._tasks), "queued": len(service._pending)},
    )
    if overloaded:
        record("scheduler", "turn.ended", "rejected", "priority_overload", trace=trace)


@probe
def priority_cancel_queued(service) -> None:
    for key in service._pending_keys:
        trace = service._replay_traces.get(key)
        if trace:
            record("scheduler", "turn.ended", "cancelled", "shutdown_while_queued", trace=trace)


@probe
def safe_enabled() -> bool:
    return enabled()


store.retention_resolver = retention_days
