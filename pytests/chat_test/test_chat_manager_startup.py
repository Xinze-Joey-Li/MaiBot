"""QA-003：会话恢复失败必须可见，且不能发布半加载缓存。"""

from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.chat.message_receive.chat_manager import BotChatSession, ChatManager

module = import_module("src.chat.message_receive.chat_manager")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["query", "conversion", "commit"])
async def test_restore_failure_preserves_cache_and_can_retry(monkeypatch, failure):
    manager = ChatManager()
    old = BotChatSession("old", "qq", user_id="user")
    manager.sessions[old.session_id] = old
    cache = manager.sessions
    restored = BotChatSession("restored", "qq", group_id="group")
    rows = [restored.to_db_instance()]
    error = RuntimeError("injected restore failure")
    if failure == "conversion":
        rows.append(SimpleNamespace())
    db = Mock()
    db.exec.return_value.all.return_value = rows
    if failure == "query":
        db.exec.side_effect = error

    @contextmanager
    def session():
        yield db
        if failure == "commit":
            raise error

    monkeypatch.setattr(module, "get_db_session", session)
    with pytest.raises((RuntimeError, AttributeError)):
        await manager.initialize()
    assert manager.sessions is cache
    assert manager.sessions == {"old": old}
    db.exec.side_effect = None
    db.exec.return_value.all.return_value = [restored.to_db_instance()]
    failure = "none"
    await manager.initialize()
    assert manager.sessions is not cache
    assert set(manager.sessions) == {"restored"}


@pytest.mark.asyncio
async def test_successful_empty_restore_replaces_stale_cache(monkeypatch):
    manager = ChatManager()
    manager.sessions["old"] = BotChatSession("old", "qq", user_id="user")
    db = Mock()
    db.exec.return_value.all.return_value = []

    @contextmanager
    def session():
        yield db

    monkeypatch.setattr(module, "get_db_session", session)
    await manager.initialize()
    assert manager.sessions == {}
