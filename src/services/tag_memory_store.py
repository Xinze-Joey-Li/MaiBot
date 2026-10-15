"""独立 SQLite 标签索引，不修改 A_Memorix 画像或长期记忆表。"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterator, List, Optional, Tuple

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid

MAX_EXTRACTION_ATTEMPTS = 5
DAY_SECONDS = 86400
_INITIALIZE_LOCK = Lock()


class StaleTagConflict(ValueError):
    """人工修订后，旧快照的冲突裁决必须重新计算。"""


@dataclass
class TagCandidate:
    name: str
    summary: str
    category_path: Tuple[str, ...]
    source_message_ids: List[str] = field(default_factory=list)
    is_active: bool = False
    requires_long_term: bool = False
    previous_tag_id: Optional[str] = None
    evidence_quotes: Dict[str, str] = field(default_factory=dict)


def validate_conflict_pairs(decisions: List[Dict[str, Any]], new_count: int) -> Dict[int, str]:
    """按独立标签对校验，聚合新标签状态，拒绝真正的多方矛盾。"""
    pairs = set()
    new_statuses: Dict[int, str] = {}
    rank = {"valid": 0, "disputed": 1, "superseded": 2}
    for item in decisions:
        index = item["new_index"]
        old_id = item["old_tag_id"]
        action = item["decision"]
        if type(index) is not int or not 0 <= index < new_count or not isinstance(old_id, str) or not old_id:
            raise ValueError("冲突裁决的标签引用无效")
        if action not in {"replace_old", "keep_old", "uncertain"} or not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ValueError("冲突裁决动作或理由无效")
        pair = (index, old_id)
        if pair in pairs:
            raise ValueError("同一新旧标签对收到重复冲突裁决")
        pairs.add(pair)
        status = {"replace_old": "valid", "keep_old": "superseded", "uncertain": "disputed"}[action]
        current = new_statuses.get(index, "valid")
        if rank[status] > rank[current]:
            new_statuses[index] = status
        else:
            new_statuses[index] = current
    replacements: Dict[str, int] = {}
    for item in decisions:
        if item["decision"] == "replace_old" and new_statuses[item["new_index"]] == "valid":
            old_id = item["old_tag_id"]
            if old_id in replacements and replacements[old_id] != item["new_index"]:
                raise ValueError("多个有效新标签同时替代同一旧事实，需要校验新标签间关系")
            replacements[old_id] = item["new_index"]
    return new_statuses


class TagMemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        # 主循环与 WebUI 的线程池共用迁移锁，防止首次访问同时执行 ALTER TABLE。
        with _INITIALIZE_LOCK:
            self._initialize_schema()

    def _initialize_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9):
                raise RuntimeError(f"不支持的标签数据库版本: {version}")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS categories (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT NOT NULL,
                    path TEXT NOT NULL, parent_id TEXT,
                    UNIQUE(session_id, path), UNIQUE(session_id, id),
                    FOREIGN KEY(session_id, parent_id) REFERENCES categories(session_id, id)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, external_id TEXT NOT NULL,
                    summary TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(session_id, external_id), UNIQUE(session_id, id)
                );
                CREATE TABLE IF NOT EXISTS batch_sources (
                    batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    message_id TEXT NOT NULL, PRIMARY KEY(batch_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS tags (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                    category_id TEXT NOT NULL, name TEXT NOT NULL, summary TEXT NOT NULL,
                    created_at REAL NOT NULL, last_active_at REAL NOT NULL,
                    is_active INTEGER NOT NULL CHECK(is_active IN (0, 1)),
                    requires_long_term INTEGER NOT NULL CHECK(requires_long_term IN (0, 1)),
                    FOREIGN KEY(session_id, batch_id) REFERENCES batches(session_id, id),
                    FOREIGN KEY(session_id, category_id) REFERENCES categories(session_id, id),
                    UNIQUE(batch_id, category_id, name)
                );
                CREATE TABLE IF NOT EXISTS tag_sources (
                    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    message_id TEXT NOT NULL, PRIMARY KEY(tag_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS batch_links (
                    session_id TEXT NOT NULL, previous_id TEXT NOT NULL, current_id TEXT NOT NULL,
                    PRIMARY KEY(previous_id, current_id),
                    FOREIGN KEY(session_id, previous_id) REFERENCES batches(session_id, id),
                    FOREIGN KEY(session_id, current_id) REFERENCES batches(session_id, id)
                );
                CREATE TABLE IF NOT EXISTS maintenance (
                    name TEXT PRIMARY KEY, last_run_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_tags_scope_category ON tags(session_id, category_id);
                CREATE INDEX IF NOT EXISTS ix_tags_cleanup ON tags(is_active, requires_long_term, last_active_at);
                CREATE INDEX IF NOT EXISTS ix_batches_scope ON batches(session_id, archived, created_at);
                CREATE TABLE IF NOT EXISTS scanner_state (name TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tag_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL, received_at REAL NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(session_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS extraction_state (
                    session_id TEXT PRIMARY KEY, last_batch_id TEXT NOT NULL REFERENCES batches(id),
                    last_submitted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS extraction_jobs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                    model_output TEXT, retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS topics (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
                    summary TEXT NOT NULL, is_active INTEGER NOT NULL, last_active_at REAL NOT NULL,
                    UNIQUE(session_id, id)
                );
                CREATE TABLE IF NOT EXISTS topic_tags (
                    session_id TEXT NOT NULL, topic_id TEXT NOT NULL, tag_id TEXT NOT NULL,
                    PRIMARY KEY(topic_id, tag_id),
                    FOREIGN KEY(session_id, tag_id) REFERENCES tags(session_id, id) ON DELETE CASCADE,
                    FOREIGN KEY(session_id, topic_id) REFERENCES topics(session_id, id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ix_tags_scope_id ON tags(session_id, id);
                CREATE INDEX IF NOT EXISTS ix_inbox_scope ON tag_inbox(session_id, id);
            """)
            # 兼容已创建的基础标签库，新增列不覆盖原有分类、标签或画像数据。
            if "auto_active" not in {row[1] for row in connection.execute("PRAGMA table_info(tags)")}:
                connection.execute("ALTER TABLE tags ADD COLUMN auto_active INTEGER NOT NULL DEFAULT 0")
            if "quote" not in {row[1] for row in connection.execute("PRAGMA table_info(tag_sources)")}:
                connection.execute("ALTER TABLE tag_sources ADD COLUMN quote TEXT NOT NULL DEFAULT ''")
            if "last_submitted_at" not in {
                row[1] for row in connection.execute("PRAGMA table_info(extraction_state)")
            }:
                connection.execute(
                    "ALTER TABLE extraction_state ADD COLUMN last_submitted_at REAL NOT NULL DEFAULT 0"
                )
                connection.execute("""
                    UPDATE extraction_state SET last_submitted_at=
                    (SELECT created_at FROM batches WHERE batches.id=extraction_state.last_batch_id)
                """)
            if "status" not in {row[1] for row in connection.execute("PRAGMA table_info(tags)")}:
                connection.execute("ALTER TABLE tags ADD COLUMN status TEXT NOT NULL DEFAULT 'valid'")
            if "revision" not in {row[1] for row in connection.execute("PRAGMA table_info(tags)")}:
                connection.execute("ALTER TABLE tags ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            new_manual_column = "manual_active_until" not in {row[1] for row in connection.execute("PRAGMA table_info(tags)")}
            if new_manual_column:
                connection.execute("ALTER TABLE tags ADD COLUMN manual_active_until REAL")
            connection.execute("CREATE INDEX IF NOT EXISTS ix_tags_manual_deadline ON tags(manual_active_until,session_id)")
            if "conflict_output" not in {row[1] for row in connection.execute("PRAGMA table_info(extraction_jobs)")}:
                connection.execute("ALTER TABLE extraction_jobs ADD COLUMN conflict_output TEXT")
            if "lease_until" not in {row[1] for row in connection.execute("PRAGMA table_info(extraction_jobs)")}:
                connection.execute("ALTER TABLE extraction_jobs ADD COLUMN lease_until REAL NOT NULL DEFAULT 0")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS extraction_metrics (
                    session_id TEXT PRIMARY KEY, last_started_at REAL NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
                    total_seconds REAL NOT NULL DEFAULT 0, last_seconds REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS ix_extraction_ready ON extraction_jobs(retry_at,lease_until);
                CREATE TABLE IF NOT EXISTS extraction_conflict_chunks (
                    job_id TEXT NOT NULL REFERENCES extraction_jobs(id) ON DELETE CASCADE,
                    cache_key TEXT NOT NULL, output TEXT NOT NULL, PRIMARY KEY(job_id,cache_key)
                );
            """)
            for column in ("extraction_requests", "conflict_requests"):
                if column not in {row[1] for row in connection.execute("PRAGMA table_info(extraction_metrics)")}:
                    connection.execute(f"ALTER TABLE extraction_metrics ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS tag_conflicts (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, batch_id TEXT NOT NULL REFERENCES batches(id),
                    old_tag_id TEXT REFERENCES tags(id) ON DELETE SET NULL,
                    new_tag_id TEXT REFERENCES tags(id) ON DELETE SET NULL,
                    decision TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tag_revisions (
                    id TEXT PRIMARY KEY, tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)
            for column, kind in (("manual_decision", "TEXT"), ("manual_reason", "TEXT"), ("resolved_at", "REAL")):
                if column not in {row[1] for row in connection.execute("PRAGMA table_info(tag_conflicts)")}:
                    connection.execute(f"ALTER TABLE tag_conflicts ADD COLUMN {column} {kind}")
            if new_manual_column:
                connection.execute("UPDATE tags SET manual_active_until=COALESCE((SELECT MAX(created_at) "
                                   "FROM tag_revisions WHERE tag_id=tags.id AND reason='人工激活话题'),last_active_at)+? "
                                   "WHERE is_active=1", (DAY_SECONDS,))
                # 旧界面激活话题曾把手动激活也写成 auto_active；没有后续自动延续时撤销该标记。
                connection.execute("UPDATE tags SET auto_active=0 WHERE is_active=1 AND EXISTS("
                                   "SELECT 1 FROM tag_revisions r WHERE r.tag_id=tags.id AND r.reason='人工激活话题' "
                                   "AND json_extract(r.before_json,'$.auto_active')=0 AND tags.last_active_at<=r.created_at)")
            if version < 6:
                self._migrate_latest_conflicts(connection)
            # 两套旧版 schema 7 分别含租约或隔离字段；统一升级到 8，保留任务及分块缓存。
            for column, kind in (("status", "TEXT NOT NULL DEFAULT 'pending'"),
                                 ("resumed", "INTEGER NOT NULL DEFAULT 0")):
                if column not in {row[1] for row in connection.execute("PRAGMA table_info(extraction_jobs)")}:
                    connection.execute(f"ALTER TABLE extraction_jobs ADD COLUMN {column} {kind}")
            if version < 8:
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TEMP TABLE saved_conflict_chunks AS SELECT * FROM extraction_conflict_chunks;
                    DROP TABLE extraction_conflict_chunks;
                    CREATE TABLE extraction_jobs_v8 (
                        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload TEXT NOT NULL,
                        model_output TEXT, retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT '', conflict_output TEXT,
                        status TEXT NOT NULL DEFAULT 'pending', resumed INTEGER NOT NULL DEFAULT 0,
                        lease_until REAL NOT NULL DEFAULT 0
                    );
                    INSERT INTO extraction_jobs_v8
                        SELECT id,session_id,payload,model_output,retry_at,attempts,error,conflict_output,status,resumed,lease_until
                        FROM extraction_jobs;
                    DROP TABLE extraction_jobs;
                    ALTER TABLE extraction_jobs_v8 RENAME TO extraction_jobs;
                    UPDATE extraction_jobs SET status='failed' WHERE attempts>=5;
                    CREATE TABLE extraction_conflict_chunks (
                        job_id TEXT NOT NULL REFERENCES extraction_jobs(id) ON DELETE CASCADE,
                        cache_key TEXT NOT NULL, output TEXT NOT NULL, PRIMARY KEY(job_id,cache_key)
                    );
                    INSERT INTO extraction_conflict_chunks SELECT * FROM saved_conflict_chunks;
                    DROP TABLE saved_conflict_chunks;
                    PRAGMA user_version=8;
                    COMMIT;
                """)
            connection.execute("CREATE INDEX IF NOT EXISTS ix_extraction_ready ON extraction_jobs(status,retry_at,lease_until)")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS extraction_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    event TEXT NOT NULL, created_at REAL NOT NULL, detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_extraction_events_job ON extraction_events(session_id,job_id);
                CREATE TABLE IF NOT EXISTS conflict_pair_cache (
                    session_id TEXT NOT NULL, cache_key TEXT NOT NULL,
                    output TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(session_id,cache_key)
                );
            """)
            connection.execute("PRAGMA user_version=9")

    @staticmethod
    def _migrate_latest_conflicts(connection: sqlite3.Connection) -> None:
        """已有冲突链只保留最后创建的裁决，链中其他旧事实不能重新恢复有效。"""
        rows = connection.execute("SELECT rowid AS sequence,* FROM tag_conflicts ORDER BY rowid DESC").fetchall()
        pending = list(rows)
        while pending:
            latest = pending.pop(0)
            component = [latest]
            ids = {value for value in (latest["old_tag_id"], latest["new_tag_id"]) if value is not None}
            changed = True
            while changed:
                changed = False
                for row in list(pending):
                    members = {value for value in (row["old_tag_id"], row["new_tag_id"]) if value is not None}
                    if row["session_id"] == latest["session_id"] and ids & members:
                        component.append(row)
                        ids.update(members)
                        pending.remove(row)
                        changed = True
            for row in component[1:]:
                connection.execute("DELETE FROM tag_conflicts WHERE id=?", (row["id"],))
            pair = {latest["old_tag_id"], latest["new_tag_id"]}
            for tag_id in ids - pair:
                connection.execute("UPDATE tags SET status='superseded',is_active=0,auto_active=0,"
                                   "manual_active_until=NULL,revision=revision+1 WHERE id=? AND status!='deleted'", (tag_id,))
            action = latest["manual_decision"] or latest["decision"]
            reason = latest["manual_reason"] or latest["reason"]
            connection.execute("UPDATE tag_conflicts SET decision=?,reason=? WHERE id=?", (action, reason, latest["id"]))
            statuses = {latest["old_tag_id"]: "superseded", latest["new_tag_id"]: "valid"} if action == "replace_old" else (
                {latest["old_tag_id"]: "valid", latest["new_tag_id"]: "superseded"} if action == "keep_old" else
                {latest["old_tag_id"]: "disputed", latest["new_tag_id"]: "disputed"})
            for tag_id, status in statuses.items():
                if status == "valid":
                    connection.execute("UPDATE tags SET status=?,revision=revision+1 WHERE id=? AND status!='deleted'", (status, tag_id))
                else:
                    connection.execute("UPDATE tags SET status=?,is_active=0,auto_active=0,manual_active_until=NULL,"
                                       "revision=revision+1 WHERE id=? AND status!='deleted'", (status, tag_id))

    @staticmethod
    def _expire_manual_activity(connection: sqlite3.Connection, timestamp: float, scope: Optional[str] = None) -> int:
        query = "UPDATE tags SET is_active=0,manual_active_until=NULL,revision=revision+1 WHERE manual_active_until<=?"
        values: List[Any] = [timestamp]
        if scope is not None:
            query += " AND session_id=?"
            values.append(scope)
        # 只移除手动保护，auto_active 继续由连续批次的话题识别决定。
        expired = connection.execute("SELECT id FROM tags WHERE manual_active_until<=?" +
                                     (" AND session_id=?" if scope is not None else ""), values).fetchall()
        count = connection.execute(query, values).rowcount
        for row in expired:
            connection.execute("UPDATE topics SET is_active=0 WHERE id IN (SELECT topic_id FROM topic_tags WHERE tag_id=?) "
                               "AND NOT EXISTS(SELECT 1 FROM topic_tags tt JOIN tags t ON t.id=tt.tag_id "
                               "WHERE tt.topic_id=topics.id AND t.status='valid' AND (t.is_active=1 OR t.auto_active=1))", (row[0],))
        return count

    def expire_manual_activity(self, now: Optional[float] = None) -> int:
        with self._connect(write=True) as connection:
            return self._expire_manual_activity(connection, time.time() if now is None else now)

    @staticmethod
    def _category(
        connection: sqlite3.Connection, session_id: str, path: Tuple[str, ...], session_name: str,
    ) -> str:
        parent_id = None
        for depth in range(len(path) + 1):
            prefix = path[:depth]
            encoded_path = json.dumps(prefix, ensure_ascii=False)
            row = connection.execute(
                "SELECT id FROM categories WHERE session_id=? AND path=?", (session_id, encoded_path)
            ).fetchone()
            if row is None:
                category_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO categories VALUES (?, ?, ?, ?, ?)",
                    (category_id, session_id, prefix[-1] if prefix else session_name, encoded_path, parent_id),
                )
            else:
                category_id = row["id"]
                if not prefix:
                    connection.execute("UPDATE categories SET name=? WHERE id=?", (session_name, category_id))
            parent_id = category_id
        return parent_id

    def record_batch(
        self, *, session_id: str, session_name: str, external_id: str, summary: str,
        message_ids: List[str], tags: List[TagCandidate], now: Optional[float] = None,
        extraction_job_id: Optional[str] = None,
        conflict_decisions: Optional[List[Dict[str, Any]]] = None,
        conflict_versions: Optional[Dict[str, int]] = None,
        conflict_categories: Optional[List[List[str]]] = None,
    ) -> Dict[str, Any]:
        if not session_id.strip() or not session_name.strip() or not external_id.strip() or not summary.strip():
            raise ValueError("会话、批次标识和批次摘要不能为空")
        sources = list(dict.fromkeys(message_ids))
        if not sources or any(not value.strip() for value in sources):
            raise ValueError("批次必须包含有效来源消息 ID")
        for tag in tags:
            if not tag.name.strip() or not tag.summary.strip() or not tag.category_path:
                raise ValueError("标签名称、摘要和分类路径不能为空")
            if any(not item.strip() for item in tag.category_path):
                raise ValueError("分类节点名称不能为空")
            if not tag.source_message_ids or not set(tag.source_message_ids).issubset(sources):
                raise ValueError("标签证据必须来自当前批次的消息")
            if not set(tag.evidence_quotes).issubset(tag.source_message_ids):
                raise ValueError("证据片段必须属于标签来源消息")
        serialized_tags = []
        for tag in tags:
            serialized = asdict(tag)
            # 空的新字段不改变基础版本的批次指纹，旧库升级后仍可幂等重放。
            if tag.previous_tag_id is None:
                serialized.pop("previous_tag_id")
            if not tag.evidence_quotes:
                serialized.pop("evidence_quotes")
            serialized_tags.append(serialized)
        payload = json.dumps(
            {"summary": summary, "sources": sources, "tags": serialized_tags},
            ensure_ascii=False, sort_keys=True,
        )
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        timestamp = time.time() if now is None else now
        with self._connect(write=True) as connection:
            self._expire_manual_activity(connection, timestamp, session_id)
            existing = connection.execute(
                "SELECT id, fingerprint FROM batches WHERE session_id=? AND external_id=?", (session_id, external_id)
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("同一批次 ID 的输入发生变化，拒绝覆盖")
                return {"batch_id": existing["id"], "created": False}
            self._check_conflict_versions(connection, session_id, conflict_versions or {}, conflict_categories)
            previous_extraction = None
            allow_topic_update = True
            job_payload = None
            if extraction_job_id is not None:
                job = connection.execute(
                    "SELECT payload,resumed FROM extraction_jobs WHERE id=? AND session_id=?", (extraction_job_id, session_id)
                ).fetchone()
                if job is None:
                    raise ValueError("提取任务不存在或不属于当前会话")
                job_payload = json.loads(job[0])
                if set(sources) != {item["message_id"] for item in job_payload["messages"]}:
                    raise ValueError("提交消息与冻结的提取任务不一致")
                previous_extraction = job_payload["previous_batch_id"]
                if "reference_snapshot" in job_payload:
                    allowed = {tag["id"]: tag for tag in job_payload["reference_snapshot"]["previous_extraction_tags"]}
                    for tag in tags:
                        if tag.previous_tag_id is not None and (
                            tag.previous_tag_id not in allowed or
                            allowed[tag.previous_tag_id]["category_path"] != list(tag.category_path)
                        ):
                            raise ValueError("提交延续引用不属于冻结的同会话上一批标签")
                state = connection.execute(
                    "SELECT last_batch_id FROM extraction_state WHERE session_id=?", (session_id,)
                ).fetchone()
                if (state[0] if state else None) != previous_extraction:
                    if not job["resumed"]:
                        raise ValueError("相邻提取状态发生变化，拒绝提交过期任务")
                    # 人工恢复隔离任务时可能已有后续批次；保留证据，但不能伪造相邻延续。
                    allow_topic_update = False
            # 分类树有稳定骨架，人物下可继续按用户 ID 建子节点，共同事件放在群公共分支。
            for branch in ("人物", "群公共"):
                for category in ("偏好", "事件", "与机器人的互动", "群活动"):
                    self._category(connection, session_id, (branch, category), session_name)
            batch_id = uuid.uuid4().hex
            previous = connection.execute(
                "SELECT id FROM batches WHERE session_id=? AND archived=0 ORDER BY rowid DESC LIMIT 3", (session_id,)
            ).fetchall()
            connection.execute(
                "INSERT INTO batches(id, session_id, external_id, summary, fingerprint, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (batch_id, session_id, external_id, summary, fingerprint, timestamp),
            )
            connection.executemany(
                "INSERT INTO batch_sources VALUES(?,?)", [(batch_id, value) for value in sources]
            )
            connection.executemany(
                "INSERT INTO batch_links VALUES(?,?,?)", [(session_id, row["id"], batch_id) for row in previous]
            )
            inserted_tags = []
            for tag in tags:
                category_id = self._category(connection, session_id, tag.category_path, session_name)
                tag_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO tags(id,session_id,batch_id,category_id,name,summary,created_at,last_active_at,"
                    "is_active,requires_long_term,manual_active_until) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (tag_id, session_id, batch_id, category_id, tag.name, tag.summary, timestamp, timestamp,
                     int(tag.is_active), int(tag.requires_long_term), timestamp + DAY_SECONDS if tag.is_active else None),
                )
                connection.executemany(
                    "INSERT INTO tag_sources(tag_id,message_id,quote) VALUES(?,?,?)",
                    [(tag_id, value, tag.evidence_quotes.get(value, ""))
                     for value in dict.fromkeys(tag.source_message_ids)],
                )
                inserted_tags.append((tag_id, tag))
            if job_payload is not None and allow_topic_update:
                self._update_topics(connection, session_id, previous_extraction, inserted_tags, timestamp)
            # 冲突裁决与新批次、消费游标在同一事务提交，失败时不能留下半次纠正。
            decisions = conflict_decisions or []
            new_statuses = validate_conflict_pairs(decisions, len(inserted_tags))
            # 更新任一旧事实时使整条旧冲突链失效，避免其它旧网页裁决恢复已被更新的事实。
            affected = {item["old_tag_id"] for item in decisions}
            prior_conflicts = [dict(row) for row in connection.execute(
                "SELECT id,old_tag_id,new_tag_id FROM tag_conflicts WHERE session_id=? AND batch_id<>?",
                (session_id, batch_id))] if affected else []
            invalidated = []
            while True:
                matched = [row for row in prior_conflicts if row["old_tag_id"] in affected or row["new_tag_id"] in affected]
                if not matched:
                    break
                for row in matched:
                    prior_conflicts.remove(row)
                    invalidated.append((row["id"],))
                    affected.update(value for value in (row["old_tag_id"], row["new_tag_id"]) if value is not None)
            connection.executemany("DELETE FROM tag_conflicts WHERE id=?", invalidated)
            old_statuses: Dict[str, str] = {}
            rank = {"valid": 0, "disputed": 1, "superseded": 2}
            for decision in decisions:
                index = decision["new_index"]
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(inserted_tags):
                    raise ValueError("冲突裁决的新标签索引无效")
                old = connection.execute(
                    "SELECT t.*,c.path FROM tags t JOIN categories c ON c.id=t.category_id "
                    "WHERE t.session_id=? AND t.id=?", (session_id, decision["old_tag_id"]),
                ).fetchone()
                new_id, candidate = inserted_tags[index]
                if (old is None or old["status"] in {"superseded", "deleted"}
                        or old["revision"] != decision.get("old_revision", 0)):
                    raise StaleTagConflict("旧标签已被修订，冲突裁决需要重算")
                if json.loads(old["path"]) != list(candidate.category_path):
                    raise ValueError("冲突裁决标签不属于同一会话分类")
                action = decision["decision"]
                if action not in {"replace_old", "keep_old", "uncertain"} or not decision["reason"].strip():
                    raise ValueError("冲突裁决动作或理由无效")
                new_status = new_statuses[index]
                # 被另一旧事实否定的新标签不能反过来淘汰 A；所有标签对先计算完再统一更新。
                old_status = ("superseded" if new_status == "valid" else
                              "disputed" if new_status == "disputed" else old["status"]) if action == "replace_old" else (
                    "valid" if action == "keep_old" else
                    "disputed" if new_status != "superseded" else old["status"])
                prior_status = old_statuses.get(old["id"], old_status)
                old_statuses[old["id"]] = max((prior_status, old_status), key=rank.__getitem__)
                if action != "replace_old":
                    # 活跃话题摘要也不能继续携带已经拒绝的新说法。
                    connection.execute("UPDATE topics SET title=?,summary=? WHERE session_id=? AND id IN "
                                       "(SELECT topic_id FROM topic_tags WHERE tag_id=?)",
                                       (old["name"], old["summary"], session_id, new_id))
                # 保留双方证据和模型理由；不直接改写用户原话或删除旧事实。
                old_sources = [dict(row) for row in connection.execute(
                    "SELECT message_id,quote FROM tag_sources WHERE tag_id=?", (old["id"],)
                )]
                evidence = json.dumps({"old": {"name": old["name"], "summary": old["summary"], "sources": old_sources},
                                       "new": asdict(candidate), "old_status_before": old["status"]}, ensure_ascii=False)
                # 同批的独立标签对都保留证据；上一轮连通冲突链已在应用前统一失效。
                connection.execute("INSERT INTO tag_conflicts(id,session_id,batch_id,old_tag_id,new_tag_id,decision,reason,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                   (uuid.uuid4().hex, session_id, batch_id, old["id"], new_id, action,
                                    decision["reason"], evidence, timestamp))
            statuses = {**old_statuses, **{inserted_tags[index][0]: status for index, status in new_statuses.items()}}
            for conflict in connection.execute("SELECT id,old_tag_id,new_tag_id,evidence FROM tag_conflicts WHERE batch_id=?", (batch_id,)):
                evidence = json.loads(conflict["evidence"])
                evidence["applied_old_status"] = statuses[conflict["old_tag_id"]]
                evidence["applied_new_status"] = statuses[conflict["new_tag_id"]]
                connection.execute("UPDATE tag_conflicts SET evidence=? WHERE id=?",
                                   (json.dumps(evidence, ensure_ascii=False), conflict["id"]))
            for tag_id, status in statuses.items():
                if status == "valid":
                    connection.execute("UPDATE tags SET status=?,revision=revision+1 WHERE id=?", (status, tag_id))
                else:
                    connection.execute("UPDATE tags SET status=?,is_active=0,auto_active=0,manual_active_until=NULL,"
                                       "revision=revision+1 WHERE id=?", (status, tag_id))
            connection.execute("""UPDATE topics SET is_active=0 WHERE session_id=? AND NOT EXISTS(
                SELECT 1 FROM topic_tags tt JOIN tags t ON t.id=tt.tag_id
                WHERE tt.topic_id=topics.id AND t.status='valid' AND (t.is_active=1 OR t.auto_active=1))""", (session_id,))
            if job_payload is not None:
                connection.executemany("DELETE FROM tag_inbox WHERE id=? AND session_id=?",
                                       [(item, session_id) for item in job_payload["inbox_ids"]])
                connection.execute(
                    "INSERT INTO extraction_state VALUES(?,?,?) ON CONFLICT(session_id) "
                    "DO UPDATE SET last_batch_id=excluded.last_batch_id,last_submitted_at=excluded.last_submitted_at",
                    (session_id, batch_id, timestamp if now is not None else time.time()),
                )
                connection.execute("DELETE FROM extraction_jobs WHERE id=?", (extraction_job_id,))
                connection.execute("INSERT INTO extraction_events(session_id,job_id,event,created_at,detail) VALUES(?,?,?,?,?)",
                                   (session_id, extraction_job_id, "committed", timestamp, json.dumps({
                                       "batch_id": batch_id, "messages_consumed": True, "new_tags": len(tags),
                                       "continued_tags": sum(tag.previous_tag_id is not None for tag in tags),
                                       "conflict_pairs": len(decisions),
                                       "superseded_tags": sum(status == "superseded" for status in statuses.values()),
                                       "disputed_tags": sum(status == "disputed" for status in statuses.values()),
                                   }, ensure_ascii=False)))
            return {"batch_id": batch_id, "created": True}

    @staticmethod
    def _update_topics(
        connection: sqlite3.Connection, session_id: str, previous_batch_id: Optional[str],
        inserted_tags: List[Tuple[str, TagCandidate]], timestamp: float,
    ) -> None:
        connection.execute("UPDATE topics SET is_active=0 WHERE session_id=?", (session_id,))
        connection.execute("UPDATE tags SET auto_active=0 WHERE session_id=?", (session_id,))
        for tag_id, tag in inserted_tags:
            if tag.previous_tag_id is None:
                continue
            previous = connection.execute("""
                SELECT t.id, c.path FROM tags t JOIN categories c ON c.id=t.category_id
                WHERE t.session_id=? AND t.batch_id=? AND t.id=?
            """, (session_id, previous_batch_id, tag.previous_tag_id)).fetchone()
            if previous is None or json.loads(previous["path"]) != list(tag.category_path):
                raise ValueError("话题延续只能引用当前会话上一次提取的同分类标签")
            topic = connection.execute(
                "SELECT topic_id FROM topic_tags WHERE session_id=? AND tag_id=?", (session_id, previous["id"])
            ).fetchone()
            topic_id = topic[0] if topic else uuid.uuid4().hex
            connection.execute(
                "INSERT INTO topics VALUES(?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET "
                "title=excluded.title,summary=excluded.summary,is_active=1,last_active_at=excluded.last_active_at",
                (topic_id, session_id, tag.name, tag.summary, timestamp),
            )
            connection.executemany("INSERT OR IGNORE INTO topic_tags VALUES(?,?,?)",
                                   [(session_id, topic_id, previous["id"]), (session_id, topic_id, tag_id)])
            connection.execute("""
                UPDATE tags SET auto_active=1,last_active_at=? WHERE session_id=? AND status='valid' AND
                id IN (SELECT tag_id FROM topic_tags WHERE topic_id=?)
            """, (timestamp, session_id, topic_id))
        connection.execute("UPDATE topics SET is_active=1 WHERE session_id=? AND id IN ("
                           "SELECT tt.topic_id FROM topic_tags tt JOIN tags t ON t.id=tt.tag_id "
                           "WHERE t.status='valid' AND t.is_active=1 AND t.manual_active_until>?)", (session_id, timestamp))

    def initialize_scanner(self, last_id: int) -> None:
        with self._connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO scanner_state VALUES('messages',?)", (last_id,))

    def scanner_position(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT last_id FROM scanner_state WHERE name='messages'").fetchone()
            if row is None:
                raise RuntimeError("消息扫描游标尚未初始化")
            return row[0]

    def enqueue_page(self, candidates: List[Dict[str, Any]], last_id: int) -> None:
        with self._connect(write=True) as connection:
            for item in candidates:
                connection.execute(
                    "INSERT OR IGNORE INTO tag_inbox(session_id,message_id,received_at,payload) VALUES(?,?,?,?)",
                    (item["session_id"], item["message_id"], item["timestamp"], json.dumps(item, ensure_ascii=False)),
                )
            connection.execute("UPDATE scanner_state SET last_id=MAX(last_id,?) WHERE name='messages'", (last_id,))

    def next_extraction_job(
        self, batch_size: int = 25, max_wait_seconds: int = 1800, *, claim: bool = False,
        batch_sizes: Optional[Dict[str, int]] = None,
    ) -> Optional[Dict[str, Any]]:
        if batch_size < 1 or max_wait_seconds < 1:
            raise ValueError("批量大小和最长等待时间必须为正数")
        if any(size < 1 for size in (batch_sizes or {}).values()):
            raise ValueError("群批量大小必须为正数")
        now = time.time()
        with self._connect(write=True) as connection:
            # 从独立失败日志恢复 SQLite 写锁期间未落盘的状态，租约中的任务不可重新选取。
            for job in connection.execute("SELECT id FROM extraction_jobs WHERE status='pending' AND lease_until<=?", (now,)):
                failure = self.read_job_cache(job["id"], "failures")
                if failure is not None:
                    connection.execute("UPDATE extraction_jobs SET status=?,attempts=?,retry_at=?,error=?, "
                                       "model_output=CASE WHEN ? THEN NULL ELSE model_output END, "
                                       "conflict_output=CASE WHEN ? THEN NULL ELSE conflict_output END WHERE id=?",
                                       (failure["status"],failure["attempts"],failure["retry_at"],failure["error"],
                                        failure["invalid_output"],failure["invalid_conflict"],job["id"]))
            ready = connection.execute("""
                WITH ready AS (
                    SELECT j.session_id,? AS batch_size,j.id AS job_id FROM extraction_jobs j
                    WHERE j.status='pending' AND j.retry_at<=? AND j.lease_until<=?
                    AND NOT EXISTS(SELECT 1 FROM extraction_jobs busy WHERE busy.session_id=j.session_id AND busy.lease_until>?)
                    UNION ALL
                    SELECT i.session_id,COALESCE(l.value,?),NULL FROM tag_inbox i
                    LEFT JOIN extraction_state s ON s.session_id=i.session_id
                    LEFT JOIN json_each(?) l ON l.key=i.session_id
                    WHERE i.session_id NOT IN(SELECT session_id FROM extraction_jobs WHERE status='pending' OR lease_until>?)
                    AND NOT EXISTS(SELECT 1 FROM extraction_jobs j,json_each(j.payload,'$.inbox_ids') reserved WHERE reserved.value=i.id)
                    GROUP BY i.session_id HAVING COUNT(*)>=COALESCE(l.value,?) OR
                    COALESCE(MAX(s.last_submitted_at),MIN(i.received_at))<=?
                )
                SELECT r.session_id,r.batch_size,r.job_id FROM ready r LEFT JOIN extraction_metrics m ON m.session_id=r.session_id
                ORDER BY COALESCE(m.last_started_at,0),r.session_id,r.job_id LIMIT 1
            """, (batch_size,now,now,now,batch_size,json.dumps(batch_sizes or {}),now,batch_size,now-max_wait_seconds)).fetchone()
            if ready is None:
                return None
            session_id, selected_batch_size, job_id = ready
            connection.execute("INSERT INTO extraction_metrics(session_id,last_started_at) VALUES(?,?) "
                               "ON CONFLICT(session_id) DO UPDATE SET last_started_at=excluded.last_started_at", (session_id,now))
            if job_id is not None:
                if claim:
                    connection.execute("UPDATE extraction_jobs SET lease_until=? WHERE id=?", (now+240,job_id))
                return dict(connection.execute("SELECT * FROM extraction_jobs WHERE id=?", (job_id,)).fetchone())
            items = connection.execute(
                "SELECT * FROM tag_inbox i WHERE session_id=? AND NOT EXISTS("
                "SELECT 1 FROM extraction_jobs j,json_each(j.payload,'$.inbox_ids') reserved WHERE reserved.value=i.id) "
                "ORDER BY id LIMIT ?", (session_id,selected_batch_size)).fetchall()
            previous = connection.execute("SELECT last_batch_id FROM extraction_state WHERE session_id=?", (session_id,)).fetchone()
            job_id = uuid.uuid4().hex
            payload_data = {"inbox_ids":[row["id"] for row in items],"messages":[json.loads(row["payload"]) for row in items],
                                  "previous_batch_id":previous[0] if previous else None}
            self._freeze_snapshot(connection, session_id, payload_data, "job_creation")
            payload = json.dumps(payload_data, ensure_ascii=False)
            connection.execute("INSERT INTO extraction_jobs(id,session_id,payload,lease_until) VALUES(?,?,?,?)",
                               (job_id,session_id,payload,now+240 if claim else 0))
            return dict(connection.execute("SELECT * FROM extraction_jobs WHERE id=?", (job_id,)).fetchone())

    @staticmethod
    def _freeze_snapshot(connection: sqlite3.Connection, session_id: str, payload: Dict[str, Any], origin: str) -> None:
        previous_id = payload["previous_batch_id"]
        context = TagMemoryStore._batch_context(connection, session_id, previous_id) if previous_id else []
        # Older context explains ellipses, but its IDs must never look like permitted continuation IDs.
        previous_tags = next((batch["tags"] for batch in context if batch["id"] == previous_id), [])
        snapshot = {
            "previous_batches": [{"summary": batch["summary"], "tags": [
                {key: tag[key] for key in ("name", "summary", "category_path")} for tag in batch["tags"]]}
                for batch in context if batch["id"] != previous_id][-3:],
            "previous_extraction_tags": previous_tags,
        }
        payload["reference_snapshot"] = snapshot
        payload["snapshot_id"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        payload["snapshot_origin"] = origin

    def freeze_extraction_job(self, job_id: str) -> Dict[str, Any]:
        """Upgrade a legacy job once; retries never re-read mutable tags or context."""
        with self._connect(write=True) as connection:
            row = connection.execute("SELECT * FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("提取任务不存在")
            payload = json.loads(row["payload"])
            if "reference_snapshot" not in payload:
                self._freeze_snapshot(connection, row["session_id"], payload, "legacy_first_resume")
                connection.execute("UPDATE extraction_jobs SET payload=? WHERE id=?",
                                   (json.dumps(payload, ensure_ascii=False), job_id))
            result = dict(row)
            result["payload"] = json.dumps(payload, ensure_ascii=False)
            return result

    def record_job_event(self, session_id: str, job_id: str, event: str, detail: Dict[str, Any]) -> bool:
        with self._connect(write=True) as connection:
            if detail.get("request_id") and connection.execute(
                "SELECT 1 FROM extraction_events WHERE session_id=? AND job_id=? AND event=? "
                "AND json_extract(detail,'$.request_id')=?",
                (session_id, job_id, event, detail["request_id"]),
            ).fetchone():
                return False
            connection.execute("INSERT INTO extraction_events(session_id,job_id,event,created_at,detail) VALUES(?,?,?,?,?)",
                               (session_id, job_id, event, time.time(), json.dumps(detail, ensure_ascii=False)))
            return True

    def job_trace(self, session_id: str, job_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT event,created_at,detail FROM extraction_events "
                                      "WHERE session_id=? AND job_id=? ORDER BY sequence", (session_id, job_id)).fetchall()
            job = connection.execute("SELECT * FROM extraction_jobs WHERE session_id=? AND id=?", (session_id, job_id)).fetchone()
            batch = connection.execute("SELECT id FROM batches WHERE session_id=? AND external_id=?",
                                       (session_id, "extract:" + job_id)).fetchone()
            if not rows and job is None and batch is None:
                raise ValueError("当前会话不存在该任务")
            events = [{"event": row["event"], "created_at": row["created_at"], **json.loads(row["detail"])} for row in rows]
            usage = [event for event in events if event["event"] == "model_usage"]
            requested = {event["request_id"] for event in events if event["event"] == "model_request"}
            measured = {event["request_id"] for event in usage}
            return {"job_id": job_id, "status": job["status"] if job else "completed" if batch else "unknown",
                    "batch_id": batch[0] if batch else None, "messages_consumed": batch is not None,
                    "error": job["error"] if job else None, "failure_attempts": job["attempts"] if job else None,
                    "recovery": f"POST /api/webui/tag-memory/{session_id}/extraction-jobs/{job_id}/retry" if job else None,
                    "requests_with_usage": len(usage), "unknown_usage_requests": len(requested - measured),
                    "usage_scope": "returned_responses_only",
                    "unmeasured_provider_attempt_ids": sorted({attempt["id"] for item in usage
                        for attempt in item.get("provider_attempts", [])[:-1]}),
                    "elapsed_seconds": sum(event["elapsed_seconds"] for event in events if event["event"] == "attempt_finished"),
                    "usage": {key: sum(item[key] for item in usage) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                    "events": events}

    def get_pair_cache(self, session_id: str, cache_key: str) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute("SELECT output FROM conflict_pair_cache WHERE session_id=? AND cache_key=? AND created_at>?",
                                     (session_id, cache_key, time.time() - 14 * DAY_SECONDS)).fetchone()
            return row[0] if row else None

    def cache_pair_result(self, session_id: str, cache_key: str, output: str) -> None:
        with self._connect(write=True) as connection:
            connection.execute("DELETE FROM conflict_pair_cache WHERE created_at<=?", (time.time() - 14 * DAY_SECONDS,))
            connection.execute("INSERT OR REPLACE INTO conflict_pair_cache VALUES(?,?,?,?)",
                               (session_id, cache_key, output, time.time()))

    def record_extraction_metrics(self, session_id: str, elapsed: float, success: bool) -> None:
        with self._connect(write=True) as connection:
            connection.execute("UPDATE extraction_metrics SET completed=completed+?,failed=failed+?, "
                               "total_seconds=total_seconds+?,last_seconds=? WHERE session_id=?",
                               (int(success), int(not success), elapsed, elapsed, session_id))

    def record_model_request(self, session_id: str, *, conflict: bool = False) -> None:
        column = "conflict_requests" if conflict else "extraction_requests"
        with self._connect(write=True) as connection:
            connection.execute(f"UPDATE extraction_metrics SET {column}={column}+1 WHERE session_id=?", (session_id,))

    def get_conflict_chunk(self, job_id: str, cache_key: str) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute("SELECT output FROM extraction_conflict_chunks WHERE job_id=? AND cache_key=?",
                                     (job_id, cache_key)).fetchone()
            return row[0] if row else None

    def cache_conflict_chunk(self, job_id: str, cache_key: str, output: str) -> None:
        with self._connect(write=True) as connection:
            connection.execute("INSERT OR REPLACE INTO extraction_conflict_chunks VALUES(?,?,?)", (job_id, cache_key, output))

    def extraction_status(self, session_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            queue = connection.execute("SELECT COUNT(*),MIN(received_at) FROM tag_inbox WHERE session_id=?",
                                       (session_id,)).fetchone()
            job = connection.execute("SELECT id,attempts,error,retry_at,lease_until,status FROM extraction_jobs WHERE session_id=? ORDER BY (lease_until>0) DESC,(status='pending') DESC,rowid DESC",
                                     (session_id,)).fetchone()
            metrics = connection.execute("SELECT * FROM extraction_metrics WHERE session_id=?", (session_id,)).fetchone()
            return {"pending_messages": queue[0], "oldest_received_at": queue[1],
                    "job": dict(job) if job else None, "metrics": dict(metrics) if metrics else None}

    def cache_extraction_output(self, job_id: str, output: str) -> None:
        with self._connect(write=True) as connection:
            cursor = connection.execute("UPDATE extraction_jobs SET model_output=? WHERE id=?", (output, job_id))
            if cursor.rowcount != 1:
                raise ValueError("提取任务不存在")

    def fail_extraction_job(self, job_id: str, error: str, *, invalid_output: bool, invalid_conflict: bool = False, terminal: bool = False) -> None:
        if invalid_output:
            self.remove_job_cache(job_id, "output")
        if invalid_output or invalid_conflict:
            self.remove_job_cache(job_id, "conflicts")
        previous = self.read_job_cache(job_id, "failures")
        # 失败计数在 SQLite 锁之外持久化，避免失败状态写入受阻时继续消耗 API。
        if previous is None:
            with self._connect() as connection:
                row = connection.execute("SELECT attempts FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise ValueError("提取任务不存在")
                previous = {"attempts": row[0]}
        attempts = previous["attempts"] + 1
        status = "failed" if terminal or attempts >= MAX_EXTRACTION_ATTEMPTS else "pending"
        retry_at = time.time() + min(60 * 2 ** min(attempts - 1, 10), 3600)
        self.write_job_cache(job_id, "failures", {
            "attempts": attempts, "status": status, "retry_at": retry_at, "error": error,
            "invalid_output": invalid_output, "invalid_conflict": invalid_output or invalid_conflict,
        })
        with self._connect(write=True) as connection:
            row = connection.execute("SELECT attempts FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("提取任务不存在")
            connection.execute("UPDATE extraction_jobs SET error=?, attempts=?, retry_at=?, status=?, lease_until=0, "
                               "model_output=CASE WHEN ? THEN NULL ELSE model_output END, "
                               "conflict_output=CASE WHEN ? THEN NULL ELSE conflict_output END WHERE id=?",
                               (error, attempts, retry_at, status,
                                invalid_output, invalid_output or invalid_conflict, job_id))

    def defer_extraction_job(self, job_id: str) -> None:
        with self._connect(write=True) as connection:
            connection.execute("UPDATE extraction_jobs SET retry_at=?,lease_until=0 WHERE id=?", (time.time() + 60, job_id))

    def list_extraction_jobs(self, session_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id,status,attempts,retry_at,error FROM extraction_jobs WHERE session_id=? ORDER BY rowid",
                (session_id,))]

    def retry_extraction_job(self, session_id: str, job_id: str) -> None:
        with self._connect(write=True) as connection:
            row=connection.execute("SELECT lease_until,status,error FROM extraction_jobs WHERE session_id=? AND id=?",(session_id,job_id)).fetchone()
            if row is None:
                raise ValueError("当前群不存在该提取任务")
            if row["lease_until"] > time.time():
                raise StaleTagConflict("提取任务正在执行，请等待完成或租约到期")
            if row["status"] != "failed" and not row["error"]:
                raise ValueError("提取任务尚未失败，不需要人工重试")
            connection.execute("UPDATE extraction_jobs SET status='pending',attempts=0,retry_at=0,error='',resumed=1,lease_until=0 "
                               "WHERE id=? AND session_id=?",(job_id,session_id))
            connection.execute("INSERT INTO extraction_events(session_id,job_id,event,created_at,detail) VALUES(?,?,?,?,?)",
                               (session_id, job_id, "recovery_requested", time.time(), json.dumps({
                                   "previous_error": row["error"], "messages_consumed": False,
                                   "reference_snapshot_preserved": True}, ensure_ascii=False)))
        self.remove_job_cache(job_id,"failures")
        self.remove_job_cache(job_id,"repair")

    def _job_cache_path(self, job_id: str, kind: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id) or kind not in {"output", "conflicts", "failures", "repair", "template", "usage"}:
            raise ValueError("任务缓存标识无效")
        return self.db_path.parent / (self.db_path.name + ".job-cache") / f"{job_id}.{kind}.json"

    def read_job_cache(self, job_id: str, kind: str) -> Any:
        path = self._job_cache_path(job_id, kind)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write_job_cache(self, job_id: str, kind: str, value: Any) -> None:
        # SQLite 被锁时仍先持久化模型结果；原子替换确保重启后不重复生成已成功输出。
        path = self._job_cache_path(job_id, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def remove_job_cache(self, job_id: str, kind: str) -> None:
        self._job_cache_path(job_id, kind).unlink(missing_ok=True)

    @staticmethod
    def _check_conflict_versions(connection: sqlite3.Connection, scope: str, versions: Dict[str, int],
                                 categories: Optional[List[List[str]]] = None) -> None:
        if categories is not None:
            relevant = {row["id"] for row in connection.execute(
                "SELECT t.id,c.path FROM tags t JOIN categories c ON c.id=t.category_id "
                "WHERE t.session_id=? AND t.status NOT IN ('deleted','superseded')", (scope,))
                if json.loads(row["path"]) in categories}
            if relevant != set(versions):
                raise StaleTagConflict("冲突检查期间同分类候选集合发生变化，需要重新判断")
        for tag_id, revision in versions.items():
            row = connection.execute("SELECT revision,status FROM tags WHERE session_id=? AND id=?", (scope, tag_id)).fetchone()
            if row is None or row[0] != revision or row[1] in {"deleted", "superseded"}:
                raise StaleTagConflict("冲突检查期间标签被修改，需要重新判断")

    def cache_conflict_output(self, job_id: str, decisions: List[Dict[str, Any]],
                              checked_tags: Optional[Dict[str, int]] = None, *, rule_signature: Optional[str] = None,
                              categories: Optional[List[List[str]]] = None) -> None:
        with self._connect(write=True) as connection:
            job = connection.execute("SELECT session_id FROM extraction_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise ValueError("冲突裁决任务不存在")
            versions = checked_tags if checked_tags is not None else {
                item["old_tag_id"]: item.get("old_revision", 0) for item in decisions}
            self._check_conflict_versions(connection, job[0], versions, categories)
            for decision in decisions:
                old = connection.execute("SELECT revision,status FROM tags WHERE id=? AND session_id=?",
                                         (decision["old_tag_id"], job[0])).fetchone()
                if old is None or old[0] != decision.get("old_revision", 0) or old[1] in {"superseded", "deleted"}:
                    raise StaleTagConflict("冲突检查期间标签被修改，需要重新判断")
            cursor = connection.execute("UPDATE extraction_jobs SET conflict_output=? WHERE id=?",
                                        (json.dumps({"decisions": decisions, "checked_tags": versions, "rule_signature": rule_signature, "categories": categories}, ensure_ascii=False), job_id))
            if cursor.rowcount != 1:
                raise ValueError("冲突裁决任务不存在")

    def list_person_user_ids(self, session_id: str) -> List[str]:
        with self._connect() as connection:
            return [row[0] for row in connection.execute(
                "SELECT DISTINCT json_extract(c.path,'$[1]') FROM categories c JOIN tags t ON t.category_id=c.id "
                "WHERE t.session_id=? AND t.status='valid' AND json_extract(c.path,'$[0]')='人物'", (session_id,))]

    def list_current_tags(
        self, session_id: str, *, person_users: Optional[List[str]] = None, limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            self._expire_manual_activity(connection, time.time(), session_id)
            result = []
            query = ("SELECT t.*,c.path FROM tags t JOIN categories c ON c.id=t.category_id "
                     "WHERE t.session_id=? AND t.status NOT IN ('superseded','deleted')")
            parameters: List[Any] = [session_id]
            if person_users is not None:
                query += (" AND t.status='valid' AND json_extract(c.path,'$[0]')='人物' "
                          "AND json_extract(c.path,'$[1]') IN (SELECT value FROM json_each(?))")
                parameters.append(json.dumps(person_users))
            query += " ORDER BY t.created_at DESC"
            if limit is not None:
                query += " LIMIT ?"
                parameters.append(limit)
            for row in connection.execute(query, parameters):
                item = dict(row)
                item["category_path"] = json.loads(item.pop("path"))
                item["evidence"] = [dict(source) for source in connection.execute(
                    "SELECT message_id,quote FROM tag_sources WHERE tag_id=?", (item["id"],)
                )]
                result.append(item)
            return result

    def search_tags(self, session_id: str, text: str, user_ids: List[str], limit: int = 8) -> List[Dict[str, Any]]:
        """本地关键词/中文二字片段召回，活跃话题优先；不将检索次数当作真实性。"""
        def terms(value: str) -> set:
            words = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", value.lower()))
            for run in re.findall(r"[\u4e00-\u9fff]+", value):
                words.update(run[index:index + 2] for index in range(len(run) - 1))
            return words

        query = terms(text)
        ranked = []
        for item in self.list_current_tags(session_id):
            if item["status"] != "valid":
                continue
            path = item["category_path"]
            # 人物标签只召回当前参与者，避免无关人物偏好污染回复。
            if path[0] == "人物" and path[1] not in user_ids:
                continue
            overlap = len(query & terms(item["name"] + " " + item["summary"]))
            active = bool(item["is_active"] or item["auto_active"])
            person = path[0] == "人物"
            if overlap == 0 and not active and not (person and item["requires_long_term"]):
                continue
            ranked.append((overlap + 4 * active + 2 * person, item["created_at"], item))
        ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [entry[2] for entry in ranked[:max(0, min(limit, 20))]]

    def get_conflicts(self, session_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM tag_conflicts WHERE session_id=? ORDER BY created_at", (session_id,)
            )]

    @staticmethod
    def _audit_tag_change(connection: sqlite3.Connection, before: sqlite3.Row, reason: str) -> None:
        after = connection.execute("SELECT * FROM tags WHERE id=?", (before["id"],)).fetchone()
        connection.execute("INSERT INTO tag_revisions VALUES(?,?,?,?,?,?,?)", (
            uuid.uuid4().hex, before["id"], before["session_id"], json.dumps(dict(before), ensure_ascii=False),
            json.dumps(dict(after), ensure_ascii=False), reason, time.time(),
        ))

    def tag_detail(self, session_id: str, tag_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            self._expire_manual_activity(connection, time.time(), session_id)
            row = connection.execute("SELECT * FROM tags WHERE session_id=? AND id=? AND status!='deleted'",
                                     (session_id, tag_id)).fetchone()
            if row is None:
                raise ValueError("当前群不存在该标签")
            return {**dict(row), "evidence": [dict(item) for item in connection.execute(
                "SELECT message_id,quote FROM tag_sources WHERE tag_id=?", (tag_id,))],
                "revisions": [dict(item) for item in connection.execute(
                    "SELECT * FROM tag_revisions WHERE tag_id=? ORDER BY created_at DESC", (tag_id,))]}

    def edit_tag(self, session_id: str, tag_id: str, *, revision: int, reason: str,
                 name: str, summary: str, is_active: bool, requires_long_term: bool) -> None:
        if not reason.strip() or not name.strip() or not summary.strip() or len(name) > 120 or len(summary) > 600:
            raise ValueError("名称、摘要或修订理由无效")
        with self._connect(write=True) as connection:
            old = connection.execute("SELECT * FROM tags WHERE session_id=? AND id=? AND status!='deleted'",
                                     (session_id, tag_id)).fetchone()
            if old is None:
                raise ValueError("当前群不存在该标签")
            if old["revision"] != revision:
                raise StaleTagConflict("标签已更新，请刷新后重试")
            if old["status"] != "valid" and is_active:
                raise ValueError("被替代或存疑的标签不能设为活跃，请先处理冲突")
            deadline = (old["manual_active_until"] if old["is_active"] and old["manual_active_until"] is not None
                        else time.time() + DAY_SECONDS) if is_active else None
            connection.execute("UPDATE tags SET name=?,summary=?,is_active=?,requires_long_term=?,"
                               "manual_active_until=?,last_active_at=?,revision=revision+1 WHERE id=?",
                               (name.strip(), summary.strip(), is_active, requires_long_term, deadline, time.time(), tag_id))
            self._audit_tag_change(connection, old, reason.strip())

    def delete_tag(self, session_id: str, tag_id: str, revision: int, reason: str) -> None:
        if not reason.strip():
            raise ValueError("删除必须填写理由")
        with self._connect(write=True) as connection:
            old = connection.execute("SELECT * FROM tags WHERE session_id=? AND id=? AND status!='deleted'",
                                     (session_id, tag_id)).fetchone()
            if old is None:
                raise ValueError("当前群不存在该标签")
            if old["revision"] != revision:
                raise StaleTagConflict("标签已更新，请刷新后重试")
            # 软删除立即停止检索与展示，原证据和人工记录按既有周期清理。
            connection.execute("UPDATE tags SET status='deleted',is_active=0,auto_active=0,manual_active_until=NULL,requires_long_term=0,"
                               "last_active_at=?,revision=revision+1 WHERE id=?", (time.time(), tag_id))
            self._audit_tag_change(connection, old, reason.strip())
            connection.execute("UPDATE topics SET is_active=0 WHERE session_id=? AND NOT EXISTS("
                               "SELECT 1 FROM topic_tags tt JOIN tags t ON t.id=tt.tag_id WHERE tt.topic_id=topics.id "
                               "AND t.status='valid' AND (t.is_active=1 OR t.auto_active=1))", (session_id,))

    def resolve_conflict(self, session_id: str, conflict_id: str, decision: str, reason: str,
                         expected_resolved_at: Optional[float] = None) -> None:
        if decision not in {"replace_old", "keep_old", "uncertain"} or not reason.strip():
            raise ValueError("人工裁决动作或理由无效")
        with self._connect(write=True) as connection:
            conflict = connection.execute("SELECT * FROM tag_conflicts WHERE session_id=? AND id=?",
                                          (session_id, conflict_id)).fetchone()
            if conflict is None:
                raise ValueError("当前群不存在该冲突，或已被更新的裁决替代")
            if conflict["resolved_at"] != expected_resolved_at:
                raise StaleTagConflict("冲突已有新的人工裁决，请刷新后重试")
            old_id, new_id = conflict["old_tag_id"], conflict["new_tag_id"]
            related = [dict(row) for row in connection.execute(
                "SELECT * FROM tag_conflicts WHERE session_id=? AND batch_id=?", (session_id, conflict["batch_id"]))]
            # 人工修改一个标签对时重新聚合它的整组关系，不能让其它合法裁决失效。
            component = []
            ids = {old_id, new_id}
            while True:
                matched = [row for row in related if row["old_tag_id"] in ids or row["new_tag_id"] in ids]
                if not matched:
                    break
                for row in matched:
                    related.remove(row)
                    component.append(row)
                    ids.update((row["old_tag_id"], row["new_tag_id"]))
            if None in ids:
                raise ValueError("冲突标签已清理，不能覆盖历史裁决")
            new_indices = {value: index for index, value in enumerate(sorted({row["new_tag_id"] for row in component}))}
            decisions = [{"new_index": new_indices[row["new_tag_id"]], "old_tag_id": row["old_tag_id"],
                          "decision": decision if row["id"] == conflict_id else row["decision"],
                          "reason": reason if row["id"] == conflict_id else row["reason"]} for row in component]
            new_statuses = validate_conflict_pairs(decisions, len(new_indices))
            statuses = {tag_id: new_statuses[index] for tag_id, index in new_indices.items()}
            rank = {"valid": 0, "disputed": 1, "superseded": 2}
            for row, item in zip(component, decisions, strict=True):
                status = new_statuses[item["new_index"]]
                original = json.loads(row["evidence"]).get("old_status_before", "valid")
                old_status = ("superseded" if status == "valid" else "disputed" if status == "disputed" else original
                              ) if item["decision"] == "replace_old" else (
                    "valid" if item["decision"] == "keep_old" else "disputed" if status != "superseded" else original)
                prior = statuses.get(row["old_tag_id"], old_status)
                statuses[row["old_tag_id"]] = max((prior, old_status), key=rank.__getitem__)
            for tag_id, status in statuses.items():
                before = connection.execute("SELECT * FROM tags WHERE session_id=? AND id=? AND status!='deleted'",
                                            (session_id, tag_id)).fetchone()
                if before is None:
                    raise ValueError("冲突标签已清理或删除，不能覆盖历史裁决")
                connection.execute("UPDATE tags SET status=?,is_active=0,auto_active=0,manual_active_until=NULL,last_active_at=?,"
                                   "revision=revision+1 WHERE id=?", (status, time.time(), tag_id))
                self._audit_tag_change(connection, before, "人工冲突裁决：" + reason.strip())
            for row in component:
                evidence = json.loads(row["evidence"])
                evidence["applied_old_status"] = statuses[row["old_tag_id"]]
                evidence["applied_new_status"] = statuses[row["new_tag_id"]]
                connection.execute("UPDATE tag_conflicts SET evidence=? WHERE id=?",
                                   (json.dumps(evidence, ensure_ascii=False), row["id"]))
            connection.execute("UPDATE tag_conflicts SET decision=?,reason=?,manual_decision=?,manual_reason=?,resolved_at=? WHERE id=?",
                               (decision, reason.strip(), decision, reason.strip(), time.time(), conflict_id))
            connection.execute("UPDATE topics SET is_active=0 WHERE session_id=? AND id IN "
                               "(SELECT topic_id FROM topic_tags WHERE tag_id IN (?,?))", (session_id, old_id, new_id))

    def set_topic_active(self, session_id: str, topic_id: str, active: bool) -> None:
        with self._connect(write=True) as connection:
            topic = connection.execute("SELECT id FROM topics WHERE session_id=? AND id=?", (session_id, topic_id)).fetchone()
            if topic is None:
                raise ValueError("当前群不存在该话题")
            members = connection.execute("SELECT t.* FROM tags t JOIN topic_tags tt ON tt.tag_id=t.id "
                                         "WHERE tt.topic_id=? AND t.session_id=? AND t.status='valid'", (topic_id, session_id)).fetchall()
            if active and not members:
                raise ValueError("该话题没有有效标签，不能激活")
            connection.execute("UPDATE topics SET is_active=?,last_active_at=? WHERE id=?", (active, time.time(), topic_id))
            for member in members:
                connection.execute("UPDATE tags SET is_active=?,auto_active=?,manual_active_until=?,"
                                   "last_active_at=?,revision=revision+1 WHERE id=?",
                                   (active, member["auto_active"] if active else 0,
                                    time.time() + DAY_SECONDS if active else None, time.time(), member["id"]))
                self._audit_tag_change(connection, member, "人工激活话题" if active else "人工结束话题")

    def ensure_scope(self, session_id: str, session_name: str) -> None:
        """为真实会话创建稳定分类骨架，不需要预先生成标签。"""
        if not session_id.strip() or not session_name.strip():
            raise ValueError("会话 ID 和显示名称不能为空")
        with self._connect(write=True) as connection:
            for branch in ("人物", "群公共"):
                for category in ("偏好", "事件", "与机器人的互动", "群活动"):
                    self._category(connection, session_id, (branch, category), session_name)

    def get_tree(self, session_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            self._expire_manual_activity(connection, time.time(), session_id)
            categories = connection.execute("SELECT * FROM categories WHERE session_id=?", (session_id,)).fetchall()
            nodes = {row["id"]: {"id": row["id"], "name": row["name"], "children": [], "tags": []}
                     for row in categories}
            root: Dict[str, Any] = {}
            for row in categories:
                if row["parent_id"] is None:
                    root = nodes[row["id"]]
                else:
                    nodes[row["parent_id"]]["children"].append(nodes[row["id"]])
            for row in connection.execute(
                "SELECT * FROM tags WHERE session_id=? AND status!='deleted' ORDER BY created_at, id", (session_id,)
            ):
                tag = dict(row)
                tag["is_active"] = bool(row["is_active"] or row["auto_active"])
                tag["source_message_ids"] = [source[0] for source in connection.execute(
                    "SELECT message_id FROM tag_sources WHERE tag_id=?", (row["id"],)
                )]
                tag["category_path"] = json.loads(next(category["path"] for category in categories if category["id"] == row["category_id"]))
                nodes[row["category_id"]]["tags"].append(tag)
            return root

    def get_batch_context(self, session_id: str, batch_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            return self._batch_context(connection, session_id, batch_id)

    @staticmethod
    def _batch_context(connection: sqlite3.Connection, session_id: str, batch_id: str) -> List[Dict[str, Any]]:
        rows = connection.execute("""
            SELECT b.* FROM batches b WHERE b.session_id=? AND b.archived=0 AND
            (b.id=? OR b.id IN (SELECT previous_id FROM batch_links WHERE session_id=? AND current_id=?) OR
             b.id IN (SELECT current_id FROM batch_links WHERE session_id=? AND previous_id=?))
            ORDER BY b.rowid
        """, (session_id, batch_id, session_id, batch_id, session_id, batch_id)).fetchall()
        result = []
        for row in rows:
            batch = dict(row)
            batch["tags"] = [
                {**dict(tag), "category_path": json.loads(tag["path"])}
                for tag in connection.execute("""
                    SELECT t.id, t.name, t.summary, (t.is_active OR t.auto_active) AS is_active,
                    t.requires_long_term, t.status, t.revision, c.path
                    FROM tags t JOIN categories c ON c.id=t.category_id
                    WHERE t.session_id=? AND t.batch_id=? AND t.status='valid' ORDER BY t.created_at, t.id
                """, (session_id, row["id"]))
            ]
            result.append(batch)
        return result

    def get_topics(self, session_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            self._expire_manual_activity(connection, time.time(), session_id)
            topics = []
            for row in connection.execute("SELECT * FROM topics WHERE session_id=?", (session_id,)):
                topic = dict(row)
                topic["tag_ids"] = [item[0] for item in connection.execute(
                    "SELECT tag_id FROM topic_tags WHERE session_id=? AND topic_id=?", (session_id, row["id"])
                )]
                topics.append(topic)
            return topics

    def update_tag(
        self, session_id: str, tag_id: str, *, is_active: Optional[bool] = None,
        requires_long_term: Optional[bool] = None, now: Optional[float] = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self._connect(write=True) as connection:
            cursor = connection.execute("""
                UPDATE tags SET is_active=COALESCE(?, is_active), requires_long_term=COALESCE(?, requires_long_term),
                manual_active_until=CASE WHEN ? IS NULL THEN manual_active_until WHEN ?=1 THEN ? ELSE NULL END,
                revision=revision+1,
                last_active_at=MAX(last_active_at, ?) WHERE session_id=? AND id=?
            """, (is_active, requires_long_term, is_active, is_active, timestamp + DAY_SECONDS, timestamp, session_id, tag_id))
            if cursor.rowcount != 1:
                raise ValueError("当前会话不存在该标签")

    def cleanup_if_due(
        self, *, retention_days: int = 14, interval_days: int = 14, now: Optional[float] = None,
    ) -> Dict[str, Any]:
        if retention_days < 1 or interval_days < 1:
            raise ValueError("清理周期和保留天数必须至少为 1 天")
        timestamp = time.time() if now is None else now
        with self._connect(write=True) as connection:
            self._expire_manual_activity(connection, timestamp)
            last_run = connection.execute("SELECT last_run_at FROM maintenance WHERE name='cleanup'").fetchone()
            if last_run and timestamp < last_run[0] + interval_days * DAY_SECONDS:
                return {"ran": False, "deleted_tags": 0, "deleted_topics": 0, "archived_batches": 0}
            cutoff = timestamp - retention_days * DAY_SECONDS
            deleted = connection.execute(
                # 失败任务仅保护引用的上一批，不无限保护该群全部标签；冲突快照失效会重新检查。
                "DELETE FROM tags WHERE is_active=0 AND auto_active=0 AND requires_long_term=0 AND last_active_at<=? "
                "AND batch_id NOT IN (SELECT json_extract(payload,'$.previous_batch_id') FROM extraction_jobs "
                "WHERE json_extract(payload,'$.previous_batch_id') IS NOT NULL)", (cutoff,)
            ).rowcount
            deleted_topics = connection.execute(
                "DELETE FROM topics WHERE is_active=0 AND last_active_at<=?", (cutoff,)
            ).rowcount
            # 冲突的另一侧仍保留时，不让审计快照无限期保留已清理标签的原文。
            for conflict in connection.execute("SELECT id,old_tag_id,new_tag_id,evidence FROM tag_conflicts "
                                               "WHERE old_tag_id IS NULL OR new_tag_id IS NULL").fetchall():
                evidence = json.loads(conflict["evidence"])
                if conflict["old_tag_id"] is None:
                    evidence["old"] = {"name": "已清理标签", "summary": "相关信息已按周期清理", "sources": []}
                if conflict["new_tag_id"] is None:
                    evidence["new"] = {"name": "已清理标签", "summary": "相关信息已按周期清理", "evidence_quotes": {}}
                connection.execute("UPDATE tag_conflicts SET evidence=? WHERE id=?",
                                   (json.dumps(evidence, ensure_ascii=False), conflict["id"]))
            connection.execute("DELETE FROM tag_conflicts WHERE old_tag_id IS NULL AND new_tag_id IS NULL AND created_at<=?", (cutoff,))
            # 保留批次身份与指纹作为幂等账本；没有保留标签的过期批次移除摘要、证据和前后关联。
            obsolete = [row[0] for row in connection.execute("""
                SELECT id FROM batches WHERE archived=0 AND created_at<=? AND
                NOT EXISTS(SELECT 1 FROM tags WHERE tags.batch_id=batches.id)
            """, (cutoff,))]
            for batch_id in obsolete:
                connection.execute("DELETE FROM batch_sources WHERE batch_id=?", (batch_id,))
                connection.execute("DELETE FROM batch_links WHERE previous_id=? OR current_id=?", (batch_id, batch_id))
                connection.execute("UPDATE batches SET summary='', archived=1 WHERE id=?", (batch_id,))
            connection.execute(
                "INSERT INTO maintenance VALUES('cleanup', ?) "
                "ON CONFLICT(name) DO UPDATE SET last_run_at=excluded.last_run_at",
                (timestamp,),
            )
            return {"ran": True, "deleted_tags": deleted,
                    "deleted_topics": deleted_topics, "archived_batches": len(obsolete)}
