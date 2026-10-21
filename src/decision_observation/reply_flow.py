"""Explicit per-tool/task capability for generation and outbound observations.

The ContextVar is only transport inside the owning asyncio task. Copied contexts
in children, callbacks and worker threads cannot use it. Nothing is attached to
messages, prompts, Replay databases or plugin arguments.
"""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, ContextManager, Iterator, Optional, Tuple
import asyncio
import json
import time
import uuid

from . import recorder
from .models import Batch, ExecutionEvent, Reference, ReplyEvent, identity


@dataclass
class Flow:
    session_id: str
    references: Tuple[Reference, ...]
    execution_id: str
    logical_turn_id: Optional[str]
    cycle_id: Optional[str]
    tool_call_id: Optional[str]
    source: str
    owner: Any
    flow_id: str
    account_id: Optional[str] = None
    reply_id: Optional[str] = None
    generation_group_id: Optional[str] = None
    generation_call_id: Optional[str] = None
    generation_active: bool = False
    send_call_id: Optional[str] = None
    sequence: int = 0


_current: ContextVar[Optional[Flow]] = ContextVar("observation_reply_flow", default=None)


@recorder.isolated
def current(session_id: str) -> Optional[Flow]:
    if not recorder.store.policy.enabled:
        return None
    flow = _current.get()
    if flow is None or flow.session_id != session_id:
        return None
    try:
        task = asyncio.current_task()
    except RuntimeError:  # No loop in a copied to_thread context; it owns no capability.
        return None
    if flow.owner is not task:
        return None
    return flow


@contextmanager
def _bound(flow: Flow) -> Iterator[Flow]:
    token = _current.set(flow)
    try:
        yield flow
    finally:
        _current.reset(token)


@recorder.isolated
def _scope(session_id: str, references: Tuple[Reference, ...], execution_id: str,
           logical_turn_id: Optional[str], cycle_id: Optional[str], tool_call_id: Optional[str], source: str,
           account_id: Optional[str] = None) -> ContextManager[Optional[Flow]]:
    if not recorder.store.policy.enabled or not execution_id or not references:
        return nullcontext()
    # Receipts are supplied by the actual consuming engine/admitted priority task.
    if any(r.session_id != session_id for r in references):
        raise ValueError("Cross-session reply flow")
    if any(r.opportunity.account_id is not None and r.opportunity.account_id != account_id for r in references):
        raise ValueError("Cross-account reply flow")
    return _bound(Flow(session_id, references, execution_id, logical_turn_id, cycle_id,
                       tool_call_id, source, asyncio.current_task(), uuid.uuid4().hex, account_id))


@recorder.isolated
def _planner_scope(tracker: Any, runtime: Any, invocation: Any) -> ContextManager[Optional[Flow]]:
    if not recorder.store.policy.enabled or invocation.tool_name != "reply":
        return nullcontext()
    return _scope(runtime.session_id, tracker.cycle_references, tracker.execution_id,
                  tracker.logical_turn_id, tracker.cycle_id, invocation.call_id, "planner",
                  runtime.chat_stream.account_id or None) or nullcontext()


def planner_scope(tracker: Any, runtime: Any, invocation: Any) -> ContextManager[Optional[Flow]]:
    return _planner_scope(tracker, runtime, invocation) or nullcontext()


@recorder.isolated
def _priority_scope(service: Any, message: Any) -> ContextManager[Optional[Flow]]:
    if not recorder.store.policy.enabled:
        return nullcontext()
    ref = service._observation_references.get(service.message_key(message))
    if ref is None:
        return nullcontext()
    trace = service._replay_traces.get(service.message_key(message))
    return _scope(message.session_id, (ref,), ref.execution_id,
                  trace.logical_turn_id if trace is not None else None, None, None, "priority_at",
                  ref.opportunity.account_id)


def priority_scope(service: Any, message: Any) -> ContextManager[Optional[Flow]]:
    return _priority_scope(service, message) or nullcontext()


@recorder.isolated
def event(session_id: str, stage: str, status: str, reason: str = "", *, key: Optional[str] = None,
          reply_id: Optional[str] = None, request_id: Optional[str] = None,
          outbound_id: Optional[str] = None, attempt_id: Optional[str] = None, **counts: Any) -> None:
    flow = current(session_id)
    if flow is None:
        return
    from src.decision_replay import recorder as replay
    from src.decision_replay import replies

    active = replies.current_reply.get()
    trace = replay.current_trace.get()
    # Existing Replay bindings are accepted only inside this explicit capability.
    trace_matches = (replay.safe_enabled() and trace is not None and trace.session_id == session_id
                     and (flow.source == "priority_at" and trace.source == "priority_at"
                          and trace.logical_turn_id == flow.logical_turn_id
                          or flow.source == "planner" and trace.logical_turn_id == flow.logical_turn_id
                          and trace.cycle_id == flow.cycle_id))
    if reply_id is not None:
        flow.reply_id = reply_id  # Existing ReplyExtensionExecution ID, never synthesized here.
    elif active is not None and active.session_id == session_id and active.source == flow.source and trace_matches:
        flow.reply_id = active.reply_id
    flow.sequence += 1
    collection = None
    if stage == "driver_send":
        from .delivery import scope_for

        collection = scope_for(flow)
        counts["collection_scope_id"] = collection.scope_id
    occurrence = identity("reply_occurrence", flow.flow_id, key or str(flow.sequence), stage)
    allowed = {"output_count", "valid_output", "request_count", "segment_count", "segment_index",
               "content_type", "driver_id", "returned_non_null", "error_type", "generation_call_id",
               "generation_status", "candidate_count", "retry_count", "reasoning_continuation_count",
               "send_call_id", "driver_reported_status", "driver_kind", "collection_scope_id",
               "platform", "route_scope"}
    detail = {k: v for k, v in counts.items() if k in allowed and (v is None or isinstance(v, (str, int, bool)))}
    detail.update(platform_confirmation="not_observed", source=flow.source,
                  account_id=flow.account_id,
                  reply_identity="observed" if flow.reply_id else "not_observed",
                  generation_identity="observed" if flow.generation_group_id else "not_observed",
                  generation_call_id=flow.generation_call_id,
                  send_call_id=flow.send_call_id,
                  missing_identity_reason=None if flow.reply_id and flow.generation_group_id else
                  "replay_disabled_or_identity_not_available_at_boundary")
    if stage == "direct_emoji":
        detail["generation_identity"] = "not_applicable"
        detail["missing_identity_reason"] = None if flow.reply_id else "reply_identity_not_observed"
    for ref in flow.references:
        if (recorder.store.generation(session_id) != ref.generation
                or ref.opportunity.run_id != recorder.store.run_id):
            continue
        if not flow.execution_id or ref.opportunity.session_id != session_id:
            raise ValueError("Invalid reply parent")
        eid = identity("reply_parent", flow.flow_id, ref.opportunity.opportunity_id,
                       ref.evaluation.evaluation_id if ref.evaluation else "")
        parent = ExecutionEvent(eid, ref.opportunity.opportunity_id,
                                ref.evaluation.evaluation_id if ref.evaluation else None,
                                flow.execution_id, time.time(), "reply_flow_linked", "observed",
                                "explicit_task_capability", flow.logical_turn_id, flow.cycle_id,
                                "trace_bound_persistence_unknown" if trace_matches else "not_observed",
                                json.dumps([m.message_event_id for m in ref.messages]), "[]",
                                ref.link_source, ref.wait_tool_call_id, ref.parent_execution_id)
        record = ReplyEvent(identity("reply_link", occurrence, eid), occurrence, eid,
                            parent.opportunity_id, parent.evaluation_id, flow.execution_id, flow.flow_id,
                            time.time(), stage, status, reason, flow.logical_turn_id, flow.cycle_id,
                            flow.tool_call_id, flow.reply_id, flow.generation_group_id, request_id,
                            outbound_id, attempt_id, json.dumps(detail))
        recorder.store.submit(Batch(session_id, ref.generation, ref.messages, ref.opportunity,
                                    ref.evaluation, parent, reply_event=record, delivery_scope=collection))


@recorder.isolated
def generation_entered(session_id: str) -> None:
    flow = current(session_id)
    if flow is None:
        return
    from src.decision_replay import recorder as replay
    from src.decision_replay import replies

    active = replies.current_reply.get()
    trace = replay.current_trace.get()
    # generation_call_id is an observation invocation ID, NOT a Replay generation ID.
    flow.generation_call_id = uuid.uuid4().hex
    flow.generation_group_id = None
    if (replay.safe_enabled() and trace is not None and trace.session_id == session_id
            and active is not None and active.session_id == session_id
            and active.source == flow.source
            and (flow.source == "priority_at" and trace.source == "priority_at"
                 and trace.logical_turn_id == flow.logical_turn_id
                 or flow.source == "planner" and trace.logical_turn_id == flow.logical_turn_id
                 and trace.cycle_id == flow.cycle_id)):
        flow.generation_group_id = replies.current_generation.get()
    flow.generation_active = True
    event(session_id, "generation", "generation_started")


@recorder.isolated
def generation_finished(session_id: str, result: Any = None, error: Optional[BaseException] = None) -> None:
    flow = current(session_id)
    if flow is None:
        return
    flow.generation_active = False
    if error is not None:
        event(session_id, "generation", "interrupted" if isinstance(error, asyncio.CancelledError) else
              "generation_failed", "function_raised", error_type=type(error).__name__)
        return
    success, output = result
    valid = bool(output.completion.response_text.strip())
    event(session_id, "generation", "generation_completed" if success else "generation_failed",
          "returned_valid_output" if success and valid else "returned_without_valid_output",
          output_count=len(output.text_fragments), valid_output=valid,
          retry_count=output.metrics.extra.get("replyer_retry_count"),
          reasoning_continuation_count=output.metrics.extra.get("replyer_reasoning_continuation_count"))


def observe_generation(function: Callable) -> Callable:
    @wraps(function)
    async def wrapper(*args, **kwargs):
        if not recorder.store.policy.enabled:
            return await function(*args, **kwargs)
        session_id = kwargs.get("stream_id") or ""
        generation_entered(session_id)
        try:
            result = await function(*args, **kwargs)
        except BaseException as error:
            generation_finished(session_id, error=error)
            raise
        generation_finished(session_id, result=result)
        return result
    return wrapper


def observe_tool(function: Callable) -> Callable:
    @wraps(function)
    async def wrapper(tool_ctx, invocation, *args, **kwargs):
        if not recorder.store.policy.enabled:
            return await function(tool_ctx, invocation, *args, **kwargs)
        sid = tool_ctx.runtime.session_id
        event(sid, "reply_tool", "entered", "actual_reply_body")
        try:
            result = await function(tool_ctx, invocation, *args, **kwargs)
        except BaseException as error:
            event(sid, "reply_tool", "interrupted" if isinstance(error, asyncio.CancelledError) else "unknown",
                  "tool_raised", error_type=type(error).__name__)
            raise
        tool_returned(sid, result)
        return result
    return wrapper


@recorder.isolated
def tool_returned(session_id: str, result) -> None:
    event(session_id, "reply_tool", "returned", "tool_success" if result.success else "tool_unsuccessful")


@recorder.isolated
def target_checked(session_id: str, target) -> None:
    if current(session_id) is not None:
        event(session_id, "reply_target", "observed" if target.session_id == session_id else "unknown",
              "target_resolved_in_runtime")


@recorder.isolated
def request_started(request: Any, task: str, session_id: str) -> None:
    flow = current(session_id)
    if flow is None or not flow.generation_active or task != "replyer":
        return
    trace = request.trace_context
    matches = trace is not None and trace.session_id == session_id
    request_id = f"{trace.request_id}:{trace.attempt}" if matches else None
    event(session_id, "generation_request", "observed",
          "existing_model_request_boundary" if matches else "request_trace_identity_not_observed_or_scope_mismatch",
          key=f"request:{flow.generation_call_id}:{request_id}" if request_id is not None else None,
          request_id=request_id, request_count=1)


def observe_send(function: Callable) -> Callable:
    @wraps(function)
    async def wrapper(message, *args, **kwargs):
        if not recorder.store.policy.enabled:
            return await function(message, *args, **kwargs)
        sid = message.session_id
        flow = current(sid)
        if flow is None:
            return await function(message, *args, **kwargs)
        original_outbound_id = message.message_id
        previous = flow.send_call_id
        flow.send_call_id = uuid.uuid4().hex
        # API entry is not a driver attempt: hooks/owner gates can still stop it.
        send_call(sid, message, "send_api_entered", "observed", outbound_id=original_outbound_id)
        try:
            result = await function(message, *args, **kwargs)
        except BaseException as error:
            send_call(sid, message, "send_api_ended", "interrupted" if isinstance(error, asyncio.CancelledError)
                      else "unknown", outbound_id=original_outbound_id, error_type=type(error).__name__)
            raise
        else:
            send_call(sid, message, "send_api_ended", "send_returned", outbound_id=original_outbound_id,
                      returned_non_null=result is not None)
            return result
        finally:
            flow.send_call_id = previous
    return wrapper


@recorder.isolated
def send_call(session_id: str, message: Any, stage: str, status: str, *, outbound_id: str, **detail: Any) -> None:
    if current(session_id) is None:
        return
    from src.decision_replay.replies import classify

    event(session_id, stage, status, outbound_id=outbound_id, content_type=classify(message), **detail)


@recorder.isolated
def driver_event(message: Any, route_key: Any, attempt_id: str, stage: str, status: str,
                 driver_id: str, error: Optional[BaseException] = None, result: Any = None) -> None:
    flow = current(message.session_id)
    if flow is None:
        return
    # Verify the actual dispatch account against explicit priority parent receipts.
    if (route_key.platform != "qq" or (route_key.account_id or None) != flow.account_id) or any(
        ref.opportunity.account_id is not None and ref.opportunity.account_id != (route_key.account_id or None)
        for ref in flow.references
    ):
        return
    from src.decision_replay.replies import classify

    event(message.session_id, stage, status, key=attempt_id,
          outbound_id=message.message_id, attempt_id=attempt_id, content_type=classify(message),
          driver_id=driver_id, error_type=type(error).__name__ if error is not None else None,
          driver_reported_status=result.status.value if result is not None else None,
          driver_kind=result.driver_kind.value if result is not None else None,
          platform=route_key.platform, route_scope=route_key.scope or None)
