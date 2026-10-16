"""QA-004：慢会话事务不占用事件循环，且并发 miss 只创建一个会话。"""

from contextlib import contextmanager
from importlib import import_module
from threading import Event, get_ident
from types import SimpleNamespace

import asyncio

import pytest
from sqlmodel import Session, create_engine, select

from src.chat.message_receive.chat_manager import BotChatSession, ChatManager
from src.common.database.database_model import ChatSession
from src.common.utils.utils_session import SessionUtils


module = import_module("src.chat.message_receive.chat_manager")


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'sessions.db').as_posix()}")
    ChatSession.__table__.create(engine)
    threads = []
    entered, release = Event(), Event()

    @contextmanager
    def get_session():
        threads.append(get_ident())
        entered.set()
        assert release.wait(2), "事件循环未能在事务等待期间继续执行"
        with Session(engine) as session:
            yield session
            session.commit()
        threads.append(get_ident())

    monkeypatch.setattr(module, "get_db_session", get_session)
    yield SimpleNamespace(engine=engine, entered=entered, release=release, threads=threads)
    release.set()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "existing", "cached_route", "cached_identity", "register"])
async def test_slow_session_transaction_leaves_loop_responsive(database, path):
    manager = ChatManager()
    account_id = "bot" if path == "cached_route" else None
    session_id = SessionUtils.calculate_session_id("qq", user_id="user", group_id="group", account_id=account_id)
    record = BotChatSession(session_id=session_id, platform="qq", group_id="group", group_name="old")
    if path == "existing":
        with Session(database.engine) as session:
            session.add(record.to_db_instance())
            session.commit()
    if path.startswith("cached") or path == "register":
        manager.sessions[session_id] = record
    message = SimpleNamespace(platform="qq", message_info=SimpleNamespace(
        additional_config={}, group_info=SimpleNamespace(group_id="group", group_name="new"),
        user_info=SimpleNamespace(user_id="user", user_nickname="user", user_cardname=None),
    ))
    if path in {"cached_identity", "existing"}:
        manager.last_messages[session_id] = message
    operation = asyncio.create_task(
        asyncio.to_thread(manager.register_message, message) if path == "register" else
        manager.get_or_create_session("qq", "user", "group", account_id=account_id)
    )
    try:
        assert await asyncio.to_thread(database.entered.wait, 1)
        # 工作线程事务尚未完成时，主循环仍能运行并释放它；无需依赖毫秒级计时阈值。
        assert not operation.done()
        assert not database.release.is_set()
        database.release.set()
        result = await asyncio.wait_for(operation, 3)
        if path != "register":
            assert result is manager.sessions[session_id]
        assert database.threads
        assert all(thread != get_ident() for thread in database.threads)
        assert all(database.threads[i] == database.threads[i + 1] for i in range(0, len(database.threads), 2))
        with Session(database.engine) as session:
            rows = session.exec(select(ChatSession)).all()
            assert len(rows) == 1
            assert rows[0].session_id == session_id
            if path in {"cached_identity", "existing", "register"}:
                assert rows[0].group_name == "new"
            if path == "cached_route":
                assert rows[0].account_id == "bot"
    finally:
        database.release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_misses_return_same_committed_session(database):
    manager = ChatManager()
    database.release.set()
    sessions = await asyncio.gather(*(
        manager.get_or_create_session("qq", "user", "group") for _ in range(12)
    ))
    assert all(session is sessions[0] for session in sessions)
    with Session(database.engine) as session:
        assert len(session.exec(select(ChatSession)).all()) == 1


@pytest.mark.asyncio
async def test_database_error_propagates_from_worker(monkeypatch):
    manager = ChatManager()

    @contextmanager
    def failed_session():
        raise RuntimeError("injected database failure")
        yield

    monkeypatch.setattr(module, "get_db_session", failed_session)
    with pytest.raises(RuntimeError, match="injected database failure"):
        await manager.get_or_create_session("qq", "user", "group")
    assert not manager.sessions
