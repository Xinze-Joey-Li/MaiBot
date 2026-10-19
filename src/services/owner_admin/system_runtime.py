"""独立软暂停状态及本地管理回执豁免，不能由消息内容或模型字段设置。"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional, Tuple

from . import overrides

_management: ContextVar[str] = ContextVar("owner_management_delivery", default="")
_lease: ContextVar[Optional[Tuple[str, str]]] = ContextVar("owner_pause_lease", default=None)


def account_for_session(session_id: str) -> str:
    from src.chat.message_receive.chat_manager import chat_manager

    chat = chat_manager.sessions.get(session_id)
    return chat.account_id or "" if chat is not None and chat.platform == "qq" else ""


def paused(account: str) -> bool:
    return bool(account) and any(entry.property == "soft_paused"
                                 for entry in overrides.override_manager.active("bot", account))


def session_paused(session_id: str) -> bool:
    return paused(account_for_session(session_id))


def capture_lease(session_id: str) -> None:
    account = account_for_session(session_id)
    _lease.set((account, overrides.override_manager.pause_epochs.get(account, "")))


def stale_lease(account: str) -> bool:
    lease = _lease.get()
    return lease is not None and lease[0] == account and lease[1] != overrides.override_manager.pause_epochs.get(account, "")


@contextmanager
def management_delivery(session_id: str) -> Iterator[None]:
    token = _management.set(session_id)
    try:
        yield
    finally:
        _management.reset(token)


class SoftPauseInterrupt(Exception):
    """只结束当前普通聊天轮，不取消主循环或管理通道。"""


def check_cycle(session_id: str) -> None:
    account = account_for_session(session_id)
    if paused(account) or stale_lease(account):
        raise SoftPauseInterrupt()


@contextmanager
def admitted_lease(lease: Tuple[str, str]) -> Iterator[None]:
    """任务创建时继承准入时的暂停代际，避免派发后、启动前暂停又恢复的竞态。"""
    token = _lease.set(lease)
    try:
        yield
    finally:
        _lease.reset(token)
