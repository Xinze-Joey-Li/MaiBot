"""Correlated logical actions and delivery lifecycle in the existing replay event model."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, Dict, Optional

import asyncio
import uuid

from . import recorder


@dataclass(frozen=True)
class Reply:
    reply_id: str
    session_id: str
    source: str
    session_type: str
    system: bool = False


current_reply: ContextVar[Optional[Reply]] = ContextVar("reply_action", default=None)
current_generation: ContextVar[Optional[str]] = ContextVar("reply_generation", default=None)


def identity(prefix: str, *parts: str) -> str:
    return prefix + ":" + uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)).hex


def correlation() -> Dict[str, Any]:
    reply = current_reply.get()
    return ({"reply_id": reply.reply_id, "source": reply.source, "session_type": reply.session_type,
             "system": reply.system, "generation_group_id": current_generation.get()} if reply else {})


async def emit(kind: str, key: str, *, reason: str = "", detail: Optional[Dict[str, Any]] = None,
               reply: Optional[Reply] = None) -> None:
    active = reply or current_reply.get()
    if active is None:
        return
    payload = {"reply_id": active.reply_id, "source": active.source, "session_type": active.session_type,
               "system": active.system, **(detail or {})}
    await asyncio.to_thread(recorder.record, "reply_stats", "reply_stats." + kind, kind, reason,
                            session_id=active.session_id, detail=payload,
                            event_id=identity(kind, active.session_id, key), persist=True)


@asynccontextmanager
async def action(session_id: str, source: str, session_type: str, *, reply_id: Optional[str] = None, system: bool = False):
    existing = current_reply.get()
    if existing is not None and existing.session_id == session_id:
        yield existing
        return
    trace = recorder.current_trace.get()
    own = trace is None or trace.session_id != session_id
    trace = recorder.new_trace(session_id, source) if own else trace
    reply = Reply(reply_id or uuid.uuid4().hex, session_id, source, session_type, system)
    with recorder.bind(trace):
        token = current_reply.set(reply)
        await emit("action_started", reply.reply_id)
        execution_id = uuid.uuid4().hex
        await emit("action_opened", execution_id)
        failed = False
        try:
            yield reply
        except BaseException:
            failed = True
            raise
        finally:
            await emit("action_closed", execution_id, detail={"action_failed": failed})
            current_reply.reset(token)


def planner_action(function):
    @wraps(function)
    async def wrapper(tool_ctx, invocation, *args, **kwargs):
        if not recorder.safe_enabled():
            return await function(tool_ctx, invocation, *args, **kwargs)
        session_id = tool_ctx.runtime.session_id
        trace = recorder.current_trace.get()
        rid = identity("reply", session_id, trace.logical_turn_id if trace else "", invocation.call_id)
        async with action(session_id, "planner", "group" if tool_ctx.runtime.chat_stream.is_group_session else "private", reply_id=rid):
            result = await function(tool_ctx, invocation, *args, **kwargs)
            if not result.success:
                await emit("action_failed", rid, reason="reply_tool_failed")
            return result
    return wrapper


def priority_action(function):
    @wraps(function)
    async def wrapper(self, message, *args, **kwargs):
        if not recorder.safe_enabled():
            return await function(self, message, *args, **kwargs)
        async with action(message.session_id, "priority_at", "group" if message.message_info.group_info else "private"):
            return await function(self, message, *args, **kwargs)
    return wrapper


def component_flags(sequence) -> Dict[str, bool]:
    from src.common.data_models.message_component_data_model import EmojiComponent, ImageComponent, TextComponent
    return {"has_text": any(isinstance(p, TextComponent) and bool(p.text.strip()) for p in sequence.components),
            "has_emoji": any(isinstance(p, EmojiComponent) for p in sequence.components),
            "has_image": any(isinstance(p, ImageComponent) for p in sequence.components)}


def classify_sequence(sequence) -> str:
    flags = component_flags(sequence)
    return "mixed" if flags["has_text"] and (flags["has_emoji"] or flags["has_image"]) else (
        "text" if flags["has_text"] else "emoji" if flags["has_emoji"] else "other")


def classify(message) -> str:
    return classify_sequence(message.raw_message)


class DeliveryUncertain(RuntimeError):
    """The platform may have accepted this send; automatic fallback can duplicate it."""


def outbound_id(message) -> str:
    flags = message.message_info.additional_config
    return flags.setdefault("statistics_outbound_id", message.message_id)


def message_reply(message, source: str = "independent_send") -> Reply:
    active = current_reply.get()
    if active is not None and active.session_id == message.session_id:
        return active
    flags = message.message_info.additional_config
    if source == "outbound_send":
        source = "independent_send"
    return Reply(flags.setdefault("statistics_reply_id", uuid.uuid4().hex), message.session_id,
                 "system" if flags.get("system_message") else "priority_at" if flags.get("priority_at_response")
                 else "silence_emoji" if flags.get("silence_emoji") else "auto_repeat" if flags.get("auto_repeat") else source,
                 "group" if message.message_info.group_info else "private", bool(flags.get("system_message")))


async def plan(message, *, optional: bool = False) -> None:
    await emit("message_planned", outbound_id(message), detail={"outbound_message_id": outbound_id(message),
               "content_type": classify(message), "optional": optional, **component_flags(message.raw_message)})


async def skip(message, reason: str) -> None:
    message.message_info.additional_config["statistics_send_result"] = "skipped"
    await emit("send_skipped", outbound_id(message), reason=reason,
               detail={"outbound_message_id": outbound_id(message), "content_type": classify(message)})


async def generation_request(request, task: str, session_id: str) -> None:
    purpose = request.trace_context.request_type if request.trace_context else ""
    if task != "replyer" or purpose not in {"maisaka.replyer", "priority_at.replyer", "replyer"}:
        return
    reply = current_reply.get()
    if reply is None or reply.session_id != session_id:
        return
    trace = request.trace_context
    if trace is None:
        return
    request_id = f"{trace.request_id}:{trace.attempt}"
    await emit("generation_request", request_id, detail={"generation_request_id": request_id,
               "generation_group_id": current_generation.get(), "request_type": purpose})


async def receipt(message, route_key, result, attempt_id: str) -> None:
    from src.platform_io.types import DeliveryStatus, DriverKind
    oid = outbound_id(message)
    base = {"outbound_message_id": oid, "attempt_id": attempt_id, "content_type": classify(message),
            "platform": route_key.platform, "account_id": route_key.account_id or "", **component_flags(message.raw_message)}
    flags = message.message_info.additional_config
    if result.driver_kind == DriverKind.LOCAL:
        await skip(message, "local_render_not_platform")
    elif result.status == DeliveryStatus.SENT and (result.external_message_id or (
            result.metadata.get("platform_acknowledged") is True and result.metadata.get("delivery_id"))):
        confirmed_id = str(result.external_message_id or ("delivery:" + str(result.metadata["delivery_id"])))
        flags["statistics_send_result"] = "sent"
        await emit("send_confirmed", identity("platform", route_key.platform, route_key.account_id or "", message.session_id,
                   confirmed_id), reason="platform_acknowledged",
                   detail={**base, "platform_message_id": confirmed_id})
    elif result.status == DeliveryStatus.DROPPED:
        await skip(message, "driver_dropped")
    elif result.status == DeliveryStatus.FAILED:
        if flags.get("statistics_send_result") != "sent":
            flags["statistics_send_result"] = "failed"
        await emit("send_failed", attempt_id, reason="platform_rejected", detail=base)
    else:
        if flags.get("statistics_send_result") != "sent":
            flags["statistics_send_result"] = "unknown"
        await emit("send_unknown", attempt_id, reason="timeout_or_unconfirmed_receipt", detail=base)


async def unresolved(message) -> Optional[Dict[str, Any]]:
    """Never resubmit a stable outbound ID whose previous dispatch may have reached the platform."""
    if not recorder.safe_enabled() or not recorder.store.path.exists():
        return None
    state = await asyncio.to_thread(recorder.store.outbound_state, message.session_id, outbound_id(message))
    return state if state is not None and state["status"] in {"sent", "unknown"} else None


@asynccontextmanager
async def generation_scope(session_id: str, generation_id: str, reply_message=None):
    active = current_reply.get()
    token = None
    if active is None or active.session_id != session_id:
        group = reply_message.message_info.group_info if reply_message is not None else None
        token = current_reply.set(Reply("generation:" + generation_id, session_id, "direct_generation",
                                      "group" if group else "private" if reply_message is not None else "unknown"))
    trace = recorder.current_trace.get()
    trace = trace if trace is not None and trace.session_id == session_id else recorder.new_trace(session_id, "direct_generation")
    with recorder.bind(trace):
        try:
            yield
        finally:
            if token is not None:
                current_reply.reset(token)


def platform_send(function):
    @wraps(function)
    async def wrapper(self, message, route_key, metadata=None):
        if not recorder.safe_enabled():
            return await function(self, message, route_key, metadata)
        from src.platform_io.types import DeliveryBatch, DeliveryReceipt, DeliveryStatus
        reply = message_reply(message)
        async with action(reply.session_id, reply.source, reply.session_type, reply_id=reply.reply_id, system=reply.system):
            await plan(message, optional=bool(message.message_info.additional_config.get("reply_emoji_attachment")))
            prior = await unresolved(message)
            if prior is not None:
                confirmed = prior["status"] == "sent"
                message.message_info.additional_config["statistics_send_result"] = "sent" if confirmed else "unknown"
                return DeliveryBatch(internal_message_id=message.message_id, route_key=route_key, receipts=[DeliveryReceipt(
                    internal_message_id=message.message_id, route_key=route_key,
                    status=DeliveryStatus.SENT if confirmed else DeliveryStatus.UNKNOWN,
                    external_message_id=prior["platform_message_ids"][-1] if confirmed else None,
                    error=None if confirmed else "Prior dispatch unresolved; reconcile before resubmitting")])
            result = await function(self, message, route_key, metadata)
            if not result.receipts:
                await skip(message, "no_driver")
            return result
    return wrapper


async def reconcile_receipt(result) -> None:
    """Late trusted receipts reuse persisted identity; ambiguous cross-session IDs are rejected."""
    from src.decision_observation.delivery import receipt_observed as observation_receipt
    observation_receipt(result)
    from contextlib import closing
    def lookup():
        with closing(recorder.store.connect()) as connection:
            rows = connection.execute("SELECT * FROM outbound_dispatches WHERE outbound_message_id=? LIMIT 2",
                                      (result.internal_message_id,)).fetchall()
            return dict(rows[0]) if len(rows) == 1 else None
    row = await asyncio.to_thread(lookup)
    if row is None:
        return
    reply = Reply(row["reply_id"], row["session_id"], row["source"], row["session_type"], bool(row["system"]))
    trace = recorder.new_trace(reply.session_id, reply.source, row["decision_id"])
    with recorder.bind(trace):
        token = current_reply.set(reply)
        try:
            await emit("send_confirmed", identity("platform", result.route_key.platform, result.route_key.account_id or "",
                       reply.session_id, str(result.external_message_id)), reason="late_platform_acknowledged", detail={
                           "outbound_message_id": row["outbound_message_id"], "platform": result.route_key.platform,
                           "account_id": result.route_key.account_id or "", "platform_message_id": str(result.external_message_id),
                           "content_type": row["content_type"], "has_text": bool(row["has_text"]),
                           "has_emoji": bool(row["has_emoji"])})
        finally:
            current_reply.reset(token)


def receipt_observed(result) -> None:
    """The broker records in-band receipts; only independent late confirmations enter here."""
    from src.platform_io.types import DeliveryStatus, DriverKind
    if (not recorder.safe_enabled() or current_reply.get() is not None or result.status != DeliveryStatus.SENT
            or result.driver_kind == DriverKind.LOCAL or not result.external_message_id):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        recorder.logger.warning("Late receipt needs async reconciliation; no running event loop")
        return
    task = loop.create_task(reconcile_receipt(result))
    def completed(future):
        if not future.cancelled() and future.exception() is not None:
            recorder.logger.warning("Late receipt reconciliation failed", exc_info=False)
    task.add_done_callback(completed)
