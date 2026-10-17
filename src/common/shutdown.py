"""进程级关停状态与内存 worker 排空协议。"""

from threading import Event
from typing import Any, Optional

import asyncio

_shutdown_requested = Event()
_exit_code = 0


def request_shutdown(reason: str = "", *, exit_code: Optional[int] = None) -> None:
    """标记正在关停；重启退出码由主入口在清理完成后读取。"""
    del reason
    global _exit_code
    if exit_code is not None:
        _exit_code = exit_code
    _shutdown_requested.set()


def is_shutdown_requested() -> bool:
    return _shutdown_requested.is_set()


def shutdown_exit_code(default: int = 0) -> int:
    return _exit_code if _exit_code else default


async def drain_worker_queue(worker: asyncio.Task, queue: asyncio.Queue[Any]) -> None:
    """等已接纳的工作处理完；worker 异常退出时显式报错，不能无限等待 join。"""
    joined = asyncio.create_task(queue.join())
    try:
        done, _ = await asyncio.wait((worker, joined), return_when=asyncio.FIRST_COMPLETED)
        if joined not in done:
            await worker
            await asyncio.sleep(0)  # 让最后一次 task_done 唤醒的 join 协程完成。
            if not joined.done():
                raise RuntimeError("关停期间 worker 已退出，但队列尚未排空")
        await joined
    finally:
        if not joined.done():
            joined.cancel()
        await asyncio.gather(joined, return_exceptions=True)
