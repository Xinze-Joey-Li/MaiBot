"""统一覆盖的运行时适配：可信直接触发、同轮人格快照和最终发送边界。"""

from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional, Tuple, TYPE_CHECKING
import time

from .overrides import EffectiveOverrides, override_manager
from . import system_runtime

from .trigger_policy import is_ignored, pending_trigger_messages

if TYPE_CHECKING:
    from src.maisaka.runtime import MaisakaHeartFlowChatting

_direct_messages: ContextVar[FrozenSet[Tuple[str, str]]] = ContextVar("owner_direct_triggers", default=frozenset())
_outbound_source: ContextVar[Optional[Any]] = ContextVar("owner_outbound_source", default=None)
_outbound_direct: ContextVar[str] = ContextVar("owner_direct_reply_session", default="")


@dataclass(frozen=True)
class Presentation:
    session_id: str
    snapshot: EffectiveOverrides
    valid_until: Optional[float]


_presentation: ContextVar[Optional[Presentation]] = ContextVar("owner_presentation", default=None)


def _capture_presentation(session_id: str) -> Presentation:
    from src.chat.message_receive.chat_manager import chat_manager

    chat = chat_manager.sessions.get(session_id)
    account = chat.account_id if chat is not None and chat.platform == "qq" else ""
    snapshot = override_manager.snapshot(session_id, account or "")
    deadlines = [entry.expires_at for entry in override_manager.active("bot", account or "")
                 if account and entry.property in {"style", "persona"} and entry.expires_at is not None]
    return Presentation(session_id, snapshot, min(deadlines) if deadlines else None)


def begin_turn(runtime: "MaisakaHeartFlowChatting") -> None:
    """仅由真实入站缓存冻结直接 @ 集合；模型不能用历史 @ 消息伪造当前豁免。"""
    system_runtime.capture_lease(runtime.session_id)
    messages = pending_trigger_messages(runtime)
    _direct_messages.set(frozenset((runtime.session_id, message.message_id) for message in messages if message.is_at))
    _outbound_direct.set("")
    _outbound_source.set(None)
    _presentation.set(_capture_presentation(runtime.session_id))


def turn_has_direct_trigger(session_id: str) -> bool:
    return any(sid == session_id for sid, _ in _direct_messages.get())


def presentation(session_id: str) -> EffectiveOverrides:
    captured = _presentation.get()
    if captured is None or captured.session_id != session_id or (
        captured.valid_until is not None and captured.valid_until <= time.time()
    ):
        captured = _capture_presentation(session_id)
    return captured.snapshot


def presentation_prompt(session_id: str) -> str:
    """Planner/Replyer 唯一表达覆盖文本来源；显式 style 优先于 persona 的表达预设。"""
    from src.common.prompt_i18n import load_prompt

    snapshot = presentation(session_id)
    preset = snapshot.style or snapshot.persona
    if not preset:
        return ""
    if preset not in {"gentle", "calm", "playful"}:
        raise ValueError("覆盖数据库包含未审核的人格预设")
    return load_prompt("owner_presentation_" + preset)


def direct_reply(
    normalize_arguments: Callable[[Dict[str, Any]], Tuple[Dict[str, Any], Any]],
) -> Callable[..., Any]:
    """对普通 reply 工具限定豁免：必须回复本轮真实接收的 @，不能根据模型正文授权。"""
    def decorate(function):
        @wraps(function)
        async def wrapped(tool_ctx, invocation, *args, **kwargs):
            arguments, _ = normalize_arguments(dict(invocation.arguments or {}))
            target = str(arguments.get("msg_id", ""))
            source = tool_ctx.runtime.find_source_message_by_id(target)
            if source is not None and is_ignored(source):
                return tool_ctx.build_failure_result(invocation.tool_name, "该用户已被屏蔽回复触发，本次未生成或发送。")
            source_token = _outbound_source.set(source)
            key = (tool_ctx.runtime.session_id, target)
            token = _outbound_direct.set(key[0] if key in _direct_messages.get() else "")
            try:
                if not override_manager.snapshot(key[0]).autonomous_allowed and _outbound_direct.get() != key[0]:
                    return tool_ctx.build_failure_result(invocation.tool_name, "该群已限制非@自主发言，本次未生成或发送回复。")
                return await function(tool_ctx, invocation, *args, **kwargs)
            finally:
                _outbound_direct.reset(token)
                _outbound_source.reset(source_token)
        return wrapped
    return decorate


def priority_reply(function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @wraps(function)
    async def wrapped(self, message, *args, **kwargs):
        # 调用方是已经过真实 @ 检测及准入的独立回复队列。
        if (is_ignored(message) or system_runtime.session_paused(message.session_id)
                or system_runtime.stale_lease(system_runtime.account_for_session(message.session_id))):
            return None
        system_runtime.capture_lease(message.session_id)
        source_token = _outbound_source.set(message)
        token = _outbound_direct.set(message.session_id if message.is_at else "")
        previous = _presentation.set(_capture_presentation(message.session_id))
        try:
            return await function(self, message, *args, **kwargs)
        finally:
            _presentation.reset(previous)
            _outbound_direct.reset(token)
            _outbound_source.reset(source_token)
    return wrapped


def managed_outbound(function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @wraps(function)
    async def wrapped(self, message, route_key, *args, **kwargs):
        from src.decision_replay.replies import skip
        from src.platform_io.types import DeliveryBatch, DeliveryReceipt, DeliveryStatus, DriverKind

        account = route_key.account_id or "" if route_key is not None and route_key.platform == "qq" else ""
        async with override_manager.account_barrier(account).delivery():
            async with override_manager.dispatch_lock(message.session_id):
                snapshot = override_manager.snapshot(message.session_id)
                source = _outbound_source.get()
                ignored = source is not None and source.session_id == message.session_id and is_ignored(source)
                stopped = system_runtime._management.get() != message.session_id and (system_runtime.paused(account) or system_runtime.stale_lease(account))
                if stopped or ignored or (not snapshot.autonomous_allowed and _outbound_direct.get() != message.session_id):
                    reason = "owner_soft_paused" if stopped else "owner_user_ignored" if ignored else "owner_autonomous_suppressed"
                    await skip(message, reason)
                    return DeliveryBatch(
                        internal_message_id=message.message_id, route_key=route_key,
                        receipts=[DeliveryReceipt(
                            internal_message_id=message.message_id, route_key=route_key, status=DeliveryStatus.DROPPED,
                            driver_id="owner_override", driver_kind=DriverKind.LOCAL, error=reason,
                        )],
                    )
                return await function(self, message, route_key, *args, **kwargs)
    return wrapped


def runtime_changed(targets: Tuple[str, ...]) -> None:
    """变更/统一到期通知只重排已有待处理消息，既不创建回合，也不强制回复。"""
    from src.chat.heart_flow.heartflow_manager import heartflow_manager

    for session_id in targets:
        runtime = heartflow_manager.heartflow_chat_list.get(session_id)
        if runtime is not None and runtime._running:
            runtime._schedule_message_turn()
