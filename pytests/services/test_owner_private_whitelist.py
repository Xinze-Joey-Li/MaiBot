"""主人私聊白名单增量：真实策略文件、路由表、M0授权及确认审计。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import json
import sqlite3

import pytest
import pytest_asyncio

from pytests.services import test_owner_admin_m3 as m3
from src.platform_io import adapter_policy
from src.platform_io.adapter_policy import AdapterIdentity, AdapterPolicyManager
from src.platform_io.routing import RouteTable
from src.platform_io.types import DriverDescriptor, DriverKind, RouteBinding, RouteKey
from src.services.owner_admin import access_policy_service, memory_access_service

base_environment = m3.environment


@pytest_asyncio.fixture
async def environment(base_environment, monkeypatch, tmp_path):
    from src import platform_io

    router, _, _ = base_environment
    policy = AdapterPolicyManager(tmp_path / "policy.toml")
    monkeypatch.setattr(adapter_policy, "get_adapter_policy_manager", lambda: policy)
    monkeypatch.setattr(access_policy_service, "get_adapter_policy_manager", lambda: policy)
    policy.set_default_action("group", "block")
    policy.set_default_action("private", "block")
    identity = AdapterIdentity("qq-fixture", "fixture.adapter", "gateway", "qq", "bot", None)
    policy.set_chat_override(identity, chat_type="private", target_id=m3.OWNER, action="allow", exact_identity=True, verify=True)
    chats = memory_access_service.known_chats("bot")
    for chat in chats:
        chat.platform, chat.account_id, chat.scope = "qq", "bot", None
    routes = RouteTable()
    routes.bind(RouteBinding(RouteKey("qq", "bot"), identity.adapter_id, DriverKind.PLUGIN))
    driver = SimpleNamespace(descriptor=DriverDescriptor(identity.adapter_id, DriverKind.PLUGIN, "qq", "bot",
                    plugin_id=identity.plugin_id, metadata={"plugin_type": "adapter", "gateway_name": "gateway"}))
    drivers = [driver]
    monkeypatch.setattr(platform_io, "get_platform_io_manager", lambda: SimpleNamespace(
        driver_registry=SimpleNamespace(list=lambda **kwargs: drivers), receive_route_table=routes))
    return SimpleNamespace(router=router, policy=policy, identity=identity, driver=driver, drivers=drivers, routes=routes)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "private"])
async def test_owner_private_can_add_new_target_without_history(environment, kind):
    env = environment
    before_other = env.policy.evaluate(env.identity, chat_type=kind, target_id="88888")
    preview = await m3.send(env.router, f"/dfy access allow {kind} 99999")
    assert preview.status == "preview", preview
    assert not env.policy.evaluate(env.identity, chat_type=kind, target_id="99999").allowed
    result = await m3.confirm(env.router, preview)
    assert result.success, result
    assert env.policy.evaluate(env.identity, chat_type=kind, target_id="99999").allowed
    assert AdapterPolicyManager(env.policy.policy_path).evaluate(env.identity, chat_type=kind, target_id="99999").allowed
    assert env.policy.evaluate(env.identity, chat_type=kind, target_id="88888") == before_other
    other_kind = "private" if kind == "group" else "group"
    assert not env.policy.evaluate(env.identity, chat_type=other_kind, target_id="99999").allowed
    assert not env.policy.evaluate(replace(env.identity, account_id="other-bot"), chat_type=kind, target_id="99999").allowed
    assert env.policy.evaluate(env.identity, chat_type="private", target_id=m3.OWNER).allowed
    with sqlite3.connect(env.router.audit_store.path) as connection:
        rows = connection.execute("SELECT authorization_result,execution_result,parameter_summary FROM owner_admin_audit").fetchall()
    assert rows[-1][:2] == ("allowed", "succeeded")
    parameters = json.loads(rows[-1][2])
    assert parameters["chat_type"] == kind and parameters["target_id"] == "99999"
    assert not (await m3.confirm(env.router, preview)).success


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize("user,group", [(m3.USER, False), (m3.OWNER, True), (m3.USER, True)])
async def test_only_owner_and_only_private_before_any_policy_access(environment, monkeypatch, kind, user, group):
    env = environment
    before = env.policy.policy_path.read_bytes()
    blocked = Mock(side_effect=AssertionError("未经授权不能查接入目标"))
    monkeypatch.setattr(memory_access_service, "known_chats", blocked)
    result = await m3.send(env.router, f"/dfy access allow {kind} 99999", user=user, group=group)
    assert result.status == "denied" and result.audit_id
    assert env.policy.policy_path.read_bytes() == before
    blocked.assert_not_called()


@pytest.mark.asyncio
async def test_confirmation_cannot_be_used_in_group_or_by_whitelisted_friend(environment):
    env = environment
    preview = await m3.send(env.router, "/dfy access allow private 99999")
    assert not (await m3.confirm(env.router, preview, group=True)).success
    assert not (await m3.confirm(env.router, preview, user=m3.USER)).success
    assert (await m3.confirm(env.router, preview)).success
    # 接入白名单不授予管理权限。
    result = await m3.send(env.router, "/dfy access allow group 88888", user="99999")
    assert result.status == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["policy", "identity", "route", "owner"])
async def test_changed_preconditions_reject_confirmation_without_write(environment, monkeypatch, change):
    env = environment
    preview = await m3.send(env.router, "/dfy access allow private 99999")
    if change == "policy":
        env.policy.set_chat_override(env.identity, chat_type="group", target_id="77777", action="allow")
    elif change == "identity":
        env.driver.descriptor = replace(env.driver.descriptor, metadata={"plugin_type": "adapter", "gateway_name": "changed"})
    elif change == "route":
        env.routes.unbind(RouteKey("qq", "bot"))
    else:
        from src.config.config import global_config
        monkeypatch.setattr(global_config.bot, "owner_qq", m3.USER)
    before = env.policy.policy_path.read_bytes()
    result = await m3.confirm(env.router, preview)
    assert not result.success
    assert env.policy.policy_path.read_bytes() == before


@pytest.mark.asyncio
async def test_ambiguous_adapter_and_missing_source_cannot_add(environment, monkeypatch):
    env = environment
    second = SimpleNamespace(descriptor=replace(env.driver.descriptor, driver_id="other-driver"))
    env.drivers.append(second)
    env.routes.bind(RouteBinding(RouteKey("qq", "bot"), "other-driver", DriverKind.PLUGIN))
    assert not (await m3.send(env.router, "/dfy access allow group 99999")).success
    monkeypatch.setattr(memory_access_service, "known_chats", lambda *args, **kwargs: [])
    assert not (await m3.send(env.router, "/dfy access allow private 99999")).success


@pytest.mark.parametrize("body", ["/dfy access allow private *", "/dfy access allow group -12345",
                                  "/dfy access allow private owner", "/dfy access allow private 123",
                                  "/dfy access deny private 99999", "/dfy access allow all 99999",
                                  "/dfy access allow private 99999 --account other"])
def test_invalid_scope_or_ids_not_registered(body):
    from src.services.owner_admin.command_router import OwnerAdminCommandRouter
    assert OwnerAdminCommandRouter._parse(body).action_id == "owner_admin.unregistered"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,label", [("group", "群聊"), ("private", "私聊")])
async def test_natural_language_uses_same_private_guard_and_confirmation(environment, kind, label):
    env = environment
    body = f"管理：添加{label}白名单 99999"
    denied = await m3.send(env.router, body, group=True)
    assert denied.status == "denied"
    preview = await m3.send(env.router, body)
    assert preview.status == "preview", preview
    assert (await m3.confirm(env.router, preview)).success
    assert env.policy.evaluate(env.identity, chat_type=kind, target_id="99999").allowed


@pytest.mark.asyncio
async def test_repeat_allow_is_idempotent_and_disabled_adapter_stays_disabled(environment):
    import tomlkit
    env = environment
    for _ in range(2):
        preview = await m3.send(env.router, "/dfy access allow private 99999")
        assert (await m3.confirm(env.router, preview)).success
    data = tomlkit.parse(env.policy.policy_path.read_text(encoding="utf-8"))
    assert data["adapters"][0]["private"]["allow_ids"].count("99999") == 1
    data["adapters"][0]["disabled"] = True
    env.policy.policy_path.write_text(tomlkit.dumps(data), encoding="utf-8")
    preview = await m3.send(env.router, "/dfy access allow group 88888")
    before = env.policy.policy_path.read_bytes()
    assert not (await m3.confirm(env.router, preview)).success
    assert env.policy.policy_path.read_bytes() == before
