"""Small, failure-isolated adapters; production values are read, never recomputed."""

from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, TYPE_CHECKING
import asyncio
import json
import time
import uuid

from .models import (
    Batch, ClaimCheck, ConsumedMessages, Evaluation, ExecutionEvent, FEATURE_VERSION, MessageEvent,
    MessageTrigger, Opportunity, OpportunityTransfer, Policy, Reference, identity,
)
from .store import ObservationStore

if TYPE_CHECKING:
    from src.config.config import ConfigManager

store = ObservationStore(Path(__file__).resolve().parents[2] / "data" / "decision_observation.db")
_registered_config_manager: Optional["ConfigManager"] = None
FEATURE_FIELDS = frozenset(
    (
        "mention_bot",
        "at_other",
        "has_question_mark",
        "placeholder_only",
        "recent_self_ratio",
        "recent_message_count",
        "seconds_since_bot_message",
        "pending_count",
        "owner_verified",
    )
)


def isolated(function: Callable) -> Callable:
    """Observation defects must be visible in health, never raised into chat."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except Exception:
            store.errors += 1
            store.dropped += 1
            store._warning("adapter_failed")
            return None

    return wrapped


def _scope(stream: Any) -> Optional[Tuple[int, int]]:
    if not store.policy.enabled:
        return None
    # Only real, registered QQ group sessions supplied by the existing runtime.
    if stream.platform != "qq" or not stream.is_group_session:
        return None
    return store.generation(stream.session_id)


def _message(message: Any, received_at: Optional[float] = None) -> MessageEvent:
    return MessageEvent(
        identity("message", message.session_id, message.message_id),
        message.session_id,
        message.message_id,
        message.message_info.user_info.user_id,
        message.timestamp.timestamp(),
        received_at,
        store.run_id,
        "inbound" if received_at is not None else "pending_observed",
    )


@isolated
def incoming(stream: Any, message: Any, received_at: Optional[float] = None) -> None:
    generation = _scope(stream)
    if generation is None or message.session_id != stream.session_id:
        return
    store.submit(
        Batch(stream.session_id, generation, (_message(message, time.time() if received_at is None else received_at),))
    )


@isolated
def incoming_runtime(runtime: Any, message: Any, received_at: Optional[float] = None) -> None:
    if store.policy.enabled:
        incoming(runtime.chat_stream, message, received_at)


@isolated
def registered(runtime: Any) -> None:
    if not store.policy.enabled:
        return
    from src.services.owner_admin.trigger_policy import pending_trigger_messages

    pending(runtime, pending_trigger_messages(runtime))


@isolated
def rejected(runtime: Any, reason: str) -> None:
    if not store.policy.enabled:
        return
    from src.services.owner_admin.trigger_policy import pending_trigger_messages

    evaluate(runtime, pending_trigger_messages(runtime), "reject", reason)


@isolated
def pending(runtime: Any, messages: Sequence[Any]) -> bool:
    generation = _scope(runtime.chat_stream)
    if generation is None:
        return False
    state = runtime._observation_workset
    if state.generation != generation:
        state.clear()
    if not messages:
        return False
    if len(messages) > 512:
        store.dropped += 1
        store._warning("workset_too_large")
        return False
    if state.opportunity is None:
        state.generation = generation
        state.opportunity = Opportunity(
            uuid.uuid4().hex, runtime.session_id, time.time(), store.run_id, store.policy.version
        )
    for message in messages:
        if message.session_id != runtime.session_id:
            raise ValueError("Cross-session pending message")
        event = _message(message)
        state.messages[event.message_event_id] = event
    store.submit(Batch(runtime.session_id, generation, tuple(state.messages.values()), state.opportunity))
    return True


@isolated
def evaluate(
    runtime: Any,
    messages: Sequence[Any],
    result: str,
    reason: str,
    detail: Optional[Dict[str, Any]] = None,
    evaluated_at: Optional[float] = None,
) -> Optional[Reference]:
    if _scope(runtime.chat_stream) is None:
        return
    if len(messages) > 512:
        store.dropped += 1
        store._warning("workset_too_large")
        return
    if not pending(runtime, messages):
        return
    state = runtime._observation_workset
    if state.opportunity is None:
        return
    # Only an explicit Runtime fallback receipt brings this ordinary forced path into scope.
    if reason == "forced_trigger_out_of_scope" and any(
        identity("message", runtime.session_id, message.message_id) in state.priority_fallbacks for message in messages
    ):
        result, reason = "pass", "priority_fallback_forced_trigger"
    data = detail or {}
    features = data.get("features")
    if features is not None and (
        set(features) != FEATURE_FIELDS or any(type(value) not in (bool, int, float) for value in features.values())
    ):
        raise ValueError("Feature schema changed; a new observation feature version is required")
    # Deliberate field projection: never serialize arbitrary replay details or message bodies.
    feature_snapshot = {
        "status": "available" if features is not None else "not_calculated",
        "values": features,
        "message_event_ids": [identity("message", runtime.session_id, m.message_id) for m in messages],
    }
    evidence = {
        key: data[key]
        for key in (
            "frequency",
            "delay_seconds",
            "threshold_source",
            "budget",
            "observation",
            "observation_delay_seconds",
            "contributions",
            "logit",
            "owner_overrides",
            "message_count_threshold",
        )
        if key in data
    }
    evidence["probability_status"] = "available" if data.get("probability") is not None else "not_calculated"
    evidence["threshold_status"] = "available" if data.get("threshold") is not None else "not_calculated"
    evidence["weights_status"] = "available" if data.get("weights_hash") is not None else "not_calculated"
    state.evaluation_sequence += 1
    oid = state.opportunity.opportunity_id
    evaluation = Evaluation(
        identity("evaluation", oid, str(state.evaluation_sequence)),
        oid,
        state.evaluation_sequence,
        time.time() if evaluated_at is None else evaluated_at,
        FEATURE_VERSION if features is not None else "not_calculated",
        json.dumps(feature_snapshot, allow_nan=False, separators=(",", ":")),
        data.get("threshold"),
        data.get("probability"),
        result,
        reason,
        data.get("weights_hash"),
        json.dumps(evidence, allow_nan=False, separators=(",", ":")),
    )
    store.submit(
        Batch(runtime.session_id, state.generation, tuple(state.messages.values()), state.opportunity, evaluation)
    )
    state.latest_evaluation = evaluation
    return Reference(runtime.session_id, state.generation, tuple(state.messages.values()), state.opportunity,
                     evaluation, reason != "forced_trigger_out_of_scope")


@isolated
def execution(
    references: Tuple[Reference, ...], stage: str, status: str, reason: str, *, session_id: str,
    execution_id: str = "", logical_turn_id: Optional[str] = None, cycle_id: Optional[str] = None,
    message_ids: Sequence[str] = (), tool_names: Sequence[str] = (), replay_status: str = "not_observed",
) -> None:
    """Small facts only. The caller supplies identities; Replay is never searched or modified."""
    if not store.policy.enabled:
        return
    for ref in references:
        if ref.session_id != session_id or ref.opportunity.session_id != session_id:
            raise ValueError("Cross-session execution reference")
        if store.generation(session_id) != ref.generation or ref.opportunity.run_id != store.run_id:
            continue
        event = ExecutionEvent(
            uuid.uuid4().hex, ref.opportunity.opportunity_id,
            ref.evaluation.evaluation_id if ref.evaluation else None, execution_id or ref.execution_id, time.time(),
            stage, status, reason, logical_turn_id, str(cycle_id) if cycle_id is not None else None,
            replay_status, json.dumps([identity("message", session_id, mid) for mid in message_ids]),
            json.dumps(list(tool_names)),
            ref.link_source, ref.wait_tool_call_id, ref.parent_execution_id,
        )
        store.submit(Batch(session_id, ref.generation, ref.messages, ref.opportunity, ref.evaluation, event))


@isolated
def consume(runtime: Any, messages: list) -> Optional[ConsumedMessages]:
    if not store.policy.enabled or not pending(runtime, messages):
        return None
    state = runtime._observation_workset
    evaluation = state.latest_evaluation
    consumed_opportunity = replace(state.opportunity, status="consumed", finalized_at=time.time())
    ref = Reference(runtime.session_id, state.generation, tuple(state.messages.values()), consumed_opportunity,
                    None, evaluation is None or evaluation.decision_reason != "forced_trigger_out_of_scope",
                    "consumed_without_dispatch")
    execution((ref,), "consumed", "consumed", "pending_batch", session_id=runtime.session_id,
              message_ids=[m.message_id for m in messages])
    return ConsumedMessages(messages, (ref,))


@isolated
def trigger(reference: Reference) -> Optional[MessageTrigger]:
    return MessageTrigger((reference,))


@isolated
def discarded(runtime: Any, payload: str) -> None:
    if isinstance(payload, MessageTrigger):
        execution(payload.references, "not_started", "not_observed", "runtime_stopped_before_dequeue",
                  session_id=runtime.session_id)


def _priority_scope(runtime: Any, message: Any) -> Optional[Tuple[Tuple[int, int], Optional[str]]]:
    if not store.policy.enabled or runtime is None:
        return None
    generation = _scope(runtime.chat_stream)
    if generation is None:
        return None
    from src.platform_io.route_key_factory import RouteKeyFactory

    account, _ = RouteKeyFactory.extract_components(message.message_info.additional_config)
    if (message.session_id != runtime.session_id or runtime.chat_stream.session_id != runtime.session_id
            or account != (runtime.chat_stream.account_id or None)):
        raise ValueError("Priority observation session/account mismatch")
    return generation, account


@isolated
def priority_claim_check(runtime: Any, message: Any, reason: str, *, detected: bool = False) -> None:
    """A classifier check is not a candidate opportunity or a second user demand."""
    scope = _priority_scope(runtime, message)
    if scope is None:
        return
    generation, account = scope
    event = _message(message)
    check = ClaimCheck(uuid.uuid4().hex, event.message_event_id, runtime.session_id, account,
                       store.run_id, time.time(), "not_candidate" if detected else "rejected_before_detection", reason)
    store.submit(Batch(runtime.session_id, generation, (event,), claim_check=check))


@isolated
def priority_candidate(runtime: Any, message: Any, *, detected: bool = True) -> Optional[Reference]:
    """Called after actual claim success. Direct admission without claim remains explicitly unknown."""
    scope = _priority_scope(runtime, message)
    if scope is None:
        return None
    generation, account = scope
    opportunity = Opportunity(
        identity("priority_opportunity", store.run_id, str(generation), runtime.session_id,
                 account or "", message.message_id),
        runtime.session_id, time.time(), store.run_id, store.policy.version,
        source_type="priority_at", status="observed", account_id=account,
    )
    ref = Reference(runtime.session_id, generation, (_message(message),), opportunity,
                    eligible=False, link_source="priority_at", execution_id=uuid.uuid4().hex)
    priority_event(None, message, ref, "priority_candidate" if detected else "priority_candidate_not_observed",
                   "observed" if detected else "not_observed", "claim_passed" if detected else "direct_admission")
    return ref


@isolated
def priority_attempt(service: Any, runtime: Any, message: Any, ref: Optional[Reference]) -> Optional[Reference]:
    scope = _priority_scope(runtime, message)
    if scope is None:
        return None
    if ref is None:
        ref = priority_candidate(runtime, message, detected=False)
    if ref is None:
        return None
    generation, account = scope
    if (ref.session_id != runtime.session_id or ref.generation != generation
            or ref.opportunity.account_id != account or ref.opportunity.source_type != "priority_at"
            or ref.opportunity.run_id != store.run_id
            or tuple(m.message_event_id for m in ref.messages) != (identity("message", runtime.session_id, message.message_id),)):
        raise ValueError("Invalid priority admission receipt")
    priority_event(service, message, ref, "priority_admission_entered", "observed", "start_reply")
    return ref


@isolated
def priority_event(service: Any, message: Any, ref: Optional[Reference], stage: str, status: str, reason: str) -> None:
    if not store.policy.enabled or ref is None:
        return
    logical_turn_id, replay_status = None, "not_observed"
    if service is not None:
        from src.decision_replay import recorder as replay

        if not replay.enabled():
            replay_status = "disabled"
        else:
            expected = service._replay_traces.get(service.message_key(message))
            trace = replay.current_trace.get()
            if (expected is not None and trace is not None and trace.source == "priority_at"
                    and trace.session_id == ref.session_id == expected.session_id
                    and trace.logical_turn_id == expected.logical_turn_id):
                logical_turn_id, replay_status = trace.logical_turn_id, "trace_bound_persistence_unknown"
    execution((ref,), stage, status, reason, session_id=message.session_id,
              message_ids=[message.message_id], logical_turn_id=logical_turn_id, replay_status=replay_status)


@isolated
def priority_admitted(service: Any, message: Any, ref: Optional[Reference], can_start: bool) -> None:
    if ref is None or not store.policy.enabled:
        return
    service._observation_references[service.message_key(message)] = ref
    reason = "available_slot" if can_start else "waiting_for_concurrency_slot"
    priority_event(service, message, ref, "priority_admitted", "admitted", reason)
    priority_event(service, message, ref, "priority_enqueued", "enqueued", reason)


@isolated
def priority_stage(service: Any, message: Any, stage: str, status: str, reason: str) -> None:
    if not store.policy.enabled:
        return
    key = service.message_key(message)
    if stage == "priority_task_started":
        service._observation_started.add(key)
    elif stage == "priority_reply_entry":
        service._observation_entered.add(key)
    priority_event(service, message, service._observation_references.get(key), stage, status, reason)


@isolated
def priority_owned(service: Any, runtime: Any, message: Any, ref: Optional[Reference], *, duplicate: bool = False) -> None:
    if not store.policy.enabled:
        return
    canonical = service._observation_references.get(service.message_key(message))
    if ref is None:
        ref = canonical or priority_candidate(runtime, message, detected=False)
    if ref is None:
        return
    scope = _priority_scope(runtime, message)
    if scope != (ref.generation, ref.opportunity.account_id) or ref.session_id != runtime.session_id:
        raise ValueError("Priority ownership receipt scope mismatch")
    matched = (canonical is not None and canonical.generation == ref.generation
               and canonical.opportunity.opportunity_id == ref.opportunity.opportunity_id
               and canonical.opportunity.run_id == ref.opportunity.run_id == store.run_id
               and canonical.opportunity.account_id == ref.opportunity.account_id)
    if matched:
        ref = replace(ref, parent_execution_id=canonical.execution_id) if duplicate else replace(ref, execution_id=canonical.execution_id)
    priority_event(service, message, ref, "priority_owned_existing" if duplicate else "priority_taken",
                   "owned", ("existing_priority_owner" if matched else "existing_priority_owner_receipt_not_observed")
                   if duplicate else "runtime_handled_committed")


@isolated
def priority_finished(service: Any, key: Tuple[str, ...], *, cancelled: bool = False, error: Optional[str] = None,
                      reason: Optional[str] = None, queued: bool = False) -> None:
    ref = service._observation_references.get(key)
    if ref is not None:
        status = "cancelled" if cancelled else ("unknown" if error else "returned")
        reason = reason or (("cancelled_after_observed_start" if key in service._observation_started else "cancelled_before_observed_start") if cancelled else (
            "execution_exception:" + error if error else ("worker_returned_send_not_observed" if key in service._observation_entered else "reply_entry_not_observed"))
        )
        execution((ref,), "priority_queue_cancelled" if queued else "priority_task_ended", status, reason,
                  session_id=key[0], message_ids=[key[1]])
    service._observation_started.discard(key)
    service._observation_entered.discard(key)
    if not service.owns_key(key):
        service._observation_references.pop(key, None)


@isolated
def priority_forget(service: Any, key: Tuple[str, ...]) -> None:
    if key not in service._tasks and key not in service._pending_keys:
        service._observation_references.pop(key, None)


@isolated
def priority_fallback(runtime: Any, message: Any, ref: Optional[Reference]) -> None:
    if not store.policy.enabled or ref is None:
        return
    scope = _priority_scope(runtime, message)
    if scope is None or scope != (ref.generation, ref.opportunity.account_id):
        return
    state = runtime._observation_workset
    mid = identity("message", runtime.session_id, message.message_id)
    if (state.opportunity is None or state.generation != ref.generation or mid not in state.messages
            or ref.session_id != runtime.session_id or ref.opportunity.run_id != store.run_id):
        raise ValueError("Fallback is not in the explicitly registered pending workset")
    state.priority_fallbacks[mid] = ref
    target = Reference(runtime.session_id, state.generation, tuple(state.messages.values()), state.opportunity)
    transfer = OpportunityTransfer(
        identity("priority_fallback", ref.execution_id, target.opportunity.opportunity_id, mid),
        ref.opportunity.opportunity_id, target.opportunity.opportunity_id, mid, ref.execution_id,
        "priority_to_ordinary", time.time(), ref.opportunity.account_id,
    )
    event = ExecutionEvent(
        event_id=uuid.uuid4().hex, opportunity_id=ref.opportunity.opportunity_id, evaluation_id=None,
        execution_id=ref.execution_id, occurred_at=time.time(), stage="priority_fallback_registered",
        status="ordinary_pending", reason="runtime_registration", logical_turn_id=None, cycle_id=None,
        replay_status="not_observed", message_event_ids=json.dumps([mid]), tool_names="[]",
        link_source=ref.link_source, wait_tool_call_id=None, parent_execution_id=ref.parent_execution_id,
    )
    # The transition fact and both parents commit together, or all remain unobserved.
    store.submit(Batch(ref.session_id, ref.generation, ref.messages, ref.opportunity,
                       execution=event, transfer=transfer, related_reference=target))


@isolated
def finalize(runtime: Any, status: str) -> None:
    if not store.policy.enabled:
        return
    state = runtime._observation_workset
    if state.opportunity is None:
        return
    opportunity = replace(state.opportunity, status=status, finalized_at=time.time())
    store.submit(Batch(runtime.session_id, state.generation, tuple(state.messages.values()), opportunity))
    state.clear()


async def reload_policy(_sections: Sequence[str] = ()) -> None:
    from src.config.config import global_config

    try:
        config = global_config.decision_observation
        policy = Policy(
            config.enabled, frozenset(config.authorized_session_ids), config.retention_days, config.queue_capacity
        )
        await asyncio.to_thread(store.configure, policy)
    except Exception:
        store.errors += 1
        store._warning("configure_failed")


async def start() -> None:
    from src.common.logger import get_logger
    from src.config.config import config_manager

    global _registered_config_manager
    await reload_policy()
    config_manager.register_reload_callback(reload_policy, sections=("decision_observation",))
    _registered_config_manager = config_manager
    get_logger("decision_observation").info(
        f"Decision Observation 初始化 | enabled={store.policy.enabled} | 授权群聊={len(store.policy.authorized_session_ids)}"
    )


async def shutdown() -> None:
    global _registered_config_manager
    manager = _registered_config_manager
    if manager is not None:
        manager.unregister_reload_callback(reload_policy)
        _registered_config_manager = None
    drained = await asyncio.to_thread(store.close)
    if manager is not None:
        from src.common.logger import get_logger

        get_logger("decision_observation").info(
            f"Decision Observation 关闭 | drained={drained} | dropped={store.dropped} | errors={store.errors}"
        )
