"""A02真实SQLite业务查询和群聊公开字段边界。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio
import json
import sqlite3

import pytest

from pytests.services import test_owner_admin_reads as reads
from src.common.database.database_model import ModelUsage
from src.services.owner_admin import natural_language, read_service
from src.services.owner_admin.read_commands import PUBLIC_GROUP_READ_ACTIONS
from src.services.owner_admin.read_service import OwnerReadService, query_window

base_environment = reads.environment


@pytest.fixture
def environment(base_environment, monkeypatch):
    router, store, db = base_environment
    end = datetime.now(read_service.LOCAL_TZ)
    start = end - timedelta(days=7)
    monkeypatch.setattr(read_service, "query_window", lambda name: (start, end))
    now = end.astimezone().replace(tzinfo=None)
    with db() as session:
        for hit, miss, inp, out, cache in [(30, 0, 100, 20, True), (0, 50, 50, 10, True), (90, 10, 90, 30, False)]:
            session.add(ModelUsage(model_name="https://private.example/sk-fixture",
                                   model_assign_name="account-private", model_api_provider_name="Cookie=fixture",
                                   request_type="test", timestamp=now - timedelta(minutes=5), time_cost=0.1,
                                   prompt_tokens=inp, completion_tokens=out, total_tokens=inp+out,
                                   prompt_cache_enabled=cache, prompt_cache_hit_tokens=hit,
                                   prompt_cache_miss_tokens=miss, cost=123.456789))
        session.commit()
    return router, store, db, start, end


def incoming(body, *, user="100000001", group=True, mid="request"):
    message = reads.incoming(body, user=user, group=group, mid=mid)
    message.timestamp = datetime.now()
    return message


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["", "1h", "24h", "7d", "today"])
async def test_public_windows_real_data_webui_semantics_and_no_private_fields(environment, window):
    from src.services import statistics_service
    router, _, _, start, end = environment
    expected = await statistics_service.get_summary_statistics(start.astimezone().replace(tzinfo=None),
                                                               end.astimezone().replace(tzinfo=None))
    result = await router.handle_message(incoming("/dfy tokens" + (" " + window if window else "")))
    assert result.success, result
    assert f"记录请求={expected.total_requests}" in result.message and expected.total_requests == 3
    assert "输入=240" in result.message and "输出=60" in result.message and "总Token=300" in result.message
    assert "缓存命中Token=30" in result.message and "未命中Token=120" in result.message
    assert "命中率=20.00%" in result.message and expected.cache_hit_rate == 0.2
    assert "Asia/Shanghai" in result.message and "[start,end]" in result.message
    serialized = result.message + json.dumps(dict(result.data), ensure_ascii=False)
    for private in ["123.456789", "370.370", "private.example", "sk-fixture", "Cookie=fixture", "account-private", "估算费用="]:
        assert private not in serialized
    models = await router.handle_message(incoming("/dfy tokens 24h --page 2", mid="models"))
    assert "模型1（匿名）" in models.message and "请求=3" in models.message
    assert "private.example" not in models.message


@pytest.mark.asyncio
async def test_private_details_unchanged(environment):
    router, *_ = environment
    result = await router.handle_message(incoming("/dfy tokens 1h", group=False))
    assert result.success and "本地记录估算费用=370.370367" in result.message
    assert "真实账单：不可获取" in result.message


@pytest.mark.asyncio
async def test_empty_and_unavailable_never_invent_rate(base_environment, monkeypatch):
    router, *_ = base_environment
    result = await router.handle_message(incoming("/dfy tokens 24h"))
    assert result.success and "记录请求=0" in result.message and "命中率=不可统计" in result.message
    assert "无缓存样本" in result.message
    monkeypatch.setattr("src.services.statistics_service._get_summary_statistics_sync",
                        Mock(side_effect=sqlite3.OperationalError("private datasource unavailable")))
    result = await router.handle_message(incoming("/dfy tokens 24h", mid="unavailable"))
    assert "数据源不可用" in result.message and "private datasource" not in result.message and "记录请求=0" not in result.message


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["/dfy tokens", "/dfy tokens 1h", "大肥鱼，今天用了多少 Token？"])
async def test_non_owner_cannot_query_even_with_owner_claim(environment, monkeypatch, body):
    router, *_ = environment
    query = Mock(side_effect=AssertionError("不得访问数据"))
    monkeypatch.setattr(OwnerReadService, "_query", query)
    message = incoming(body, user="stranger")
    message.message_info.user_info.user_nickname = "主人"
    message.message_info.additional_config.update(owner_verified=True, authorized=True)
    result = await router.handle_message(message)
    assert not result.success and result.status == "denied"
    query.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("body,window", [
    ("大肥鱼，今天用了多少 Token？", "today"),
    ("大肥鱼，看看最近一小时的 Token 消耗。", "1h"),
    ("今天调用了多少次模型？", "today"),
    ("大肥鱼，最近 24 小时的缓存命中率是多少？", "24h"),
    ("大肥鱼，这几天 Token 用量怎么样？", "7d"),
])
async def test_natural_group_same_registered_action(environment, body, window):
    router, *_ = environment
    result = await router.handle_message(incoming(body))
    assert result.success, result
    with sqlite3.connect(router.audit_store.path) as connection:
        action, parameters = connection.execute("SELECT action_id,parameter_summary FROM owner_admin_audit").fetchone()
    parameters = json.loads(parameters)
    assert action == "owner_admin.read.tokens" and parameters["window"] == window
    assert parameters["entry_source"] == "natural_language" and parameters["output_view"] == "public_view"


@pytest.mark.asyncio
async def test_fees_require_private_and_no_read_in_group(environment, monkeypatch):
    router, *_ = environment
    query = Mock(side_effect=AssertionError("群聊费用请求不得读取"))
    monkeypatch.setattr(OwnerReadService, "_query", query)
    result = await router.handle_message(incoming("大肥鱼，今天 API 花了多少钱？"))
    assert result.status == "private_required" and "私聊" in result.message
    query.assert_not_called()


@pytest.mark.parametrize("body", ["Token很贵啊", "缓存命中率是什么", "日志里写着：大肥鱼，今天用了多少Token？",
                                 "他刚才说今天调用了多少次模型", "大肥鱼，你知道Token是什么吗？"])
def test_token_chitchat_does_not_execute(body):
    assert natural_language.candidate(body) in (None, "clarify")


@pytest.mark.asyncio
async def test_other_actions_and_arbitrary_targets_remain_denied(environment):
    router, *_ = environment
    assert PUBLIC_GROUP_READ_ACTIONS == {"owner_admin.read.tokens"}
    for index, body in enumerate(["/dfy status", "/dfy stats 24h", "/dfy models", "/dfy health",
                                  "/dfy tokens 24h --group 99999", "/dfy tokens 999d",
                                  "/dfy memory me", "/dfy style gentle 2h"]):
        result = await router.handle_message(incoming(body, mid=str(index)))
        assert not result.success and result.status == "denied", (body, result)


@pytest.mark.asyncio
async def test_source_interval_day_and_no_configuration_writes(environment):
    from src.services.owner_admin import overrides
    router, *_ = environment
    start, end = query_window("today")
    assert start.hour == start.minute == start.second == 0 and end.utcoffset() == timedelta(hours=8)
    before = overrides.override_manager.active()
    assert (await router.handle_message(incoming("/dfy tokens 24h"))).success
    assert overrides.override_manager.active() == before


@pytest.mark.asyncio
async def test_slow_statistics_does_not_block_loop(environment, monkeypatch):
    import time
    from src.services import statistics_service
    router, *_ = environment
    original = statistics_service._get_summary_statistics_sync
    def slow(*args):
        time.sleep(0.15)
        return original(*args)
    monkeypatch.setattr(statistics_service, "_get_summary_statistics_sync", slow)
    task = asyncio.create_task(router.handle_message(incoming("/dfy tokens")))
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.01)
    assert (await task).success and ticks >= 8


@pytest.mark.asyncio
@pytest.mark.parametrize("body,group_id,status", [
    ("/dfy tokens 1h", "123", "sent"),
    ("大肥鱼，今天用了多少 Token？", "456", "sent"),
    ("/dfy tokens 24h", "123", "failed"),
    ("/dfy tokens 7d", "456", "unknown"),
    ("/dfy tokens 1h", "456", "cancelled"),
])
async def test_trusted_inbound_real_send_broker_route_and_delivery_audit(environment, monkeypatch, body, group_id, status):
    from src.chat.message_receive import bot as bot_module
    from src.chat.message_receive.chat_manager import BotChatSession, chat_manager
    from src.chat.message_receive.message import SessionMessage
    from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
    from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
    from src.platform_io.drivers.base import PlatformIODriver
    from src.platform_io.manager import PlatformIOManager
    from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverDescriptor, DriverKind, RouteBinding, RouteKey
    from src.services import send_service

    router, *_ = environment
    message = SessionMessage(message_id="native-request", timestamp=datetime.now(), platform="qq")
    message.message_info = MessageInfo(user_info=UserInfo("100000001", "群友"),
                                       group_info=GroupInfo(group_id, "测试群"), additional_config={"account_id": "bot"})
    message.raw_message = MessageSequence([TextComponent(body)])
    message.processed_plain_text = body
    sid = bot_module.SessionUtils.calculate_session_id("qq", user_id="100000001", group_id=group_id, account_id="bot")
    chat = BotChatSession(sid, "qq", group_id=group_id, group_name="测试群", account_id="bot")
    monkeypatch.setitem(chat_manager.sessions, sid, chat)
    monkeypatch.setattr(bot_module, "owner_admin_command_router", router)
    monkeypatch.setattr(send_service, "get_bot_account", lambda *args: "bot")
    async def hook(name, outbound, **kwargs):
        return SimpleNamespace(aborted=False, kwargs=kwargs), outbound
    monkeypatch.setattr(send_service, "_invoke_send_hook", hook)
    monkeypatch.setattr(send_service, "_notify_memory_automation_on_message_sent", AsyncMock())
    stored = AsyncMock(side_effect=AssertionError("管理回执不写聊天记忆"))
    monkeypatch.setattr(send_service, "_store_sent_message", stored)
    captured = []
    class Driver(PlatformIODriver):
        async def start(self):
            pass
        async def stop(self):
            pass
        async def send_message(self, message, route_key, metadata=None):
            captured.append((message.session_id, message.message_info.group_info.group_id,
                             message.processed_plain_text, route_key))
            if status == "cancelled":
                raise asyncio.CancelledError()
            return DeliveryReceipt(message.message_id, route_key, DeliveryStatus(status), driver_id=self.driver_id,
                                   driver_kind=DriverKind.PLUGIN, external_message_id="900001" if status == "sent" else None)
    manager = PlatformIOManager()
    driver = Driver(DriverDescriptor("fixture-driver", DriverKind.PLUGIN, "qq", account_id="bot", plugin_id="fixture.adapter"))
    manager.register_driver(driver)
    manager.bind_send_route(RouteBinding(RouteKey("qq", "bot"), driver.driver_id, DriverKind.PLUGIN))
    monkeypatch.setattr(send_service, "get_platform_io_manager", lambda: manager)
    bot = bot_module.ChatBot()
    planner = AsyncMock(side_effect=AssertionError("管理查询不应进入Planner或LLM"))
    monkeypatch.setattr(bot.heartflow_message_receiver, "process_message", planner)
    if status == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await bot.receive_message(message)
    else:
        await bot.receive_message(message)
    assert message.is_command and len(captured) == 1
    assert captured[0][0] == sid and captured[0][1] == group_id and captured[0][3] == RouteKey("qq", "bot")
    assert "总Token=300" in captured[0][2] and "370.370" not in captured[0][2] and "private.example" not in captured[0][2]
    planner.assert_not_awaited()
    stored.assert_not_awaited()
    with sqlite3.connect(router.audit_store.path) as connection:
        action, target, parameters, execution, delivery = connection.execute("""SELECT a.action_id,a.chat_id,
            a.parameter_summary,a.execution_result,d.status FROM owner_admin_audit a
            JOIN owner_admin_deliveries d ON a.audit_id=d.audit_id""").fetchone()
    assert action == "owner_admin.read.tokens" and target == group_id and execution == "succeeded"
    assert json.loads(parameters)["output_view"] == "public_view"
    assert delivery == ("confirmed" if status == "sent" else "unknown" if status == "cancelled" else "unconfirmed")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["reply", "forward"])
async def test_quoted_and_forwarded_commands_never_access_statistics(environment, monkeypatch, kind):
    from src.common.data_models.message_component_data_model import ForwardComponent, ForwardNodeComponent, ReplyComponent, TextComponent
    router, *_ = environment
    query = Mock(side_effect=AssertionError("不得执行引用或转发命令"))
    monkeypatch.setattr(OwnerReadService, "_query", query)
    message = incoming("/dfy tokens 1h")
    message.raw_message.components = ([ReplyComponent(target_message_id="owner-message", target_message_content="/dfy tokens 1h")]
        if kind == "reply" else [ForwardNodeComponent(forward_components=[ForwardComponent(
            user_id="100000001", user_nickname="主人", message_id="forwarded",
            content=[TextComponent("/dfy tokens 1h")])])])
    result = await router.handle_message(message)
    assert result.status == "rejected"
    query.assert_not_called()


def test_public_projection_never_reads_any_private_field():
    class NumbersOnly:
        total_requests = 1
        input_tokens = 5
        output_tokens = 2
        total_tokens = 7
        cache_hit_tokens = 0
        cache_miss_tokens = 0
        cache_hit_rate = None
        def __getattr__(self, name):
            raise AssertionError("公开投影读取了白名单外字段: " + name)
    assert "总Token=7" in OwnerReadService.public_view(NumbersOnly(), [])[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("window,requests", [("1h", 1), ("24h", 2), ("7d", 3)])
async def test_actual_interval_filters_records_like_webui(base_environment, window, requests):
    from src.services.statistics_service import get_summary_statistics
    router, _, db = base_environment
    now = datetime.now()
    with db() as session:
        for age in [timedelta(minutes=30), timedelta(hours=2), timedelta(days=2), timedelta(days=8)]:
            session.add(ModelUsage(model_name="fixture", model_api_provider_name="fixture", request_type="test",
                                   timestamp=now-age, time_cost=0.1, prompt_tokens=10, completion_tokens=2,
                                   total_tokens=12, cost=0.1))
        session.commit()
    start, end = query_window(window)
    expected = await get_summary_statistics(start.astimezone().replace(tzinfo=None), end.astimezone().replace(tzinfo=None))
    result = await router.handle_message(incoming("/dfy tokens " + window))
    assert expected.total_requests == requests
    assert f"记录请求={requests}" in result.message and f"总Token={requests*12}" in result.message


@pytest.mark.asyncio
async def test_private_natural_fees_and_audit_view_preserved(environment):
    router, *_ = environment
    result = await router.handle_message(incoming("大肥鱼，今天 API 花了多少钱？", group=False))
    assert result.success and "本地记录估算费用=370.370367" in result.message
    with sqlite3.connect(router.audit_store.path) as connection:
        parameters = json.loads(connection.execute("SELECT parameter_summary FROM owner_admin_audit").fetchone()[0])
    assert parameters["output_view"] == "owner_private_view"


@pytest.mark.asyncio
async def test_invalid_account_rejected_before_query(environment, monkeypatch):
    from src.services.bot_account_service import bot_account_service
    router, *_ = environment
    monkeypatch.setattr(bot_account_service, "is_bot_self", lambda *args: False)
    query = Mock(side_effect=AssertionError("未验证Bot不能查询"))
    monkeypatch.setattr(OwnerReadService, "_query", query)
    result = await router.handle_message(incoming("/dfy tokens"))
    assert result.error_code == "bot_account_unavailable"
    query.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["100000001", "stranger"])
async def test_invalid_natural_window_is_denied_and_audited(environment, user):
    router, *_ = environment
    result = await router.handle_message(incoming("管理：查询Token消耗 999d", user=user))
    assert not result.success and result.audit_id
