"""所有群聊表情共用发送额度，跨事件循环原子占位。"""

from threading import Lock
from typing import Dict, Set, Tuple

import time


class GroupEmojiGate:
    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: Set[Tuple[str, str]] = set()
        self._last_sent: Dict[Tuple[str, str], float] = {}

    def acquire(self, key: Tuple[str, str]) -> bool:
        with self._lock:
            if key in self._pending:
                return False
            last_sent = self._last_sent.get(key)
            if last_sent is not None and time.monotonic() - last_sent < 120:
                return False
            self._pending.add(key)
            return True

    def finish(self, key: Tuple[str, str], sent: bool) -> None:
        with self._lock:
            if sent:
                self._last_sent[key] = time.monotonic()
            self._pending.discard(key)


group_emoji_gate = GroupEmojiGate()
