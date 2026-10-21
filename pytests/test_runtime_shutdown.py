"""QA-001：统一关闭、队列排空和进程退出前事务完成。"""

from contextlib import closing
from importlib import import_module
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import ast
import asyncio
import sqlite3
import subprocess
import sys

import pytest

from src.common import runtime_shutdown as lifecycle
from src.common import shutdown as state
from src.services.priority_at_reply_service import PriorityAtReplyService


@pytest.fixture
def shutdown_state(monkeypatch):
    monkeypatch.setattr(state, "_shutdown_requested", Event())
    monkeypatch.setattr(state, "_exit_code", 0)
    monkeypatch.setattr(lifecycle, "_shutdown_task", None)
    monkeypatch.setattr(lifecycle, "_system", None)
    monkeypatch.setattr(lifecycle, "_waiters", set())


@pytest.fixture
def runtime(monkeypatch, shutdown_state):
    order = []

    def operation(name):
        async def run(*args, **kwargs):
            order.append(name)

        return AsyncMock(side_effect=run)

    components = {}
    for module, name, method, label in [
        ("src.config.config", "config_manager", "stop_file_watcher", "config"),
        ("src.services.priority_at_reply_service", "priority_at_reply_service", "shutdown", "priority"),
        ("src.services.tag_extraction_service", "tag_extraction_service", "shutdown", "tag"),
        ("src.services.tag_memory_service", "tag_memory_service", "shutdown", "tag_cleanup"),
        ("src.services.owner_admin.overrides", "override_manager", "shutdown", "overrides"),
        ("src.chat.image_system.image_manager", "image_manager", "shutdown", "image"),
        ("src.services.memory_flow_service", "memory_automation_service", "shutdown", "memory"),
        ("src.A_memorix.host_service", "a_memorix_host_service", "stop", "kernel"),
        ("src.core.event_bus", "event_bus", "emit", "event"),
        ("src.manager.async_task_manager", "async_task_manager", "stop_and_wait_all_tasks", "tasks"),
    ]:
        components[module, name] = SimpleNamespace(**{method: operation(label)})
    components["src.core.types", "EventType"] = SimpleNamespace(ON_STOP="on_stop")
    components["src.chat.heart_flow.heartflow_manager", "heartflow_manager"] = SimpleNamespace(
        heartflow_chat_list={"group": SimpleNamespace(stop=operation("planner"))}
    )
    components["src.platform_io.manager", "get_platform_io_manager"] = lambda: SimpleNamespace(
        drain_inbound=operation("inbound")
    )
    components["src.plugin_runtime.integration", "get_plugin_runtime_manager"] = lambda: SimpleNamespace(
        stop=operation("plugin")
    )
    components["src.mcp_module.service", "get_mcp_service"] = lambda: SimpleNamespace(close=operation("mcp"))
    components["src.emoji_system.emoji_manager", "emoji_manager"] = SimpleNamespace(
        shutdown=lambda: order.append("emoji")
    )
    monkeypatch.setattr(lifecycle, "_component", lambda module, name: components[module, name])
    monkeypatch.setattr(lifecycle, "_cancel_remaining_tasks", operation("remaining"))
    monkeypatch.setattr(lifecycle, "_finish_io", operation("io"))
    system = SimpleNamespace(
        _runtime_task=None,
        _scheduled_tasks=[],
        watchdog_task=None,
        webui_server=SimpleNamespace(shutdown=operation("webui")),
    )
    lifecycle.register_main_system(system)
    return SimpleNamespace(system=system, components=components, order=order)


@pytest.mark.asyncio
async def test_shutdown_is_shared_and_orders_producers_before_consumers(runtime):
    await asyncio.gather(lifecycle.shutdown_runtime(), lifecycle.shutdown_runtime(runtime.system))
    order = runtime.order
    assert order.count("tag") == 1
    assert order.index("inbound") < order.index("planner") < order.index("priority")
    assert order.index("priority") < order.index("memory") < order.index("kernel")
    assert order[-2:] == ["remaining", "io"]
    assert state.is_shutdown_requested()
    runtime.components[
        "src.services.priority_at_reply_service", "priority_at_reply_service"
    ].shutdown.assert_awaited_once_with(drain=True)


@pytest.mark.asyncio
async def test_caller_cancellation_cannot_interrupt_database_cleanup(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    async def finish():
        entered.set()
        await release.wait()
        runtime.order.append("committed")

    runtime.components["src.services.tag_extraction_service", "tag_extraction_service"].shutdown.side_effect = finish
    caller = asyncio.create_task(lifecycle.shutdown_runtime())
    await entered.wait()
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert "committed" in runtime.order and runtime.order[-1] == "io"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("cleanup failed"), asyncio.CancelledError()])
async def test_cleanup_error_remains_visible_but_later_resources_close(runtime, error):
    runtime.components["src.services.tag_memory_service", "tag_memory_service"].shutdown.side_effect = error
    with pytest.raises(ExceptionGroup, match="运行时清理"):
        await lifecycle.shutdown_runtime()
    assert "memory" in runtime.order and "plugin" in runtime.order and runtime.order[-1] == "io"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "class_name,config_name",
    [
        ("PersonFactWritebackService", "person_fact_writeback_enabled"),
        ("ChatSummaryWritebackService", "chat_summary_writeback_enabled"),
    ],
)
async def test_memory_queue_drains_and_rejects_new_work(monkeypatch, class_name, config_name):
    module = import_module("src.services.memory_flow_service")
    monkeypatch.setattr(module.global_config.a_memorix.integration, config_name, True)
    service = getattr(module, class_name)()
    entered, release = asyncio.Event(), asyncio.Event()
    handled = []

    async def handle(message):
        entered.set()
        await release.wait()
        handled.append(message)

    monkeypatch.setattr(service, "_handle_message", handle)
    service._worker_task = asyncio.create_task(service._worker_loop())
    for message in ("first", "second", "third"):
        await service.enqueue(message)
    await entered.wait()
    shutdown_task = asyncio.create_task(service.shutdown())
    await asyncio.sleep(0)
    await service.enqueue("late")
    assert not shutdown_task.done()
    release.set()
    await asyncio.wait_for(shutdown_task, 3)
    assert handled == ["first", "second", "third"]
    assert service._queue.empty()
    await asyncio.wait_for(service._queue.join(), 1)


@pytest.mark.asyncio
async def test_dead_worker_reports_error_instead_of_hanging_join():
    queue = asyncio.Queue()
    queue.put_nowait("pending")

    async def failed():
        raise RuntimeError("worker failed")

    worker = asyncio.create_task(failed())
    with pytest.raises(RuntimeError, match="worker failed"):
        await asyncio.wait_for(state.drain_worker_queue(worker, queue), 1)


@pytest.mark.asyncio
async def test_priority_shutdown_drains_existing_queue_without_admitting_more(monkeypatch):
    service = PriorityAtReplyService(max_concurrent=1, max_per_session=1)
    release, entered = asyncio.Event(), asyncio.Event()
    completed = []

    async def reply(message, *args):
        entered.set()
        await release.wait()
        completed.append(message.message_id)

    monkeypatch.setattr(service, "_reply", reply)
    runtime = SimpleNamespace(_chat_history=[])
    for index in range(3):
        assert service.start_reply(SimpleNamespace(session_id="group", message_id=str(index), platform="qq",
            message_info=SimpleNamespace(additional_config={"account_id": "bot"})), runtime)
    await entered.wait()
    shutdown_task = asyncio.create_task(service.shutdown(drain=True))
    await asyncio.sleep(0)
    assert not service.start_reply(SimpleNamespace(session_id="group", message_id="late", platform="qq",
        message_info=SimpleNamespace(additional_config={"account_id": "bot"})), runtime)
    assert not shutdown_task.done()
    release.set()
    await asyncio.wait_for(shutdown_task, 3)
    assert completed == ["0", "1", "2"]
    assert not service._pending and not service._tasks


def function_from_source(path, name, namespace):
    node = next(
        node
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("force", [False, True])
def test_runner_requests_graceful_exit_before_waiting_or_explicit_second_interrupt(platform, force):
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [KeyboardInterrupt(), KeyboardInterrupt(), 0] if force else [KeyboardInterrupt(), 0]

    def exit_process(code):
        raise SystemExit(code)

    popen = Mock(return_value=process)
    namespace = dict(
        logger=Mock(),
        t=lambda key, **kw: key,
        os=SimpleNamespace(environ={}),
        time=SimpleNamespace(sleep=Mock()),
        sys=SimpleNamespace(platform=platform, argv=["bot.py"], executable="python", exit=exit_process),
        subprocess=SimpleNamespace(Popen=popen, CREATE_NEW_PROCESS_GROUP=16),
        signal=SimpleNamespace(CTRL_BREAK_EVENT=1, SIGINT=2),
        RESTART_EXIT_CODE=42,
    )
    runner = function_from_source(Path(__file__).resolve().parents[1] / "bot.py", "run_runner_process", namespace)
    with pytest.raises(SystemExit):
        runner()
    process.send_signal.assert_called_once_with(1 if platform == "win32" else 2)
    process.terminate.assert_not_called()
    assert process.kill.call_count == int(force)
    assert popen.call_args.kwargs["creationflags"] == (16 if platform == "win32" else 0)


@pytest.mark.asyncio
async def test_webui_restart_delegates_to_shared_shutdown(monkeypatch, shutdown_state):
    source = Path(__file__).resolve().parents[1] / "src/webui/routers/system.py"
    close = AsyncMock()
    monkeypatch.setattr(lifecycle, "shutdown_runtime", close)
    namespace = {"_RESTART_EXIT_CODE": 42}
    restart = function_from_source(source, "_stop_runtime_before_restart", namespace)
    await restart()
    close.assert_awaited_once()
    assert state.shutdown_exit_code() == 42


@pytest.mark.asyncio
async def test_quiescing_inbound_waits_for_accepted_dispatch(monkeypatch, shutdown_state):
    from src.platform_io.manager import PlatformIOManager
    from src.platform_io.types import DriverKind, InboundMessageEnvelope, RouteKey

    manager = PlatformIOManager()
    monkeypatch.setattr(manager._receive_route_table, "has_binding_for_driver", lambda *args: True)
    release, entered = asyncio.Event(), asyncio.Event()

    async def dispatch(envelope):
        entered.set()
        await release.wait()

    manager.set_inbound_dispatcher(dispatch)
    envelope = InboundMessageEnvelope(RouteKey("test"), "driver", DriverKind.PLUGIN, external_message_id="one")
    assert await manager.accept_inbound(envelope)
    await entered.wait()
    state.request_shutdown()
    assert not await manager.accept_inbound(envelope)
    shutdown_task = asyncio.create_task(manager.drain_inbound())
    await asyncio.sleep(0)
    assert not shutdown_task.done()
    release.set()
    await asyncio.wait_for(shutdown_task, 1)


@pytest.mark.asyncio
async def test_worker_entry_delegates_to_shared_shutdown():
    close = AsyncMock()
    owner = object()
    namespace = {"MainSystem": object, "shutdown_runtime": close}
    worker_shutdown = function_from_source(
        Path(__file__).resolve().parents[1] / "bot.py", "graceful_shutdown", namespace
    )
    await worker_shutdown(owner)
    close.assert_awaited_once_with(owner)


@pytest.mark.asyncio
async def test_replay_close_waits_for_daemon_batch_and_joins_worker(tmp_path):
    from src.decision_replay.store import ReplayStore

    store = ReplayStore(tmp_path / "replay.db")
    entered, release = Event(), Event()
    original = store.write_batch

    def delayed(events):
        entered.set()
        assert release.wait(3)
        original(events)

    store.write_batch = delayed
    store.submit(
        dict(
            event_id="event",
            timestamp=1700000000.0,
            session_id="group",
            logical_turn_id="turn",
            source="test",
            cycle_id=None,
            request_id=None,
            message_ids=[],
            stage="gate",
            event_type="gate.decided",
            decision="accept",
            reason_code="test",
            detail={},
            duration_ms=None,
            schema_version=1,
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)
    shutdown_task = asyncio.create_task(asyncio.to_thread(store.close))
    await asyncio.sleep(0)
    assert not shutdown_task.done()
    release.set()
    await asyncio.wait_for(shutdown_task, 3)
    assert not store.worker.is_alive()
    assert store.queue.unfinished_tasks == 0
    assert store.errors == 0


def test_process_exit_after_shared_shutdown_preserves_thread_transaction_and_replay(tmp_path):
    # 独立进程使用真实 coordinator、executor、SQLite 和 writer；其余外部组件为无副作用 stub。
    script = tmp_path / "shutdown_child.py"
    script.write_text(
        r"""
from pathlib import Path
from types import SimpleNamespace
from threading import Event, Timer
from contextlib import closing
import asyncio, json, os, sqlite3, sys, time
root=Path(sys.argv[1]);data=Path(sys.argv[2]);sys.path.insert(0,str(root))
sys.modules['src.common.logger']=SimpleNamespace(get_logger=lambda *a:SimpleNamespace(info=lambda *a:None,exception=lambda *a:None))
from src.common import runtime_shutdown as lifecycle
from src.common.shutdown import request_shutdown,shutdown_exit_code
from src.decision_replay.store import ReplayStore
async def noop(*args,**kwargs):pass
store=ReplayStore(data/'replay.db')
def component(module,name):
    if name=='store':return store
    if name=='EventType':return SimpleNamespace(ON_STOP='on_stop')
    if name=='heartflow_manager':return SimpleNamespace(heartflow_chat_list={})
    value=SimpleNamespace(stop_file_watcher=noop,shutdown=noop,stop=noop,close=noop,
                          stop_and_wait_all_tasks=noop,drain_inbound=noop,emit=noop)
    return (lambda:value) if name.startswith('get_') else value
lifecycle._component=component
async def run():
    db=data/'transaction.db'
    with closing(sqlite3.connect(db)) as c, c:c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE probe(value TEXT)')
    entered,release=Event(),Event()
    def write():
        with closing(sqlite3.connect(db)) as c, c:
            c.execute("INSERT INTO probe VALUES('committed')")
            entered.set()
            assert release.wait(5)
    task=asyncio.create_task(asyncio.to_thread(write))
    assert await asyncio.to_thread(entered.wait,2)
    store.submit(dict(event_id='event',timestamp=time.time(),session_id='group',logical_turn_id='turn',source='test',
        cycle_id=None,request_id=None,message_ids=[],stage='gate',event_type='gate.decided',decision='accept',
        reason_code='test',detail={},duration_ms=None,schema_version=1))
    system=SimpleNamespace(_runtime_task=asyncio.current_task(),_scheduled_tasks=[],watchdog_task=None,webui_server=None)
    lifecycle.register_main_system(system)
    request_shutdown('restart',exit_code=42)
    Timer(0.2,release.set).start()
    await lifecycle.shutdown_runtime()
    assert task.cancelled()
    assert not store.worker.is_alive()
    assert store.queue.unfinished_tasks==0
    assert shutdown_exit_code()==42
asyncio.run(run())
os._exit(42)
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-B", str(script), str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 42, result.stderr
    with closing(sqlite3.connect(tmp_path / "transaction.db")) as connection:
        assert connection.execute("SELECT value FROM probe").fetchall() == [("committed",)]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with closing(sqlite3.connect(tmp_path / "replay.db")) as connection:
        assert connection.execute("SELECT event_id FROM events").fetchall() == [("event",)]


@pytest.mark.asyncio
async def test_webui_thread_waits_for_its_sqlite_executor(monkeypatch, tmp_path):
    from src.webui import webui_server as module

    entered, release = Event(), Event()
    database = tmp_path / "webui.db"

    def write():
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE probe(value TEXT)")
            connection.execute("INSERT INTO probe VALUES('saved')")
            entered.set()
            assert release.wait(3)

    class Server:
        def __init__(self, **kwargs):
            self.exit = None

        async def start(self):
            self.exit = asyncio.Event()
            self.worker = asyncio.create_task(asyncio.to_thread(write))
            await self.exit.wait()

        async def shutdown(self, timeout=None):
            self.exit.set()

    monkeypatch.setattr(module, "WebUIServer", Server)
    server = module.ThreadedWebUIServer(hosts=["127.0.0.1"], port=7999)
    monkeypatch.setattr(server, "_maybe_register_reload_callback", Mock())
    monkeypatch.setattr(server, "_maybe_unregister_reload_callback", Mock())
    await asyncio.to_thread(server.start)
    assert await asyncio.to_thread(entered.wait, 2)
    shutdown_task = asyncio.create_task(server.shutdown(timeout=None))
    try:
        await asyncio.sleep(0)
        assert not shutdown_task.done()
        release.set()
        await asyncio.wait_for(shutdown_task, 3)
        assert server._thread is None
        with closing(sqlite3.connect(database)) as connection, connection:
            assert connection.execute("SELECT value FROM probe").fetchall() == [("saved",)]
    finally:
        release.set()
        await shutdown_task


@pytest.mark.asyncio
async def test_one_memory_cleanup_failure_does_not_skip_other_queues():
    from src.services.memory_flow_service import MemoryAutomationService

    service = MemoryAutomationService()
    service._started = True
    service.image_writeback = SimpleNamespace(shutdown=AsyncMock(side_effect=RuntimeError("image cleanup failed")))
    service.chat_summary_writeback = SimpleNamespace(shutdown=AsyncMock())
    service.fact_writeback = SimpleNamespace(shutdown=AsyncMock())
    with pytest.raises(ExceptionGroup, match="记忆写回"):
        await service.shutdown()
    service.chat_summary_writeback.shutdown.assert_awaited_once()
    service.fact_writeback.shutdown.assert_awaited_once()
    assert not service._started


@pytest.mark.asyncio
async def test_webui_loop_cancellation_during_restart_does_not_cancel_main_cleanup(runtime, monkeypatch):
    from src.common import runtime_loop
    from src.webui import webui_server as module

    previous = runtime_loop.get_main_loop()
    runtime_loop.set_main_loop(asyncio.get_running_loop())
    entered = Event()

    class Server:
        def __init__(self, **kwargs):
            self.exit = None

        async def start(self):
            self.exit = asyncio.Event()

            async def restart():
                entered.set()
                await runtime_loop.run_on_main_loop(lifecycle.shutdown_runtime())

            self.restart = asyncio.create_task(restart())
            await self.exit.wait()

        async def shutdown(self, timeout=None):
            self.exit.set()

    monkeypatch.setattr(module, "WebUIServer", Server)
    server = module.ThreadedWebUIServer(hosts=["127.0.0.1"], port=7999)
    monkeypatch.setattr(server, "_maybe_register_reload_callback", Mock())
    monkeypatch.setattr(server, "_maybe_unregister_reload_callback", Mock())
    runtime.system.webui_server = server
    try:
        await asyncio.to_thread(server.start)
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.shield(lifecycle._shutdown_task), 3)
        assert server._thread is None
        assert runtime.order.count("tag") == 1
        assert runtime.order[-1] == "io"
    finally:
        runtime_loop.set_main_loop(previous)


@pytest.mark.asyncio
async def test_restart_cancels_root_without_deadlocking_its_finally(runtime):
    entered = asyncio.Event()

    async def root():
        runtime.system._runtime_task = asyncio.current_task()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await lifecycle.shutdown_runtime(runtime.system)

    owner = asyncio.create_task(root())
    await entered.wait()
    await asyncio.wait_for(lifecycle.shutdown_runtime(), 2)
    result = await asyncio.gather(owner, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert runtime.order.count("tag") == 1
    assert runtime.order[-1] == "io"
