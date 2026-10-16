"""启动方必须收到 QA-003/QA-023 失败，不能继续启动下游服务。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import sys

import pytest

from src import main


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_component", ["sessions", "plugins"])
async def test_main_initialize_does_not_report_success_after_component_failure(monkeypatch, failed_component):
    error = RuntimeError("injected startup failure")
    plugins = SimpleNamespace(start=AsyncMock(), supervisors=[])
    sessions = SimpleNamespace(initialize=AsyncMock(), regularly_save_sessions=AsyncMock())
    memory = SimpleNamespace(start=AsyncMock())
    tags = SimpleNamespace(start=AsyncMock())
    extraction = SimpleNamespace(start=AsyncMock())
    host = SimpleNamespace(start=AsyncMock(), register_config_reload_callback=Mock())
    mcp = SimpleNamespace(register_config_reload_callback=Mock())
    monkeypatch.setitem(sys.modules, "src.plugin_runtime.integration", SimpleNamespace(
        get_plugin_runtime_manager=lambda: plugins))
    monkeypatch.setitem(sys.modules, "src.chat.message_receive.chat_manager", SimpleNamespace(chat_manager=sessions))
    monkeypatch.setitem(sys.modules, "src.A_memorix.host_service", SimpleNamespace(a_memorix_host_service=host))
    monkeypatch.setitem(sys.modules, "src.mcp_module.service", SimpleNamespace(get_mcp_service=lambda: mcp))
    monkeypatch.setitem(sys.modules, "src.emoji_system.emoji_manager", SimpleNamespace(
        emoji_manager=SimpleNamespace(load_emojis_from_db=Mock())))
    monkeypatch.setitem(sys.modules, "src.services.memory_flow_service", SimpleNamespace(memory_automation_service=memory))
    monkeypatch.setitem(sys.modules, "src.services.tag_memory_service", SimpleNamespace(tag_memory_service=tags))
    monkeypatch.setitem(sys.modules, "src.services.tag_extraction_service", SimpleNamespace(tag_extraction_service=extraction))
    monkeypatch.setitem(sys.modules, "src.services.tool_record_cleanup_service", SimpleNamespace(
        run_startup_tool_record_vacuum_if_needed=Mock()))
    monkeypatch.setattr(main.config_manager, "start_file_watcher", AsyncMock())
    monkeypatch.setattr(main.prompt_manager, "load_prompts", Mock())
    if failed_component == "sessions":
        sessions.initialize.side_effect = error
    else:
        plugins.start.side_effect = error
    system = main.MainSystem()
    monkeypatch.setattr(system, "_start_webui_server", Mock())
    with pytest.raises(RuntimeError) as caught:
        await system.initialize()
    assert caught.value is error
    sessions.regularly_save_sessions.assert_not_awaited()
    memory.start.assert_not_awaited()
    tags.start.assert_not_awaited()
    extraction.start.assert_not_awaited()
    system._start_webui_server.assert_not_called()
    assert system.watchdog_task is None
