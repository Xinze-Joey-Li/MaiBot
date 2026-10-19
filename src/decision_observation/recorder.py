"""Small, failure-isolated adapters; production values are read, never recomputed."""

from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, TYPE_CHECKING
import asyncio
import json
import time
import uuid

from .models import Batch, Evaluation, FEATURE_VERSION, MessageEvent, Opportunity, Policy, identity
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
) -> None:
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
