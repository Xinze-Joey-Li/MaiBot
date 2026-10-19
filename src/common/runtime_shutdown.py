"""主循环上的统一关停：先停止生产，再排空内存任务并等待数据库线程。"""

from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Set

import asyncio
import inspect

from src.common.logger import get_logger
from src.common.shutdown import request_shutdown

if TYPE_CHECKING:
    from src.main import MainSystem

logger = get_logger("runtime_shutdown")
_system: Optional["MainSystem"] = None
_shutdown_task: Optional[asyncio.Task] = None
_waiters: Set[asyncio.Task] = set()


def register_main_system(system: "MainSystem") -> None:
    global _system, _shutdown_task
    _system = system
    _shutdown_task = None


def _component(module: str, name: str) -> Any:
    # 仅在关停步骤执行时加载对应组件，启动失败时也能逐项尝试清理。
    return getattr(import_module(module), name)


async def shutdown_runtime(system: Optional["MainSystem"] = None) -> None:
    """所有入口复用同一任务；取消调用方也不能中断已经开始的事务清理。"""
    global _shutdown_task
    owner = system if system is not None else _system
    if owner is None:
        raise RuntimeError("主运行时尚未注册，不能执行进程关停")
    current = asyncio.current_task()
    _waiters.add(current)
    try:
        if _shutdown_task is None:
            _shutdown_task = asyncio.create_task(_shutdown(owner), name="runtime_shutdown")
        try:
            await asyncio.shield(_shutdown_task)
        except asyncio.CancelledError:
            await asyncio.shield(_shutdown_task)
            raise
    finally:
        _waiters.discard(current)


async def _cancel_remaining_tasks(system: "MainSystem") -> None:
    excluded = {*_waiters, asyncio.current_task(), system._runtime_task}
    remaining = [task for task in asyncio.all_tasks() if task not in excluded and not task.done()]
    for task in remaining:
        if not task.cancelling():
            task.cancel()
    if remaining:
        await asyncio.gather(*remaining, return_exceptions=True)


async def _finish_io() -> None:
    # 取消 to_thread 的包装任务不会停止线程。必须等 executor 完全退出，
    # 再关闭 daemon 回放 writer，避免线程最后产生的事件落在 flush 之后。
    loop = asyncio.get_running_loop()
    await loop.shutdown_default_executor()
    store = _component("src.decision_replay.recorder", "store")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shutdown-replay")
    try:
        await loop.run_in_executor(executor, store.close)
    finally:
        # close 的实际 I/O 已完成，不在事件循环上同步 join 空闲的辅助线程。
        executor.shutdown(wait=False)


async def _shutdown(system: "MainSystem") -> None:
    request_shutdown("runtime_shutdown")
    # 先停止启动/调度主任务，防止关停期间它继续创建组件；不等待主任务本身，
    # 因为模块入口的 finally 也会等待同一个关停任务。
    root = system._runtime_task
    if root is not None and root not in _waiters and not root.done() and not root.cancelling():
        root.cancel()
    errors: List[Exception] = []

    async def step(name: str, operation: Callable[[], Any]) -> None:
        try:
            result = operation()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            errors.append(RuntimeError(f"关停步骤意外取消: {name}"))
            logger.exception(f"关停步骤意外取消: {name}")
        except Exception as error:
            errors.append(error)
            logger.exception(f"关停步骤失败: {name}")

    if system.webui_server is not None:
        # WebUI 有独立事件循环和请求线程池，也必须停止接收并等正在执行的请求完成。
        await step("关闭 WebUI", lambda: system.webui_server.shutdown(timeout=None))
    await step("停止配置监听", lambda: _component("src.config.config", "config_manager").stop_file_watcher())
    # 停止周期生产者和网络接收任务；主调度任务本身由入口退出，不能在这里等待它。
    for task in system._scheduled_tasks:
        if not task.cancelling():
            task.cancel()
    if system._scheduled_tasks:
        await asyncio.gather(*system._scheduled_tasks, return_exceptions=True)
    system._scheduled_tasks.clear()
    await step(
        "等待已验收入站", lambda: _component("src.platform_io.manager", "get_platform_io_manager")().drain_inbound()
    )

    await step(
        "ON_STOP",
        lambda: _component("src.core.event_bus", "event_bus").emit(
            event_type=_component("src.core.types", "EventType").ON_STOP
        ),
    )

    manager = None
    try:
        manager = _component("src.chat.heart_flow.heartflow_manager", "heartflow_manager")
    except Exception as error:
        errors.append(error)
        logger.exception("加载会话运行时关停入口失败")
    if manager is not None:
        for chat in list(manager.heartflow_chat_list.values()):
            await step("停止 Planner", chat.stop)
    await step(
        "排空独立 @",
        lambda: _component("src.services.priority_at_reply_service", "priority_at_reply_service").shutdown(drain=True),
    )
    # 标签及图片任务已持久化，可留待重启续跑；必须等它们已经发起的 I/O 结束。
    await step(
        "停止标签提取并等待事务",
        lambda: _component("src.services.tag_extraction_service", "tag_extraction_service").shutdown(),
    )
    await step("等待标签清理", lambda: _component("src.services.tag_memory_service", "tag_memory_service").shutdown())
    await step(
        "等待图片描述写回", lambda: _component("src.chat.image_system.image_manager", "image_manager").shutdown()
    )
    await step(
        "排空记忆写回", lambda: _component("src.services.memory_flow_service", "memory_automation_service").shutdown()
    )
    await step("关闭记忆内核", lambda: _component("src.A_memorix.host_service", "a_memorix_host_service").stop())
    await step("停止插件", lambda: _component("src.plugin_runtime.integration", "get_plugin_runtime_manager")().stop())
    await step("停止表情模块", lambda: _component("src.emoji_system.emoji_manager", "emoji_manager").shutdown())
    await step(
        "停止托管任务",
        lambda: _component("src.manager.async_task_manager", "async_task_manager").stop_and_wait_all_tasks(),
    )
    await step("关闭 MCP", lambda: _component("src.mcp_module.service", "get_mcp_service")().close())
    await step("停止覆盖到期协调器", lambda: _component("src.services.owner_admin.overrides", "override_manager").shutdown())
    if system.watchdog_task is not None:
        system.watchdog_task.cancel()
        await asyncio.gather(system.watchdog_task, return_exceptions=True)
        system.watchdog_task = None
    await step("取消剩余后台任务", lambda: _cancel_remaining_tasks(system))
    await step("等待数据库线程并排空回放", _finish_io)
    if errors:
        raise ExceptionGroup("运行时清理存在失败步骤", errors)
    logger.info("运行时关停完成：在途线程已结束，内存写回与回放队列已排空")
