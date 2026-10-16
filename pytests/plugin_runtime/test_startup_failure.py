"""QA-023：运行时启动异常回滚后向调用方传播，配置跳过保持正常返回。"""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio

import pytest

from src.plugin_runtime.integration import PluginRuntimeManager

module = import_module("src.plugin_runtime.integration")


@pytest.fixture
def runtime(monkeypatch):
    manager = PluginRuntimeManager()
    config = SimpleNamespace(plugin_runtime=SimpleNamespace(enabled=True))
    monkeypatch.setattr(module.config_manager, "get_global_config", lambda: config)
    register, unregister = Mock(), Mock()
    monkeypatch.setattr(module.config_manager, "register_reload_callback", register)
    monkeypatch.setattr(module.config_manager, "unregister_reload_callback", unregister)
    broker = SimpleNamespace(set_inbound_dispatcher=Mock(), clear_inbound_dispatcher=Mock(),
                             ensure_send_pipeline_ready=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(module, "get_platform_io_manager", lambda: broker)
    supervisor = SimpleNamespace(group_name="builtin", stop=AsyncMock())
    monkeypatch.setattr(manager, "_resolve_runtime_plugin_dirs", lambda: ([Path("builtin")], []))
    monkeypatch.setattr(manager, "_cleanup_plugin_load_residue_dirs", Mock())
    monkeypatch.setattr(manager, "_sync_plugin_dependencies", AsyncMock(
        return_value=SimpleNamespace(environment_changed=False)))
    monkeypatch.setattr(manager, "ensure_builtin_hook_specs_registered", Mock())
    monkeypatch.setattr(manager, "_build_supervisors", Mock(
        side_effect=lambda *args: setattr(manager, "_builtin_supervisor", supervisor)))
    monkeypatch.setattr(manager, "_start_supervisors", AsyncMock(return_value=[supervisor]))
    monkeypatch.setattr(manager, "_start_plugin_file_watcher", AsyncMock())
    monkeypatch.setattr(manager, "_stop_plugin_file_watcher", AsyncMock())
    monkeypatch.setattr(manager._hook_dispatcher, "stop", AsyncMock())
    notified = Mock()
    manager.subscribe_status_changes(notified)
    return SimpleNamespace(manager=manager, broker=broker, supervisor=supervisor,
                           register=register, unregister=unregister, notified=notified, config=config)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["pipeline", "supervisors", "watcher", "callback"])
async def test_start_failure_rolls_back_and_propagates_original(runtime, phase):
    manager = runtime.manager
    operation = {
        "pipeline": runtime.broker.ensure_send_pipeline_ready,
        "supervisors": manager._start_supervisors,
        "watcher": manager._start_plugin_file_watcher,
        "callback": runtime.register,
    }[phase]
    error = RuntimeError("injected start failure")
    operation.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        await manager.start()
    assert caught.value is error
    assert not manager.is_running
    assert not manager.supervisors
    runtime.broker.clear_inbound_dispatcher.assert_called_once()
    runtime.broker.stop.assert_awaited_once()
    runtime.supervisor.stop.assert_awaited_once()
    manager._hook_dispatcher.stop.assert_awaited_once()
    runtime.notified.assert_called_once()
    # 故障消除后同一个管理器仍可启动，不保留失败时的资源状态。
    operation.side_effect = None
    await manager.start()
    assert manager.is_running
    runtime.register.assert_called()
    calls = manager._start_supervisors.await_count
    await manager.start()
    assert manager._start_supervisors.await_count == calls


@pytest.mark.asyncio
async def test_rollback_failure_does_not_hide_start_error_or_skip_other_cleanup(runtime):
    manager = runtime.manager
    error = RuntimeError("start failure")
    manager._start_supervisors.side_effect = error
    manager._stop_plugin_file_watcher.side_effect = RuntimeError("cleanup failure")
    with pytest.raises(RuntimeError) as caught:
        await manager.start()
    assert caught.value is error
    runtime.supervisor.stop.assert_awaited_once()
    runtime.broker.stop.assert_awaited_once()
    assert not manager.is_running
    runtime.notified.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [True, False])
async def test_explicit_disabled_or_no_directories_is_normal_skip(runtime, monkeypatch, disabled):
    if disabled:
        runtime.config.plugin_runtime.enabled = False
    else:
        monkeypatch.setattr(runtime.manager, "_resolve_runtime_plugin_dirs", lambda: ([], []))
    await runtime.manager.start()
    assert not runtime.manager.is_running
    runtime.manager._start_supervisors.assert_not_awaited()
    runtime.broker.ensure_send_pipeline_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_start_rolls_back_and_propagates_cancellation(runtime):
    runtime.manager._start_supervisors.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await runtime.manager.start()
    assert not runtime.manager.is_running
    runtime.supervisor.stop.assert_awaited_once()
    runtime.broker.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_after_callback_registration_unregisters_and_keeps_original_error(runtime):
    error = RuntimeError("status listener failed")

    def fail_status():
        raise error

    runtime.manager.subscribe_status_changes(fail_status)
    with pytest.raises(RuntimeError) as caught:
        await runtime.manager.start()
    assert caught.value is error
    runtime.unregister.assert_called_once_with(runtime.manager._config_reload_callback)
    assert not runtime.manager._config_reload_callback_registered
    assert not runtime.manager.is_running
    runtime.supervisor.stop.assert_awaited_once()
    runtime.broker.stop.assert_awaited_once()
