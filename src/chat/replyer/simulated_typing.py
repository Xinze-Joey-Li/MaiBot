"""回复模型与模拟输入共用同一计时窗口，避免叠加模型等待时间。"""

import asyncio
import time


def remaining_typing_delay(text: str, elapsed_seconds: float) -> float:
    """只统计汉字，前十字免等待，额外时间最多三点五秒。"""
    count = sum(
        "\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff"
        or "\U00020000" <= char <= "\U000323af"
        for char in text
    )
    duration = min(3.5, max(0, count - 10) * 0.12)
    return max(0.0, duration - max(0.0, elapsed_seconds))


async def wait_for_simulated_typing(text: str, started_at: float) -> float:
    delay = remaining_typing_delay(text, time.perf_counter() - started_at)
    if delay > 0:
        await asyncio.sleep(delay)
    return delay
