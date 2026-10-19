"""Replay store: observational events use a bounded queue; reply facts commit off-loop."""

from contextlib import closing
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, RLock, Thread
from typing import Any, Callable, Dict, List, Optional

import json
import logging
import sqlite3
import time
import uuid

logger = logging.getLogger(__name__)


class ReplayStore:
    def __init__(self, path: Path, retention_days: int = 7, capacity: int = 4096) -> None:
        self.path = path
        self.retention_days = retention_days
        self.retention_resolver: Optional[Callable[[], int]] = None
        self.queue: Queue = Queue(maxsize=capacity)
        self._closing = Event()
        self.lock = Lock()
        self.persistence_lock = RLock()
        self.worker: Optional[Thread] = None
        self.dropped = 0
        self.errors = 0
        self.last_cleanup = 0.0
        self._warned_at = 0.0

    def connect(self) -> sqlite3.Connection:
        with self.persistence_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=2)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS replay_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reply_event_keys (
                event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, timestamp REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbound_dispatches (
                session_id TEXT NOT NULL, outbound_message_id TEXT NOT NULL, reply_id TEXT NOT NULL,
                decision_id TEXT NOT NULL, source TEXT NOT NULL, session_type TEXT NOT NULL,
                system INTEGER NOT NULL, content_type TEXT NOT NULL, has_text INTEGER NOT NULL,
                has_emoji INTEGER NOT NULL, status TEXT NOT NULL,
                platform_message_ids TEXT NOT NULL DEFAULT '[]', last_event_at REAL NOT NULL,
                PRIMARY KEY(session_id,outbound_message_id));
            CREATE TABLE IF NOT EXISTS turns (
              logical_turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, session_name TEXT,
              source TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, outcome TEXT,
              gate_result TEXT, planner_result TEXT, emoji_result TEXT, failed INTEGER DEFAULT 0,
              spoken INTEGER DEFAULT 0, message_ids TEXT NOT NULL DEFAULT '[]',
              gate_detail TEXT, last_event_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS turns_session_time ON turns(session_id,started_at);
            CREATE INDEX IF NOT EXISTS turns_time ON turns(started_at);
            CREATE INDEX IF NOT EXISTS turns_gate_time ON turns(gate_result,started_at);
            CREATE INDEX IF NOT EXISTS turns_planner_time ON turns(planner_result,started_at);
            CREATE INDEX IF NOT EXISTS turns_emoji_time ON turns(emoji_result,started_at);
            CREATE INDEX IF NOT EXISTS turns_outcome_time ON turns(outcome,started_at);
            CREATE INDEX IF NOT EXISTS turns_source_time ON turns(source,started_at);
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
              timestamp REAL NOT NULL, session_id TEXT NOT NULL, logical_turn_id TEXT NOT NULL,
              cycle_id TEXT, request_id TEXT, message_ids TEXT NOT NULL, stage TEXT NOT NULL,
              event_type TEXT NOT NULL, decision TEXT, reason_code TEXT, detail TEXT NOT NULL,
              duration_ms REAL, schema_version INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS events_turn_time ON events(logical_turn_id,timestamp,sequence);
            CREATE INDEX IF NOT EXISTS events_session_time ON events(session_id,timestamp);
            CREATE TABLE IF NOT EXISTS requests (
              request_id TEXT PRIMARY KEY, logical_turn_id TEXT NOT NULL, session_id TEXT NOT NULL,
              model TEXT, task TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_hit_tokens INTEGER,
              cache_miss_tokens INTEGER, cost REAL, duration_ms REAL, status TEXT, usage_source TEXT);
            CREATE INDEX IF NOT EXISTS requests_turn ON requests(logical_turn_id);
            """)
            return connection

    def connect_readonly(self) -> sqlite3.Connection:
        """不创建文件、建表或触发清理的查询连接。"""
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def submit(self, event: Dict[str, Any]) -> None:
        try:
            with self.lock:
                if self._closing.is_set():
                    raise RuntimeError("Decision Replay writer is closed")
                if self.worker is None or not self.worker.is_alive():
                    self.worker = Thread(target=self._run, name="decision-replay-writer", daemon=True)
                    self.worker.start()
                self.queue.put_nowait(event)
        except Full:
            self.dropped += 1
            self._warning("Decision Replay queue full; event dropped")
        except Exception:
            self.errors += 1
            self._warning("Decision Replay enqueue failed")

    def _warning(self, text: str) -> None:
        if time.monotonic() - self._warned_at > 60:
            self._warned_at = time.monotonic()
            logger.warning(text, exc_info=False)

    def _run(self) -> None:
        while not self._closing.is_set() or not self.queue.empty():
            batch = []
            try:
                batch.append(self.queue.get(timeout=1))
            except Empty:
                if self.path.exists() and time.time() - self.last_cleanup > 3600:
                    try:
                        self.cleanup()
                    except Exception:
                        self.errors += 1
                        self._warning("Decision Replay cleanup failed")
                continue
            while len(batch) < 100:
                try:
                    batch.append(self.queue.get_nowait())
                except Empty:
                    break
            try:
                self.write_batch(batch)
            except Exception:
                self.errors += 1
                self.dropped += len(batch)
                self._warning("Decision Replay persistence failed; chat continues")
            finally:
                for _ in batch:
                    self.queue.task_done()

    def write_batch(self, events: List[Dict[str, Any]]) -> None:
        with self.persistence_lock:
            committed = []
            with closing(self.connect()) as connection, connection:
                expanded = []
                for event in events:
                    expanded.append(event)
                    if event["event_type"] == "reply_stats.send_confirmed" and not event["detail"].get("system"):
                        derived = {**event, "event_type": "reply_stats.reply_visible",
                                   "event_id": "reply_visible:" + uuid.uuid5(uuid.NAMESPACE_URL,
                                       event["session_id"] + "|" + event["detail"]["reply_id"]).hex}
                        expanded.append(derived)
                for event in expanded:
                    if event["event_type"].startswith("reply_stats."):
                        unique = connection.execute("INSERT OR IGNORE INTO reply_event_keys VALUES(?,?,?)",
                            (event["event_id"], event["event_type"], event["timestamp"]))
                        if not unique.rowcount:
                            continue
                        connection.execute("INSERT OR IGNORE INTO replay_metadata VALUES('reply_metrics_started_at',?)", (str(event["timestamp"]),))
                    turn_id = event["logical_turn_id"]
                    detail = event["detail"]
                    connection.execute(
                        "INSERT OR IGNORE INTO turns(logical_turn_id,session_id,session_name,source,started_at,last_event_at) VALUES (?,?,?,?,?,?)",
                        (
                            turn_id,
                            event["session_id"],
                            detail.get("session_name"),
                            event["source"],
                            event["timestamp"],
                            event["timestamp"],
                        ),
                    )
                    turn = connection.execute(
                        "SELECT session_id,message_ids FROM turns WHERE logical_turn_id=?", (turn_id,)
                    ).fetchone()
                    if turn["session_id"] != event["session_id"]:
                        raise ValueError("Cross-session trace rejected")
                    values = [
                        event[k]
                        for k in ("event_id", "timestamp", "session_id", "logical_turn_id", "cycle_id", "request_id")
                    ]
                    values += [
                        json.dumps(event["message_ids"]),
                        event["stage"],
                        event["event_type"],
                        event["decision"],
                        event["reason_code"],
                        json.dumps(detail, ensure_ascii=False),
                        event["duration_ms"],
                        event["schema_version"],
                    ]
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO events(event_id,timestamp,session_id,logical_turn_id,cycle_id,request_id,message_ids,stage,event_type,decision,reason_code,detail,duration_ms,schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                    if not cursor.rowcount:
                        continue
                    if event["event_type"].startswith("reply_stats."):
                        from .reply_metrics import persist_delivery_state
                        persist_delivery_state(connection, event)
                        committed.append(event)
                    ids = list(
                        dict.fromkeys(
                            json.loads(turn["message_ids"]) + (event["message_ids"] if stage_is_input(event) else [])
                        )
                    )
                    connection.execute(
                        "UPDATE turns SET last_event_at=MAX(last_event_at,?),message_ids=? WHERE logical_turn_id=?",
                        (event["timestamp"], json.dumps(ids), turn_id),
                    )
                    stage = event["stage"]
                    if event["event_type"] == "planner.started" or (stage == "model" and event["decision"] == "started"):
                        connection.execute(
                            "UPDATE turns SET ended_at=NULL,outcome=NULL WHERE logical_turn_id=?", (turn_id,)
                        )
                    if stage == "gate":
                        connection.execute(
                            "UPDATE turns SET gate_result=?,gate_detail=? WHERE logical_turn_id=?",
                            (event["decision"], json.dumps(detail), turn_id),
                        )
                    if stage == "planner" and event["event_type"] == "planner.finalized":
                        connection.execute(
                            "UPDATE turns SET planner_result=CASE WHEN planner_result='reply' THEN 'reply' WHEN ?='reply' THEN 'reply' WHEN planner_result='wait' AND ?='no_reply' THEN 'wait' ELSE ? END WHERE logical_turn_id=?",
                            (event["decision"], event["decision"], event["decision"], turn_id),
                        )
                    if stage == "emoji":
                        connection.execute(
                            "UPDATE turns SET emoji_result=CASE WHEN emoji_result='sent' THEN 'sent' ELSE ? END WHERE logical_turn_id=?",
                            (event["reason_code"], turn_id),
                        )
                    if stage == "send" and event["decision"] == "sent":
                        if detail.get("has_text"):
                            connection.execute("UPDATE turns SET spoken=1 WHERE logical_turn_id=?", (turn_id,))
                        if detail.get("has_emoji"):
                            connection.execute("UPDATE turns SET emoji_result='sent' WHERE logical_turn_id=?", (turn_id,))
                    if stage == "send" and event["decision"] == "failed" and detail.get("has_emoji"):
                        connection.execute(
                            "UPDATE turns SET emoji_result=CASE WHEN emoji_result IN ('sent','cooldown_blocked') THEN emoji_result ELSE 'send_failed' END WHERE logical_turn_id=?",
                            (turn_id,),
                        )
                    if event["decision"] in ("failed", "error"):
                        connection.execute("UPDATE turns SET failed=1 WHERE logical_turn_id=?", (turn_id,))
                    if event["event_type"] == "turn.ended":
                        connection.execute(
                            "UPDATE turns SET ended_at=?,outcome=? WHERE logical_turn_id=?",
                            (event["timestamp"], event["reason_code"], turn_id),
                        )
                    if stage == "model" and event["request_id"]:
                        connection.execute(
                            "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,cache_hit_tokens=excluded.cache_hit_tokens,cache_miss_tokens=excluded.cache_miss_tokens,cost=excluded.cost,duration_ms=excluded.duration_ms,status=excluded.status,usage_source=excluded.usage_source WHERE requests.logical_turn_id=excluded.logical_turn_id AND requests.session_id=excluded.session_id AND excluded.status!='started'",
                            (
                                event["request_id"],
                                turn_id,
                                event["session_id"],
                                detail.get("model"),
                                detail.get("task"),
                                detail.get("input_tokens"),
                                detail.get("output_tokens"),
                                detail.get("cache_hit_tokens"),
                                detail.get("cache_miss_tokens"),
                                detail.get("cost"),
                                event["duration_ms"],
                                event["decision"],
                                detail.get("usage_source", "unknown"),
                            ),
                        )
            if committed:
                from .reply_metrics import project
                projected = project(committed, end=time.time() + 1)
                for fact in projected["records"]:
                    logger.info("回复统计事件: event_id=%s decision_id=%s reply_id=%s type=%s 指标=%s reason=%s",
                                fact["event_id"], fact["decision_id"], fact["reply_id"], fact["event_type"],
                                [projected["labels"][key] for key in fact["metrics"]], fact["reason"])
            if time.time() - self.last_cleanup > 3600:
                self.cleanup()

    def cleanup(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        days = self.retention_resolver() if self.retention_resolver is not None else self.retention_days
        cutoff = now - max(1, days) * 86400
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "DELETE FROM requests WHERE logical_turn_id IN (SELECT logical_turn_id FROM turns WHERE started_at<? AND last_event_at<?)",
                (cutoff, cutoff),
            )
            connection.execute(
                "DELETE FROM events WHERE logical_turn_id IN (SELECT logical_turn_id FROM turns WHERE started_at<? AND last_event_at<?)",
                (cutoff, cutoff),
            )
            connection.execute("DELETE FROM turns WHERE started_at<? AND last_event_at<?", (cutoff, cutoff))
        self.last_cleanup = now

    def outbound_state(self, session_id: str, outbound_message_id: str) -> Optional[Dict[str, Any]]:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM outbound_dispatches WHERE session_id=? AND outbound_message_id=?",
                                     (session_id, outbound_message_id)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["platform_message_ids"] = json.loads(result["platform_message_ids"])
            return result

    def flush(self, timeout: float = 3) -> bool:
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.queue.unfinished_tasks

    def close(self) -> None:
        """停止接纳观察事件，等待已排队批次提交并退出写入线程。"""
        with self.lock:
            self._closing.set()
            worker = self.worker
        if worker is not None:
            worker.join()
        if self.queue.unfinished_tasks:
            raise RuntimeError("Decision Replay writer exited with unfinished events")

    def query(
        self,
        *,
        session_id: str = "",
        start: float = 0,
        end: Optional[float] = None,
        source: str = "",
        gate: str = "",
        planner: str = "",
        emoji: str = "",
        spoken: Optional[bool] = None,
        failed: Optional[bool] = None,
        page: int = 1,
        page_size: int = 25,
        ascending: bool = False,
    ) -> Dict[str, Any]:
        if time.time() - self.last_cleanup > 3600:
            self.cleanup()
        clauses = ["started_at>=?", "started_at<?"]
        values: List[Any] = [start, time.time() if end is None else end]
        for column, value in (
            ("session_id", session_id),
            ("source", source),
            ("gate_result", gate),
            ("planner_result", planner),
            ("emoji_result", emoji),
        ):
            if column == "emoji_result" and value == "not_sent":
                clauses.append("COALESCE(emoji_result,'')!='sent'")
                continue
            if value:
                clauses.append(column + "=?")
                values.append(value)
        for column, value in (("spoken", spoken), ("failed", failed)):
            if value is not None:
                clauses.append(column + "=?")
                values.append(int(value))
        where = " WHERE " + " AND ".join(clauses)
        page_size = max(1, min(100, page_size))
        page = max(1, page)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM turns" + where, values).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM turns"
                + where
                + " ORDER BY started_at "
                + ("ASC" if ascending else "DESC")
                + ",logical_turn_id LIMIT ? OFFSET ?",
                values + [page_size, (page - 1) * page_size],
            ).fetchall()
            summary = dict(
                connection.execute(
                    "SELECT COUNT(*) triggers,SUM(source IN ('message','priority_at')) external_triggers,SUM(spoken) spoken,SUM(gate_result='pass') gate_passed,SUM(gate_result IS NOT NULL) gate_evaluated,SUM(planner_result='no_reply') planner_no_reply,AVG((ended_at-started_at)*1000) avg_duration_ms FROM turns"
                    + where,
                    values,
                ).fetchone()
            )
            usage = dict(
                connection.execute(
                    "SELECT COUNT(*) model_calls,SUM(task='planner') planner_calls,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(cache_hit_tokens) cache_hit_tokens,SUM(cache_miss_tokens) cache_miss_tokens,SUM(cost) cost,COUNT(cost) priced_calls,COUNT(input_tokens) measured_calls FROM requests WHERE logical_turn_id IN (SELECT logical_turn_id FROM turns"
                    + where
                    + ")",
                    values,
                ).fetchone()
            )
            summary.update(usage)
            for key in (
                "external_triggers",
                "spoken",
                "gate_passed",
                "gate_evaluated",
                "planner_no_reply",
                "planner_calls",
            ):
                if summary[key] is None:
                    summary[key] = 0
            if usage["priced_calls"] != usage["model_calls"]:
                summary["cost"] = None
            summary["gate_pass_rate"] = (
                summary["gate_passed"] / summary["gate_evaluated"] if summary["gate_evaluated"] else None
            )
            return {
                "items": [self._turn(dict(row), connection) for row in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "summary": summary,
                "health": {
                    "dropped_events": self.dropped,
                    "write_errors": self.errors,
                    "queue_size": self.queue.qsize(),
                },
            }

    def _turn(self, row: Dict[str, Any], connection: sqlite3.Connection) -> Dict[str, Any]:
        row["message_ids"] = json.loads(row["message_ids"])
        row["gate_detail"] = json.loads(row["gate_detail"]) if row["gate_detail"] else None
        row["duration_ms"] = (row["ended_at"] - row["started_at"]) * 1000 if row["ended_at"] else None
        row["status"] = "complete" if row["ended_at"] else "incomplete_or_running"
        row["usage"] = dict(
            connection.execute(
                "SELECT COUNT(*) model_calls,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(cache_hit_tokens) cache_hit_tokens,SUM(cache_miss_tokens) cache_miss_tokens,SUM(cost) cost,COUNT(cost) priced_calls FROM requests WHERE logical_turn_id=?",
                (row["logical_turn_id"],),
            ).fetchone()
        )
        if row["usage"]["priced_calls"] != row["usage"]["model_calls"]:
            row["usage"]["cost"] = None
        return row

    def detail(self, turn_id: str, session_id: str, *, after: int = 0, limit: int = 100,
               read_only: bool = False) -> Optional[Dict[str, Any]]:
        with closing(self.connect_readonly() if read_only else self.connect()) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM turns WHERE logical_turn_id=? AND session_id=?", (turn_id, session_id)
            ).fetchone()
            if row is None:
                return None
            result = self._turn(dict(row), connection)
            events = connection.execute(
                "SELECT * FROM events WHERE logical_turn_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (turn_id, after, min(200, max(1, limit))),
            ).fetchall()
            result["events"] = [
                {**dict(e), "detail": json.loads(e["detail"]), "message_ids": json.loads(e["message_ids"])}
                for e in events
            ]
            result["requests"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM requests WHERE logical_turn_id=? ORDER BY request_id LIMIT 100", (turn_id,)
                )
            ]
            result["requests_total"] = connection.execute(
                "SELECT COUNT(*) FROM requests WHERE logical_turn_id=?", (turn_id,)
            ).fetchone()[0]
            result["requests_truncated"] = result["requests_total"] > len(result["requests"])
            result["next_cursor"] = events[-1]["sequence"] if events else after
            result["has_more"] = bool(
                connection.execute(
                    "SELECT 1 FROM events WHERE logical_turn_id=? AND sequence>? LIMIT 1",
                    (turn_id, result["next_cursor"]),
                ).fetchone()
            )
            return result

    def request_page(self, turn_id: str, session_id: str, page: int = 1, limit: int = 100) -> Dict[str, Any]:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            count = connection.execute(
                "SELECT COUNT(*) FROM requests WHERE logical_turn_id=? AND session_id=?", (turn_id, session_id)
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM requests WHERE logical_turn_id=? AND session_id=? ORDER BY request_id LIMIT ? OFFSET ?",
                (turn_id, session_id, min(200, max(1, limit)), (max(1, page) - 1) * min(200, max(1, limit))),
            ).fetchall()
            return {"items": [dict(r) for r in rows], "total": count, "page": page}


def stage_is_input(event: Dict[str, Any]) -> bool:
    return event["stage"] in {"message", "scheduler", "planner"} and event["event_type"] != "turn.ended"
