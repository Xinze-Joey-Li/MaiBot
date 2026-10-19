"""统一覆盖存储与无 I/O 有效快照；基础配置始终按当前值读取，不保存恢复副本。"""

from contextlib import asynccontextmanager, closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
import asyncio
import json
import logging
import sqlite3
import time
import uuid

logger = logging.getLogger("owner_overrides")

@dataclass(frozen=True)
class Override:
    override_id: str
    property: str
    target_scope: str
    target_id: str
    value: Any
    created_by: str
    created_at: float
    expires_at: Optional[float]
    status: str
    priority: int
    request_id: str


@dataclass(frozen=True)
class EffectiveOverrides:
    muted: bool = False
    mode: str = "normal"
    temporary_frequency: Optional[float] = None
    persistent_frequency: Optional[float] = None
    threshold_delta: float = 0.0
    style: str = ""
    persona: str = ""
    override_ids: Tuple[str, ...] = ()

    @property
    def autonomous_allowed(self) -> bool:
        return not self.muted and self.mode != "mention_only"

    def frequency(self, current_base: float) -> float:
        # 基础零频率/暂停不能由覆盖重新打开；调用者还保留原有安全和冷却门控。
        if current_base <= 0:
            return 0.0
        if self.temporary_frequency is not None:
            return self.temporary_frequency
        inherited = self.persistent_frequency if self.persistent_frequency is not None else current_base
        factor = {"quiet": 0.5, "active": 1.25}.get(self.mode, 1.0)
        return min(1.0, max(0.0, inherited * factor))


class AccountDispatchBarrier:
    """正常投递并发读，软暂停变更独占写；写者等待旧投递结束后才确认。"""

    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.readers: Dict[asyncio.Task, int] = {}
        self.writer = False
        self.waiting_writers = 0

    @asynccontextmanager
    async def delivery(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        async with self.condition:
            # 同一任务的嵌套投递必须可重入，否则与正在等待的暂停形成自锁。
            await self.condition.wait_for(lambda: task in self.readers or (not self.writer and not self.waiting_writers))
            self.readers[task] = self.readers.get(task, 0) + 1
        try:
            yield
        finally:
            async with self.condition:
                self.readers[task] -= 1
                if not self.readers[task]:
                    del self.readers[task]
                self.condition.notify_all()

    @asynccontextmanager
    async def transition(self) -> AsyncIterator[None]:
        async with self.condition:
            if asyncio.current_task() in self.readers:
                raise RuntimeError("不能在同一投递回调内切换暂停；需独立管理请求")
            self.waiting_writers += 1
            try:
                await self.condition.wait_for(lambda: not self.readers and not self.writer)
                self.writer = True
            finally:
                self.waiting_writers -= 1
                self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.writer = False
                self.condition.notify_all()


class OverrideManager:
    """每次变更在一个事务中替换同属性同层覆盖；一次发布完整内存快照。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: Tuple[Override, ...] = ()
        self._write_lock = asyncio.Lock()
        self._dispatch_locks: Dict[str, asyncio.Lock] = {}
        self._account_barriers: Dict[str, AccountDispatchBarrier] = {}
        self._changed = asyncio.Event()
        self._expiry_task: Optional[asyncio.Task] = None
        self._loaded = False
        self.pause_epochs: Dict[str, str] = {}
        self._listeners: List[Callable[[Tuple[str, ...]], None]] = []

    def dispatch_lock(self, session_id: str) -> asyncio.Lock:
        return self._dispatch_locks.setdefault(session_id, asyncio.Lock())

    def account_barrier(self, account: str) -> AccountDispatchBarrier:
        return self._account_barriers.setdefault(account, AccountDispatchBarrier())

    @asynccontextmanager
    async def transition_lock(self, property: str, target_id: str) -> AsyncIterator[None]:
        if property == "soft_paused":
            async with self.account_barrier(target_id).transition():
                async with self.dispatch_lock(target_id):
                    yield
        else:
            async with self.dispatch_lock(target_id):
                yield

    def subscribe(self, listener: Callable[[Tuple[str, ...]], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS overrides (
          override_id TEXT PRIMARY KEY, property TEXT NOT NULL, target_scope TEXT NOT NULL,
          target_id TEXT NOT NULL, value TEXT NOT NULL, created_by TEXT NOT NULL,
          created_at REAL NOT NULL, expires_at REAL, status TEXT NOT NULL,
          priority INTEGER NOT NULL, request_id TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS overrides_target ON overrides(target_scope,target_id,status);
        CREATE UNIQUE INDEX IF NOT EXISTS overrides_active_property
          ON overrides(target_scope,target_id,property,priority) WHERE status='active';
        CREATE TABLE IF NOT EXISTS override_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT, override_id TEXT NOT NULL, status TEXT NOT NULL,
          actor TEXT NOT NULL, timestamp REAL NOT NULL, request_id TEXT NOT NULL);
        """)
        return connection

    @staticmethod
    def _rows(connection: sqlite3.Connection) -> Tuple[Override, ...]:
        entries = tuple(Override(**{**dict(row), "value": json.loads(row["value"])})
                        for row in connection.execute("SELECT * FROM overrides WHERE status='active'"))
        for entry in entries:
            duration = round(entry.expires_at - entry.created_at) if entry.expires_at is not None else None
            OverrideManager._validate(entry.property, entry.target_scope, entry.value, entry.priority, duration, False)
        return entries

    @staticmethod
    def _transition(connection: sqlite3.Connection, rows: List[sqlite3.Row], status: str,
                    actor: str, request_id: str, now: float) -> None:
        for row in rows:
            connection.execute("UPDATE overrides SET status=? WHERE override_id=?", (status, row["override_id"]))
            connection.execute(
                "INSERT INTO override_events(override_id,status,actor,timestamp,request_id) VALUES(?,?,?,?,?)",
                (row["override_id"], status, actor or ("system:expiry" if status == "expired" else row["created_by"]),
                 now, request_id or row["request_id"]),
            )

    def _expire_and_load(self) -> Tuple[Override, ...]:
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                "SELECT * FROM overrides WHERE status='active' AND expires_at IS NOT NULL AND expires_at<=?", (now,),
            ).fetchall()
            self._transition(connection, expired, "expired", "", "", now)
            return self._rows(connection)

    def _load_pause_epochs(self) -> Dict[str, str]:
        with closing(self._connect()) as connection:
            return {row[0]: row[1] for row in connection.execute(
                "SELECT target_id,override_id FROM overrides WHERE property='soft_paused' AND target_scope='bot' ORDER BY rowid"
            )}

    async def initialize(self, *, run_timer: bool = True) -> None:
        async with self._write_lock:
            if not self._loaded:
                self._entries = await asyncio.to_thread(self._expire_and_load)
                self.pause_epochs = await asyncio.to_thread(self._load_pause_epochs)
                await asyncio.to_thread(self._prime_presets)
                self._loaded = True
        if run_timer and (self._expiry_task is None or self._expiry_task.done()):
            self._expiry_task = asyncio.create_task(self._expiry_loop(), name="owner_override_expiry")
            self._expiry_task.add_done_callback(self._report_expiry_failure)

    @staticmethod
    def _report_expiry_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("覆盖到期协调器失败：持久化/重排已停止；有效快照仍按到期时间过滤，请检查存储。",
                         exc_info=(type(error), error, error.__traceback__))

    @staticmethod
    def _prime_presets() -> None:
        from src.common.prompt_i18n import load_prompt

        for locale in ("zh-CN", "en-US", "ja-JP"):
            for preset in ("gentle", "calm", "playful"):
                load_prompt("owner_presentation_" + preset, locale=locale)

    def _publish(self, entries: Tuple[Override, ...], changed_targets: Tuple[str, ...]) -> None:
        self._entries = entries
        for entry in entries:
            if entry.property == "soft_paused":
                self.pause_epochs[entry.target_id] = entry.override_id
        self._changed.set()
        for listener in self._listeners:
            listener(changed_targets)

    async def change(self, *, property: str, target_scope: str, target_id: str, value: Any,
                     created_by: str, request_id: str, duration: Optional[int] = None,
                     priority: int = 100, cancel: bool = False,
                     authorize: Optional[Callable[[], bool]] = None) -> Optional[Override]:
        """与实际平台投递共用群锁：确认变更后，旧回合不能跨过发送边界。"""
        self._validate(property, target_scope, value, priority, duration, cancel)
        if not target_id or not created_by or not request_id:
            raise ValueError("覆盖必须关联目标、操作者和审计请求")
        await self.initialize(run_timer=False)
        # 所有路径锁顺序一致：群发送锁 → 全局持久化锁。
        async with self.transition_lock(property, target_id):
            async with self._write_lock:
                now = time.time()
                entry = None if cancel else Override(
                    uuid.uuid4().hex, property, target_scope, target_id, value, created_by, now,
                    now + duration if duration is not None else None, "active", priority, request_id,
                )

                def persist() -> Tuple[Override, ...]:
                    with closing(self._connect()) as connection, connection:
                        if authorize is not None and not authorize():
                            raise PermissionError("排队期间主人授权已撤销")
                        connection.execute("BEGIN IMMEDIATE")
                        previous = connection.execute(
                            "SELECT * FROM overrides WHERE status='active' AND target_scope=? AND target_id=? "
                            "AND property=? AND priority=?", (target_scope, target_id, property, priority),
                        ).fetchall()
                        expired = [row for row in previous if row["expires_at"] is not None and row["expires_at"] <= now]
                        self._transition(connection, expired, "expired", "", "", now)
                        previous = [row for row in previous if row["expires_at"] is None or row["expires_at"] > now]
                        self._transition(connection, previous, "cancelled" if cancel else "replaced",
                                         created_by, request_id, now)
                        if entry is not None:
                            values = asdict(entry)
                            values["value"] = json.dumps(value)
                            connection.execute(
                                "INSERT INTO overrides VALUES (:override_id,:property,:target_scope,:target_id,:value,"
                                ":created_by,:created_at,:expires_at,:status,:priority,:request_id)", values,
                            )
                            connection.execute(
                                "INSERT INTO override_events(override_id,status,actor,timestamp,request_id) VALUES(?,?,?,?,?)",
                                (entry.override_id, "active", created_by, now, request_id),
                            )
                        return self._rows(connection)

                # shield 后仍等待事务结束，取消不能留下“已提交但内存未发布”的覆盖。
                task = asyncio.create_task(asyncio.to_thread(persist))
                try:
                    entries = await asyncio.shield(task)
                except asyncio.CancelledError:
                    self._publish(await task, (target_id,))
                    raise
                self._publish(entries, (target_id,))
                return entry

    @staticmethod
    def _validate(property: str, scope: str, value: Any, priority: int, duration: Optional[int], cancel: bool) -> None:
        allowed = {"chat": {"muted", "frequency", "threshold", "mode"}, "bot": {"style", "persona", "soft_paused"}}
        if property.startswith("ignore:") and scope in {"chat", "bot"} and priority == 100:
            import re

            if not re.fullmatch(r"ignore:[1-9][0-9]{4,19}", property) or (not cancel and value is not True):
                raise ValueError("用户屏蔽参数无效")
            allowed[scope] = {*allowed[scope], property}
        if property not in allowed.get(scope, set()) or priority not in {10, 100}:
            raise ValueError("覆盖属性或层级不受支持")
        if priority == 10 and (scope != "chat" or property not in {"frequency", "threshold", "mode"}):
            raise ValueError("持久配置只允许群频率、阈值和行为模式")
        if duration is not None and not 60 <= duration <= 604800:
            raise ValueError("覆盖时长必须在1分钟到7天内")
        if not cancel and priority == 100 and duration is None:
            raise ValueError("临时覆盖必须声明有效期")
        if cancel:
            return
        if property == "frequency" and (type(value) not in {int, float} or not 0 <= value <= 1):
            raise ValueError("频率必须是0到1的有限数值")
        if property == "threshold" and (type(value) not in {int, float} or not -0.5 <= value <= 0.5):
            raise ValueError("阈值偏移必须在正负50个百分点内")
        if property == "mode" and value not in {"quiet", "normal", "active", "mention_only"}:
            raise ValueError("未知行为模式")
        if property == "style" and value not in {"gentle", "calm"}:
            raise ValueError("未知表达预设")
        if property == "persona" and value not in {"gentle", "calm", "playful"}:
            raise ValueError("未知预设")
        if property in {"muted", "soft_paused"} and value is not True:
            raise ValueError("静音覆盖只允许True；恢复应取消覆盖")

    def active(self, scope: str = "", target_id: str = "") -> Tuple[Override, ...]:
        now = time.time()
        return tuple(entry for entry in self._entries if (entry.expires_at is None or entry.expires_at > now)
                     and (not scope or entry.target_scope == scope) and (not target_id or entry.target_id == target_id))

    def snapshot(self, session_id: str, bot_account_id: str = "") -> EffectiveOverrides:
        entries = [entry for entry in self.active() if
                   (entry.target_scope == "chat" and entry.target_id == session_id)
                   or (entry.target_scope == "bot" and entry.target_id == bot_account_id and bot_account_id)]
        values = {(entry.property, entry.priority): entry.value for entry in entries}
        mode = values.get(("mode", 100), values.get(("mode", 10), "normal"))
        delta = values.get(("threshold", 100))
        if delta is None:
            delta = values.get(("threshold", 10), 0.0) + {"quiet": 0.05, "active": -0.05}.get(mode, 0.0)
        return EffectiveOverrides(
            muted=bool(values.get(("muted", 100), False)), mode=mode,
            temporary_frequency=values.get(("frequency", 100)), persistent_frequency=values.get(("frequency", 10)),
            threshold_delta=min(0.5, max(-0.5, delta)), style=values.get(("style", 100), ""),
            persona=values.get(("persona", 100), ""), override_ids=tuple(entry.override_id for entry in entries),
        )

    async def _expiry_loop(self) -> None:
        while True:
            self._changed.clear()
            expirations = [entry.expires_at for entry in self._entries if entry.expires_at is not None]
            delay = max(0.05, min(expirations) - time.time()) if expirations else 3600
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=delay)
            except asyncio.TimeoutError:
                async with self._write_lock:
                    targets = tuple({entry.target_id for entry in self._entries if entry.expires_at is not None
                                     and entry.expires_at <= time.time()})
                    self._publish(await asyncio.to_thread(self._expire_and_load), targets)

    async def shutdown(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            await asyncio.gather(self._expiry_task, return_exceptions=True)
            self._expiry_task = None
        async with self._write_lock:
            pass


override_manager = OverrideManager(Path(__file__).resolve().parents[3] / "data/owner_admin/overrides.sqlite3")
