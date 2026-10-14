from unittest.mock import AsyncMock

import pytest

from src.chat.replyer import simulated_typing


@pytest.mark.parametrize("text,elapsed,expected", [
    ("字" * 10, 0, 0), ("字" * 11, 0, 0.12),
    ("字" * 20, 0.5, 0.7), ("字" * 50, 0, 3.5),
    ("字" * 50, 4, 0), ("abc，？！123😀" + "字" * 10, 0, 0),
    ("字" * 11, 0.2, 0), ("𠀀" * 11, 0, 0.12),
])
def test_remaining_delay(text, elapsed, expected):
    assert simulated_typing.remaining_typing_delay(text, elapsed) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_wait_subtracts_model_time_and_uses_nonblocking_sleep(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(simulated_typing.asyncio, "sleep", sleep)
    monkeypatch.setattr(simulated_typing.time, "perf_counter", lambda: 100.5)
    assert await simulated_typing.wait_for_simulated_typing("字" * 20, 100) == pytest.approx(0.7)
    sleep.assert_awaited_once()
    sleep.reset_mock()
    assert await simulated_typing.wait_for_simulated_typing("字" * 10, 100) == 0
    sleep.assert_not_awaited()
