"""M1 回归使用临时空数据库与隔离审计，不伪造监控/决策事件，不触达 QQ/LLM。"""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import sqlite3
import threading
import time

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from src.common.database.database_model import ChatSession
from src.decision_replay.store import ReplayStore
from src.services.owner_admin import read_data, read_service
from src.services.owner_admin.audit_store import OwnerAdminAuditStore
from src.services.owner_admin.command_router import OwnerAdminCommandRouter
from src.services.owner_admin.read_commands import READ_ACTIONS, parse_read_command
from src.services.owner_admin.read_service import OwnerReadService, query_window, report, safe_text

COMMANDS = [
    "/dfy status", "/dfy tokens 1h", "/dfy stats 24h", "/dfy models", "/dfy errors 24h",
    "/dfy replay missing-id", "/dfy persona show", "/dfy memory me", "/dfy learning status",
    "/dfy chats", "/dfy health",
]


def incoming(body, *, user="100000001", group=False, mid="request"):
    from src.common.data_models.message_component_data_model import TextComponent

    return SimpleNamespace(
        platform="qq", session_id="private-owner", message_id=mid,
        raw_message=SimpleNamespace(components=[TextComponent(body)]),
        message_info=SimpleNamespace(group_info=SimpleNamespace(group_id="123") if group else None,
                                     user_info=SimpleNamespace(user_id=user),
                                     additional_config={"account_id": "bot"}),
    )


@pytest.fixture
def environment(tmp_path, monkeypatch):
    from src.config import config
    from src.decision_replay import recorder
    from src.services import bot_account_service, statistics_service
    from src.maisaka.monitor import event_store

    monkeypatch.setattr(config.global_config.bot, "owner_qq", "100000001")
    monkeypatch.setattr(config.global_config.chat.tag_memory, "data_dir", str(tmp_path / "tags"))
    monkeypatch.setattr(bot_account_service.bot_account_service, "is_bot_self", lambda *args: True)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    @contextmanager
    def db(*args, **kwargs):
        with Session(engine) as session:
            yield session

    with db() as session:
        session.add(ChatSession(session_id="known", platform="qq", account_id="bot", group_id="123",
                                group_name="测试群", last_active_timestamp=datetime(2026, 1, 1)))
        session.commit()
    monkeypatch.setattr(read_data, "get_db_session", db)
    monkeypatch.setattr(event_store, "get_db_session", db)
    monkeypatch.setattr(statistics_service, "get_db_session", db)
    # Token 口径不依赖消息数；避免其额外访问默认聊天库。
    monkeypatch.setattr(statistics_service, "count_messages", lambda **kwargs: 0)
    store = ReplayStore(tmp_path / "replay.db")
    with closing(store.connect()):
        pass  # 仅初始化真实 schema，无合成事件。
    monkeypatch.setattr(recorder, "store", store)
    monkeypatch.setattr(OwnerReadService, "_runtime_snapshot", staticmethod(lambda: {
        "loop_running": True, "broker_started": False, "plugins_running": False,
        "plugin_statuses": {}, "loaded_chats": frozenset(), "chat_runtimes": {},
    }))
    router = OwnerAdminCommandRouter(audit_store=OwnerAdminAuditStore(tmp_path / "audit.db"))
    yield router, store, db
    store.close()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", COMMANDS)
async def test_all_eleven_commands_run_through_m0_and_are_audited(environment, body):
    router, store, _ = environment
    result = await router.handle_message(incoming(body))
    assert result.success, result
    assert result.status == "succeeded"
    assert len(result.message) <= 1500
    with sqlite3.connect(router.audit_store.path) as connection:
        row = connection.execute("SELECT action_id,authorization_result,execution_result FROM owner_admin_audit").fetchone()
    assert row == (OwnerAdminCommandRouter._parse(body).action_id, "allowed", "succeeded")
    with closing(store.connect_readonly()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", COMMANDS)
@pytest.mark.parametrize("user,group", [("stranger", False), ("100000001", True), ("stranger", True)])
async def test_every_query_denies_non_owner_or_group_before_data_access(environment, monkeypatch, body, user, group):
    router, _, _ = environment
    query = Mock(side_effect=AssertionError("未经授权不得读取数据"))
    monkeypatch.setattr(OwnerReadService, "_query", query)
    result = await router.handle_message(incoming(body, user=user, group=group))
    assert not result.success and result.status == "denied"
    query.assert_not_called()


@pytest.mark.parametrize("body", [
    "/dfy tokens 999d", "/dfy tokens -1h", "/dfy stats other 24h", "/dfy health --page 0",
    "/dfy models --page 101", "/dfy persona set hi", "/dfy memory other", "/dfy memory me --person_id stranger",
    "/dfy replay latest", "/dfy replay latest arbitrary", "/dfy tokens 24h extra", "/dfy restart",
])
def test_invalid_or_write_syntax_remains_unregistered(body):
    assert OwnerAdminCommandRouter._parse(body).action_id == "owner_admin.unregistered"


@pytest.mark.parametrize("body", [
    "/dfy tokens 24h", "/dfy tokens 7d", "/dfy stats 123 24h", "/dfy replay latest 123",
    "/dfy memory me 喜欢", "/dfy chats --page 2",
])
def test_supported_variants(body):
    assert parse_read_command(body.split()) is not None


@pytest.mark.asyncio
async def test_empty_replay_missing_identity_and_expiration_are_not_invented(environment):
    router, _, _ = environment
    result = await router.handle_message(incoming("/dfy replay missing-id"))
    assert "未找到" in result.message and "超期" in result.message
    assert "原因" in result.message and "无法" in result.message
    assert "回复成功" not in result.message


@pytest.mark.asyncio
async def test_tokens_matches_webui_same_empty_interval_without_claiming_bill(environment, monkeypatch):
    from src.services.statistics_service import get_model_statistics, get_summary_statistics

    router, _, _ = environment
    start, end = query_window("24h")
    monkeypatch.setattr(read_service, "query_window", lambda name: (start, end))
    expected = await get_summary_statistics(start.astimezone().replace(tzinfo=None), end.astimezone().replace(tzinfo=None))
    models = await get_model_statistics(start.astimezone().replace(tzinfo=None), end.astimezone().replace(tzinfo=None))
    result = await router.handle_message(incoming("/dfy tokens 24h"))
    assert f"记录请求={expected.total_requests}" in result.message
    assert f"输入={expected.input_tokens}" in result.message and f"输出={expected.output_tokens}" in result.message
    assert "真实账单：不可获取" in result.message and "估算费用" in result.message
    assert models == [] and "Asia/Shanghai" in result.message


@pytest.mark.asyncio
async def test_empty_reply_projection_matches_webui_and_never_substitutes_legacy_counts(environment):
    from src.decision_replay.reply_metrics import statistics

    router, store, _ = environment
    expected = statistics(store, read_only=True)
    result = await router.handle_message(incoming("/dfy stats 123 24h"))
    assert expected["effective_since"] is None
    assert expected["counts"]["platform_messages"] == 0
    assert "不可统计" in result.message
    assert "旧口径" not in result.message or "冒充" in result.message


def test_time_windows_and_pages_are_bounded_and_explicit():
    start, end = query_window("7d")
    assert (end - start) == timedelta(days=7)
    assert start.utcoffset() == timedelta(hours=8)
    result = report("models", ["x" * 500] * 100, 2, note="api_key=secret-value")
    assert len(result.message) < 1500 and "第 2/25 页" in result.message
    assert "secret-value" not in result.message
    assert safe_text("https://host/?token=private") == "[redacted]"


def test_replay_reader_never_creates_or_writes_data(tmp_path):
    store = ReplayStore(tmp_path / "missing.db")
    with pytest.raises(sqlite3.OperationalError):
        store.connect_readonly()
    assert not store.path.exists()
    with closing(store.connect()):
        pass
    with closing(store.connect_readonly()) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM events")


@pytest.mark.asyncio
async def test_slow_queries_do_not_block_main_loop(environment, monkeypatch):
    router, _, _ = environment
    started = threading.Event()
    finished = threading.Event()

    def slow_query(self, command, request):
        started.set()
        finished.wait(timeout=2)
        return ["空数据"], "测试阻塞数据源"

    monkeypatch.setattr(OwnerReadService, "_query", slow_query)
    task = asyncio.create_task(router.handle_message(incoming("/dfy tokens 24h")))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        begin = time.monotonic()
        await asyncio.sleep(0.02)
        assert time.monotonic() - begin < 0.25
        assert not task.done()
    finally:
        finished.set()
        await task


@pytest.mark.asyncio
async def test_missing_bot_scope_is_denied_and_ordinary_chat_is_untouched(environment, monkeypatch):
    from src.services import bot_account_service

    router, _, _ = environment
    assert await router.handle_message(incoming("普通聊天")) is None
    monkeypatch.setattr(bot_account_service.bot_account_service, "is_bot_self", lambda *args: False)
    result = await router.handle_message(incoming("/dfy health"))
    assert result.error_code == "bot_account_unavailable"


def test_permission_registry_contains_only_eleven_new_read_actions(environment):
    router, _, _ = environment
    actions = [spec for spec in router.registry.list_actions() if spec.action_id.startswith("owner_admin.read.")]
    assert len(actions) == 11
    assert {spec.permission_id for spec in actions} == {value[0] for value in READ_ACTIONS.values()}
    assert all(spec.owner_required and spec.allowed_chat_types == frozenset({"private"}) for spec in actions)


@pytest.mark.asyncio
async def test_memory_filters_only_authenticated_user_and_never_expires_tags(environment, monkeypatch):
    from pathlib import Path
    from src.config.config import global_config
    from src.services.tag_memory_store import TagMemoryStore

    router, _, _ = environment
    directory = Path(global_config.chat.tag_memory.data_dir)
    directory.mkdir()
    store = TagMemoryStore(directory / "memory_index.db")
    store.initialize()
    original = TagMemoryStore.list_current_tags
    users = []

    def checked_read(self, session_id, **kwargs):
        users.append(kwargs["person_users"])
        assert kwargs["read_only"] is True
        return original(self, session_id, **kwargs)

    expiration = Mock(side_effect=AssertionError("只读查询不得更新标签"))
    monkeypatch.setattr(TagMemoryStore, "_expire_manual_activity", expiration)
    monkeypatch.setattr(TagMemoryStore, "list_current_tags", checked_read)
    result = await router.handle_message(incoming("/dfy memory me"))
    assert result.success and "无可用记录" in result.message
    assert users == [["100000001"]]
    expiration.assert_not_called()


def test_missing_memory_read_does_not_create_database(tmp_path):
    from src.services.tag_memory_store import TagMemoryStore

    store = TagMemoryStore(tmp_path / "missing-memory.db")
    with pytest.raises(sqlite3.OperationalError):
        store.list_current_tags("session", person_users=["100000001"], read_only=True)
    assert not store.db_path.exists()


@pytest.mark.asyncio
async def test_unknown_cache_and_incomplete_replay_are_explicit(environment):
    router, _, _ = environment
    tokens = await router.handle_message(incoming("/dfy tokens 24h", mid="tokens"))
    assert "命中率=不可统计" in tokens.message
    assert "归一化" in tokens.message
    replay = await router.handle_message(incoming("/dfy replay missing-id", mid="replay"))
    assert "10000" in replay.message
    assert "不等于完整决策回放" in replay.message


def test_report_caps_total_pages_and_marks_truncation():
    result = report("chats", ["data"] * 501, 100)
    assert result.data["pages"] == 100 and result.data["row_count"] == 400
    assert "前400行" in result.message


def test_real_runtime_snapshot_and_process_probe_are_readable():
    snapshot = OwnerReadService._runtime_snapshot()
    assert isinstance(snapshot["chat_runtimes"], dict)
    usage = OwnerReadService._process_usage()
    assert usage and all(isinstance(line, str) for line in usage)


@pytest.mark.parametrize("secret", [
    'api_key=private-value', '"api_key": "private-value"', "Bearer private-value",
    "ghp_" + "a" * 30,
])
def test_secret_text_is_never_rendered(secret):
    assert "private-value" not in safe_text(secret)
    assert "a" * 30 not in safe_text(secret)


@pytest.mark.asyncio
async def test_consecutive_queries_leave_event_loop_responsive(environment, monkeypatch):
    router, _, _ = environment

    def query(self, command, request):
        time.sleep(0.03)
        return ["无记录"], "隔离阻塞源"

    monkeypatch.setattr(OwnerReadService, "_query", query)
    tasks = [asyncio.create_task(router.handle_message(incoming("/dfy tokens 24h", mid=str(i)))) for i in range(8)]
    ticks = 0
    while not all(task.done() for task in tasks):
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks >= 6
    assert all(result.success for result in await asyncio.gather(*tasks))
