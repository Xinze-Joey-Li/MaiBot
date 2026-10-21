"""In-process execution rights, separate from reusable conversation context.

Active work never expires. Finished normal/priority rights have separate bounded
2000-entry horizons; eviction and service shutdown end this finite guarantee.
No TTL, persistence, automatic retry, or delivery-success inference is provided.
"""
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Any, Dict, Optional, OrderedDict as OrderedDictType, Sequence, Set, Tuple
import uuid

from src.platform_io.route_key_factory import RouteKeyFactory

MessageKey = Tuple[str, str, str, str]
Token = Tuple[MessageKey, int]


def message_key(message: Any) -> MessageKey:
    # This private attribute is an internal stamp on dynamic SessionMessage and
    # legacy fixture objects, never a value trusted from external metadata.
    captured = getattr(message, "_processing_message_key", None)
    if captured is not None:
        return captured
    account, _ = RouteKeyFactory.extract_components(message.message_info.additional_config)
    mid = str(message.message_id).strip() if message.message_id is not None else ""
    if not mid:
        mid = "missing:" + uuid.uuid4().hex
    key = str(message.session_id), mid, str(message.platform or "").strip().lower(), account or ""
    message._processing_message_key = key
    return key


@dataclass(slots=True)
class Right:
    owner: str
    phase: str
    version: int
    runtime_id: int


class ProcessingRights:
    """A single authority; queues and tasks are lifecycle indexes, not claimers."""

    def __init__(self, *, max_normal: int = 2000, max_priority: int = 2000) -> None:
        if min(max_normal, max_priority) < 1:
            raise ValueError("Finished-right capacities must be positive")
        self._lock = RLock()
        self._version = 0
        self.active: Dict[MessageKey, Right] = {}
        self.normal: OrderedDictType[MessageKey, Right] = OrderedDict()
        self.priority: OrderedDictType[MessageKey, Right] = OrderedDict()
        self._limits = {"normal": max_normal, "priority": max_priority}
        self._running: Dict[int, Set[Token]] = {}

    def get(self, key: MessageKey) -> Optional[Right]:
        with self._lock:
            return self.active.get(key) or self.normal.get(key) or self.priority.get(key)

    def take(self, key: MessageKey, owner: str, phase: str, runtime: Any) -> Optional[Token]:
        with self._lock:
            if self.get(key) is not None:
                return None
            self._version += 1
            self.active[key] = Right(owner, phase, self._version, id(runtime))
            return key, self._version

    def release(self, token: Token) -> bool:
        """Rollback only this registration, never a later owner with the same key."""
        with self._lock:
            key, version = token
            right = self.active.get(key)
            if right is None or right.version != version:
                return False
            del self.active[key]
            return True

    def finish(self, token: Token) -> Optional[MessageKey]:
        """Retain every outcome including cancellation/UNKNOWN, without retry."""
        with self._lock:
            key, version = token
            right = self.active.get(key)
            if right is None or right.version != version:
                return None
            del self.active[key]
            right.phase = "retained"
            cache = self.normal if right.owner == "normal" else self.priority
            cache[key] = right
            if len(cache) > self._limits[right.owner]:
                expired, _ = cache.popitem(last=False)
                return expired
            return None

    def start_priority(self, key: MessageKey) -> Token:
        with self._lock:
            right = self.active[key]
            if right.owner != "priority":
                raise RuntimeError("Non-priority right cannot run in priority queue")
            right.phase = "running"
            return key, right.version

    def consume_normal(self, runtime: Any, messages: Sequence[Any]) -> None:
        eligible = {message_key(message) for message in messages}
        # Runtime's cache/cursor stay on its existing event loop. Only metadata
        # transitions are locked, never an unbounded whole pending batch.
        for message in runtime.message_cache[runtime._last_processed_index:]:
            key = message_key(message)
            if key not in eligible:
                with self._lock:
                    right = self.active.get(key)
                    if (right is not None and right.owner == "normal"
                            and right.phase == "pending" and right.runtime_id == id(runtime)):
                        self.release((key, right.version))
        for message in messages:
            key = message_key(message)
            with self._lock:
                right = self.get(key)
                if right is None:
                    # Existing pending from restoration/legacy callers is still
                    # ordinary input, never a new priority admission.
                    self.take(key, "normal", "pending", runtime)
                    right = self.active[key]
                if right.owner != "normal" or right.phase == "retained":
                    raise RuntimeError("Non-pending message reached ordinary consumption")
                right.phase = "running"
                self._running.setdefault(id(runtime), set()).add((key, right.version))

    def finish_normal(self, runtime: Any) -> None:
        with self._lock:
            tokens = self._running.pop(id(runtime), ())
        for token in tokens:
            self.finish(token)

    def priority_keys(self) -> Set[MessageKey]:
        with self._lock:
            return set(self.priority) | {key for key, right in self.active.items() if right.owner == "priority"}

    def clear(self) -> None:
        with self._lock:
            self.active.clear()
            self.normal.clear()
            self.priority.clear()
            self._running.clear()
            # Do not reset versions: late callbacks from before shutdown cannot
            # match a new right even if the same coordinator is reused.
