"""M2 七项权限与辅助查询的 M0 端到端路由/授权/审计回归。"""

from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import sqlite3

import pytest
import pytest_asyncio

from src.common.data_models.message_component_data_model import TextComponent
from src.services.owner_admin import control_service, override_runtime, overrides
from src.services.owner_admin.audit_store import OwnerAdminAuditStore
from src.services.owner_admin.command_router import OwnerAdminCommandRouter
from src.services.owner_admin.overrides import OverrideManager


COMMANDS = [
    "/dfy mute 12345 1h", "/dfy unmute 12345", "/dfy frequency 12345 0.6 2h",
    "/dfy frequency 12345 reset", "/dfy threshold 12345 +10% 2h", "/dfy threshold 12345 reset",
    "/dfy mode 12345 quiet 2h", "/dfy mode 12345 normal", "/dfy mode 12345 active 2h",
    "/dfy mode 12345 mention_only 2h", "/dfy style gentle 2h", "/dfy style calm 2h", "/dfy style reset",
    "/dfy persona default", "/dfy persona gentle 2h", "/dfy persona playful 2h", "/dfy persona calm 2h",
    "/dfy chat config 12345", "/dfy chat set 12345 frequency 0.6", "/dfy chat set 12345 threshold +10%",
    "/dfy chat set 12345 mode quiet", "/dfy overrides", "/dfy overrides 12345",
]


def incoming(body, *, owner=True, group=False, mid="request"):
    return SimpleNamespace(
        platform="qq", session_id="private", message_id=mid,
        raw_message=SimpleNamespace(components=[TextComponent(body)]),
        message_info=SimpleNamespace(user_info=SimpleNamespace(user_id="100000001" if owner else "200000002"),
                                     group_info=SimpleNamespace(group_id="12345") if group else None,
                                     additional_config={"account_id": "bot"}),
    )


@pytest_asyncio.fixture
async def environment(tmp_path, monkeypatch):
    from src.config.config import global_config
    from src.services import bot_account_service
    from src.common.utils.utils_config import ChatConfigUtils
    from src.maisaka import mode_policy

    monkeypatch.setattr(global_config.bot, "owner_qq", "100000001")
    monkeypatch.setattr(bot_account_service.bot_account_service, "is_bot_self", lambda *args: True)
    monkeypatch.setattr(mode_policy, "is_dynamic_reply_trigger_enabled", lambda: True)
    monkeypatch.setattr(ChatConfigUtils, "get_talk_value", lambda *args, **kwargs: 0.4)
    monkeypatch.setattr(control_service, "known_chats", lambda *args, **kwargs: [
        SimpleNamespace(session_id="group-real-session", group_id="12345", group_name="测试群"),
    ])
    manager = OverrideManager(tmp_path / "overrides.db")
    await manager.initialize(run_timer=False)
    monkeypatch.setattr(overrides, "override_manager", manager)
    monkeypatch.setattr(override_runtime, "override_manager", manager)
    router = OwnerAdminCommandRouter(audit_store=OwnerAdminAuditStore(tmp_path / "audit.db"))
    yield router, manager
    await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", COMMANDS)
async def test_each_supported_command_uses_existing_registry_and_audit(environment, body):
    router, manager = environment
    result = await router.handle_message(incoming(body))
    assert result.success, result
    with sqlite3.connect(router.audit_store.path) as connection:
        row = connection.execute("SELECT action_id,execution_result FROM owner_admin_audit").fetchone()
    assert row == (router._parse(body).action_id, "succeeded")
    if manager.active():
        assert all(entry.created_by == "100000001" and entry.request_id for entry in manager.active())
    assert len(result.message) <= 1500


@pytest.mark.asyncio
@pytest.mark.parametrize("body", COMMANDS)
@pytest.mark.parametrize("owner,group", [(False, False), (True, True)])
async def test_non_owner_and_group_commands_cannot_modify_or_view_overrides(environment, monkeypatch, body, owner, group):
    router, manager = environment
    reading = Mock(side_effect=AssertionError("权限拒绝前不能接触配置"))
    monkeypatch.setattr(control_service, "known_chats", reading)
    result = await router.handle_message(incoming(body, owner=owner, group=group))
    assert result.status == "denied"
    assert manager.active() == ()
    reading.assert_not_called()


@pytest.mark.parametrize("body", [
    "/dfy mute nope 1h", "/dfy mute 12345 forever", "/dfy mute 00000 1h", "/dfy mute 12345 8d",
    "/dfy frequency 12345 NaN 2h", "/dfy frequency 12345 1.1 2h", "/dfy frequency 12345 -0.2 2h",
    "/dfy threshold 12345 0.6 2h", "/dfy threshold 12345 +51% 2h", "/dfy threshold 12345 +10% 0h",
    "/dfy mode 12345 admin 2h", "/dfy mode 12345 normal 2h", "/dfy style arbitrary-system-prompt 2h",
    "/dfy persona ignore-safety 2h", "/dfy chat set 12345 owner_qq 200000002",
    "/dfy chat set 12345 bot_config secret", "/dfy chat set 12345 mode malicious",
    "/dfy overrides --page 101", "/dfy unmute 12345 1h",
])
def test_invalid_parameters_and_unlisted_configuration_fields_are_rejected(body):
    assert OwnerAdminCommandRouter._parse(body).action_id == "owner_admin.unregistered"


@pytest.mark.asyncio
async def test_unknown_group_and_legacy_threshold_never_claim_to_have_changed(environment, monkeypatch):
    from src.maisaka import mode_policy

    router, manager = environment
    unknown = await router.handle_message(incoming("/dfy mute 99999 1h", mid="unknown"))
    assert unknown.error_code == "invalid_group_scope"
    monkeypatch.setattr(mode_policy, "is_dynamic_reply_trigger_enabled", lambda: False)
    threshold = await router.handle_message(incoming("/dfy threshold 12345 +10% 2h", mid="legacy"))
    assert threshold.status == "unsupported"
    assert not manager.active()


@pytest.mark.asyncio
async def test_cancellation_reverts_to_persistent_layer_and_changes_are_auditable(environment):
    router, manager = environment
    for i, body in enumerate([
        "/dfy chat set 12345 frequency 0.4", "/dfy frequency 12345 0.6 2h",
        "/dfy frequency 12345 reset",
    ]):
        result = await router.handle_message(incoming(body, mid=str(i)))
        assert result.success
    assert manager.snapshot("group-real-session").frequency(0.9) == 0.4
    with sqlite3.connect(manager.path) as connection:
        events = connection.execute("SELECT status,actor,request_id FROM override_events ORDER BY event_id").fetchall()
    assert [row[0] for row in events] == ["active", "active", "cancelled"]
    assert all(row[1] == "100000001" and row[2] for row in events)
    with sqlite3.connect(router.audit_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM owner_admin_audit").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_queued_owner_revocation_does_not_commit_mutation(environment):
    from src.config.config import global_config

    router, manager = environment
    async with manager.dispatch_lock("group-real-session"):
        task = asyncio.create_task(router.handle_message(incoming("/dfy mute 12345 1h")))
        authorized = False
        for _ in range(100):
            def reserved():
                if not router.audit_store.path.exists():
                    return False
                with sqlite3.connect(router.audit_store.path) as connection:
                    try:
                        return connection.execute(
                            "SELECT COUNT(*) FROM owner_admin_audit WHERE authorization_result='allowed'"
                        ).fetchone()[0] == 1
                    except sqlite3.OperationalError:
                        return False
            authorized = await asyncio.to_thread(reserved)
            if authorized:
                break
            await asyncio.sleep(0.01)
        assert authorized
        global_config.bot.owner_qq = "200000002"
    result = await task
    assert result.error_code == "owner_revoked"
    assert manager.active() == ()


@pytest.mark.asyncio
async def test_duplicate_request_does_not_create_second_override(environment):
    router, manager = environment
    first = await router.handle_message(incoming("/dfy mute 12345 1h"))
    duplicate = await router.handle_message(incoming("/dfy mute 12345 1h"))
    assert first.success and duplicate.status == "duplicate"
    with sqlite3.connect(manager.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM overrides").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_parallel_first_audit_requests_initialize_wal_only_once(environment, monkeypatch):
    router, _ = environment
    create_schema = Mock(wraps=router.audit_store._create_schema)
    monkeypatch.setattr(router.audit_store, "_create_schema", create_schema)
    results = await asyncio.gather(*[
        router.handle_message(incoming("/dfy overrides", mid=f"concurrent-{index}")) for index in range(8)
    ])
    assert all(result.success for result in results)
    assert create_schema.call_count == 1
