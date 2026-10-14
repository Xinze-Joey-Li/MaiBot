"""临界评分消息的一次性参与机会，不产生额外模型判断请求。"""

from collections import deque
from typing import Deque, Optional, Set, Tuple


class PendingObservation:
    def __init__(self) -> None:
        self._used: Set[str] = set()
        self._order: Deque[str] = deque()
        self._pending: Optional[Tuple[Tuple[str, ...], float, float]] = None

    def evaluate(
        self, *, message_ids: Tuple[str, ...], now: float, newest_at: float,
        last_reply_at: float, gap: float, eligible: bool,
    ) -> Tuple[bool, Optional[float]]:
        """返回是否给予参与机会和首次观察所需延迟；同一消息不会重排计时。"""
        valid = (eligible and bool(message_ids) and 0 <= gap <= 0.03 + 1e-9
                 and 0 <= now - newest_at <= 60 and now - last_reply_at >= 30)
        if self._pending is not None:
            ids, started, reply_at = self._pending
            if not valid or ids != message_ids or last_reply_at != reply_at or now - started > 15:
                self._pending = None
                return False, None
            if now - started >= 10:
                self._pending = None
                return True, None
            return False, None
        if not valid or any(item in self._used for item in message_ids):
            return False, None
        for item in message_ids:
            self._used.add(item)
            self._order.append(item)
        # 与聊天缓存生命周期相匹配，避免长期运行无限保留消息 ID。
        while len(self._order) > 2000:
            self._used.discard(self._order.popleft())
        self._pending = (message_ids, now, last_reply_at)
        return False, 10.0
