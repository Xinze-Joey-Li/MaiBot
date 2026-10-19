"""Bounded observation writer, independent of chat and Decision Replay databases.

configure/delete/close are control-plane operations: call them off the event loop.
submit never takes the database/commit lock. Revocation and deletion invalidate
queued generations before waiting for an in-flight transaction to finish.
"""

from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, RLock, Thread
from typing import Any, Dict, List, Optional, Set, Tuple
import json
import logging
import sqlite3
import time
import uuid

from .models import Batch, Policy, SCHEMA_VERSION

logger = logging.getLogger("decision_observation")


class ObservationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.run_id = uuid.uuid4().hex
        self._policy_state = (0, Policy())
        self._session_epochs: Dict[str, int] = {}
        self._deleted_sessions: Set[str] = set()
        self._commit_lock = RLock()
        self._control_lock = Lock()
        self._closed = Event()
        self._worker: Optional[Thread] = None
        self.queue: Queue = Queue(maxsize=self.policy.queue_capacity)
        self.submitted = 0
        self.committed = 0
        self.dropped = 0
        self.revoked = 0
        self.errors = 0
        self.write_errors = 0
        self.last_error: Optional[str] = None
        self._warned_at = 0.0
        self._last_cleanup = 0.0
        self._initialized = False
        self._accepting = True

    @property
    def policy(self) -> Policy:
        return self._policy_state[1]

    def generation(self, session_id: str) -> Optional[Tuple[int, int]]:
        epoch, policy = self._policy_state
        if (
            self._closed.is_set()
            or not policy.enabled
            or session_id in self._deleted_sessions
            or session_id not in policy.authorized_session_ids
        ):
            return None
        return epoch, self._session_epochs.get(session_id, 0)

    def configure(self, policy: Policy) -> None:
        """Return only after old-policy commits stop; queue admission stops immediately."""
        with self._control_lock:
            if policy == self.policy:
                return
            if self._closed.is_set():
                raise RuntimeError("Observation store is closed; create a new instance")
            # Publish invalidation before waiting for SQLite. submit has no I/O locks.
            previous = self.policy
            self._policy_state = (self._policy_state[0] + 1, policy)
            with self._commit_lock:
                if previous.enabled and self.path.exists():
                    try:
                        with closing(self._connect()) as connection, connection:
                            connection.execute(
                                "UPDATE opportunities SET status='interrupted',finalized_at=? "
                                "WHERE status='pending' AND run_id=? AND policy_version=?",
                                (time.time(), self.run_id, previous.version),
                            )
                    except Exception:
                        self.errors += 1
                        self._warning("policy_finalize_failed")
                # A live queue is not resized: its bound is enforced additionally at admission.
                if self._worker is None:
                    self.queue = Queue(maxsize=policy.queue_capacity)
                if policy.enabled and self._worker is None:
                    self._worker = Thread(target=self._run, name="decision-observation-writer", daemon=True)
                    self._worker.start()

    def submit(self, batch: Batch) -> bool:
        """Best effort admission: no database, waits, retries or log payloads."""
        if not self._accepting or self.generation(batch.session_id) != batch.generation:
            self.revoked += 1
            return False
        try:
            if len(batch.messages) > 512 or self.queue.qsize() >= self.policy.queue_capacity:
                raise Full
            self.queue.put_nowait(batch)
            self.submitted += 1
            return True
        except Full:
            self.dropped += 1
            self._warning("queue_full")
            return False

    def _warning(self, code: str) -> None:
        self.last_error = code
        if time.monotonic() - self._warned_at >= 60:
            self._warned_at = time.monotonic()
            logger.warning("Decision Observation 降级: %s | dropped=%s errors=%s", code, self.dropped, self.errors)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=0.1)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("Unsupported observation schema")
            if version == 0:
                connection.executescript("""
                BEGIN;
                CREATE TABLE message_events (
                    message_event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    platform_message_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                    event_timestamp REAL NOT NULL, received_timestamp REAL, run_id TEXT NOT NULL,
                    source_type TEXT NOT NULL, schema_version INTEGER NOT NULL,
                    UNIQUE(session_id,platform_message_id));
                CREATE INDEX messages_session_time ON message_events(session_id,event_timestamp);
                CREATE INDEX messages_sender_time ON message_events(sender_id,event_timestamp);
                CREATE TABLE opportunities (
                    opportunity_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at REAL NOT NULL,
                    run_id TEXT NOT NULL, policy_version TEXT NOT NULL, source_type TEXT NOT NULL,
                    finalized_at REAL, status TEXT NOT NULL);
                CREATE INDEX opportunities_session_time ON opportunities(session_id,created_at);
                CREATE INDEX opportunities_status_time ON opportunities(status,created_at);
                CREATE TABLE opportunity_messages (
                    opportunity_id TEXT NOT NULL REFERENCES opportunities ON DELETE CASCADE,
                    message_event_id TEXT NOT NULL REFERENCES message_events ON DELETE CASCADE,
                    role TEXT NOT NULL, PRIMARY KEY(opportunity_id,message_event_id,role));
                CREATE INDEX links_message ON opportunity_messages(message_event_id);
                CREATE TABLE evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    opportunity_id TEXT NOT NULL REFERENCES opportunities ON DELETE CASCADE,
                    evaluation_sequence INTEGER NOT NULL, evaluated_at REAL NOT NULL,
                    feature_version TEXT NOT NULL, feature_snapshot TEXT NOT NULL,
                    effective_threshold REAL, calculated_probability REAL, gate_result TEXT NOT NULL,
                    decision_reason TEXT NOT NULL, weights_hash TEXT, evidence TEXT NOT NULL,
                    UNIQUE(opportunity_id,evaluation_sequence));
                CREATE INDEX evaluations_result_time ON evaluations(gate_result,evaluated_at);
                CREATE TABLE outcomes (
                    opportunity_id TEXT PRIMARY KEY REFERENCES opportunities ON DELETE CASCADE,
                    gate_result TEXT NOT NULL DEFAULT 'not_observed',
                    planner_status TEXT NOT NULL DEFAULT 'not_observed',
                    replyer_status TEXT NOT NULL DEFAULT 'not_observed',
                    send_status TEXT NOT NULL DEFAULT 'not_observed',
                    replay_turn_id TEXT, linkage_status TEXT NOT NULL DEFAULT 'phase_2b_not_connected');
                PRAGMA user_version=1;
                COMMIT;
                """)
            if not self._initialized:
                with connection:
                    connection.execute(
                        "UPDATE opportunities SET status='interrupted',finalized_at=? "
                        "WHERE status='pending' AND run_id<>?",
                        (time.time(), self.run_id),
                    )
                self._initialized = True
            return connection
        except BaseException:
            connection.close()
            raise

    def _write(self, batches: List[Batch]) -> None:
        # The control plane waits on this lock in a thread, never on the message loop.
        with self._commit_lock:
            accepted = [b for b in batches if self.generation(b.session_id) == b.generation]
            self.revoked += len(batches) - len(accepted)
            if not accepted:
                return
            with closing(self._connect()) as connection, connection:
                for batch in accepted:
                    for message in batch.messages:
                        connection.execute(
                            "INSERT INTO message_events VALUES(?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(message_event_id) DO UPDATE SET "
                            "received_timestamp=COALESCE(message_events.received_timestamp,excluded.received_timestamp)",
                            tuple(asdict(message).values()),
                        )
                    opportunity = batch.opportunity
                    if opportunity is None:
                        continue
                    connection.execute(
                        "INSERT INTO opportunities VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(opportunity_id) DO UPDATE SET "
                        "status=excluded.status,finalized_at=excluded.finalized_at "
                        "WHERE opportunities.status='pending'",
                        tuple(asdict(opportunity).values()),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO outcomes(opportunity_id) VALUES(?)", (opportunity.opportunity_id,)
                    )
                    connection.executemany(
                        "INSERT OR IGNORE INTO opportunity_messages VALUES(?,?,'pending')",
                        [(opportunity.opportunity_id, m.message_event_id) for m in batch.messages],
                    )
                    if batch.evaluation is not None:
                        connection.execute(
                            "INSERT OR IGNORE INTO evaluations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            tuple(asdict(batch.evaluation).values()),
                        )
                        connection.execute(
                            "UPDATE outcomes SET gate_result=(SELECT gate_result FROM evaluations "
                            "WHERE opportunity_id=? ORDER BY evaluation_sequence DESC LIMIT 1) "
                            "WHERE opportunity_id=?",
                            (opportunity.opportunity_id, opportunity.opportunity_id),
                        )
                # A policy update can be published while this transaction is in flight.
                if any(self.generation(b.session_id) != b.generation for b in accepted):
                    connection.rollback()
                    self.revoked += len(accepted)
                    return
            self.committed += len(accepted)

    def _run(self) -> None:
        while not self._closed.is_set() or not self.queue.empty():
            batches: List[Batch] = []
            try:
                batches.append(self.queue.get(timeout=0.1))
            except Empty:
                if self.policy.enabled and time.monotonic() - self._last_cleanup > 3600:
                    try:
                        self.cleanup()
                    except Exception:
                        self.errors += 1
                        self._warning("cleanup_failed")
                    self._last_cleanup = time.monotonic()
                continue
            while len(batches) < 64:
                try:
                    batches.append(self.queue.get_nowait())
                except Empty:
                    break
            try:
                self._write(batches)
            except Exception:
                self.errors += 1
                self.write_errors += 1
                self.dropped += len(batches)
                self._warning("write_failed")
            finally:
                for _ in batches:
                    self.queue.task_done()
        # An orderly stop does not imply the bot executed any downstream stage.
        if self.policy.enabled and self.path.exists():
            try:
                with self._commit_lock, closing(self._connect()) as connection, connection:
                    connection.execute(
                        "UPDATE opportunities SET status='interrupted',finalized_at=? "
                        "WHERE status='pending' AND run_id=?",
                        (time.time(), self.run_id),
                    )
            except Exception:
                self.errors += 1
                self._warning("shutdown_finalize_failed")

    def flush(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return not self.queue.unfinished_tasks

    def close(self, timeout: float = 3.0) -> bool:
        """Bounded drain, then invalidate anything that could otherwise write late."""
        with self._control_lock:
            deadline = time.monotonic() + timeout
            self._accepting = False
            drained = self.flush(timeout)
            self._closed.set()
            if self._worker is not None:
                self._worker.join(timeout=max(0.0, deadline - time.monotonic()))
            return drained and (self._worker is None or not self._worker.is_alive())

    def cleanup(self, now: Optional[float] = None) -> None:
        with self._commit_lock:
            if not self.policy.enabled or self._closed.is_set():
                return
            cutoff = (time.time() if now is None else now) - self.policy.retention_days * 86400
            with closing(self._connect()) as connection, connection:
                connection.execute("DELETE FROM opportunities WHERE created_at<?", (cutoff,))
                connection.execute(
                    "DELETE FROM message_events WHERE event_timestamp<? AND NOT EXISTS "
                    "(SELECT 1 FROM opportunity_messages l WHERE l.message_event_id=message_events.message_event_id)",
                    (cutoff,),
                )

    def delete_session(self, session_id: str) -> None:
        """Delete and stop collecting this session for this store's lifetime.

        Keeping a tombstone prevents subsequent checks of old Runtime pending
        messages from recreating deleted data. A new store/run is required to resume.
        """
        with self._control_lock:
            self._deleted_sessions.add(session_id)
            self._session_epochs[session_id] = self._session_epochs.get(session_id, 0) + 1
            with self._commit_lock:
                if not self.path.exists():
                    return
                with closing(self._connect()) as connection, connection:
                    connection.execute("DELETE FROM opportunities WHERE session_id=?", (session_id,))
                    connection.execute("DELETE FROM message_events WHERE session_id=?", (session_id,))

    def query(self, session_id: str, limit: int = 100) -> Dict[str, Any]:
        """Read only: no file creation, migration, cleanup or guessed cross-session links."""
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        result: Dict[str, Any] = {"messages": [], "opportunities": [], "evaluations": [], "outcomes": []}
        if not self.path.exists():
            return result
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as connection:
            connection.row_factory = sqlite3.Row
            result["messages"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM message_events WHERE session_id=? ORDER BY event_timestamp DESC LIMIT ?",
                    (session_id, limit),
                )
            ]
            result["opportunities"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM opportunities WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                )
            ]
            for opportunity in result["opportunities"]:
                oid = opportunity["opportunity_id"]
                opportunity["message_event_ids"] = [
                    r[0]
                    for r in connection.execute(
                        "SELECT message_event_id FROM opportunity_messages WHERE opportunity_id=? ORDER BY message_event_id",
                        (oid,),
                    )
                ]
                for row in connection.execute(
                    "SELECT * FROM evaluations WHERE opportunity_id=? ORDER BY evaluation_sequence LIMIT ?",
                    (oid, limit),
                ):
                    item = dict(row)
                    item["feature_snapshot"] = json.loads(item["feature_snapshot"])
                    item["evidence"] = json.loads(item["evidence"])
                    result["evaluations"].append(item)
                result["outcomes"].extend(
                    dict(r) for r in connection.execute("SELECT * FROM outcomes WHERE opportunity_id=?", (oid,))
                )
        return result

    def health(self) -> Dict[str, Any]:
        return {
            "enabled": self.policy.enabled,
            "run_id": self.run_id,
            "queue_size": self.queue.qsize(),
            "queue_capacity": self.policy.queue_capacity,
            "submitted_batches": self.submitted,
            "committed_batches": self.committed,
            "dropped_batches": self.dropped,
            "revoked_batches": self.revoked,
            "errors": self.errors,
            "write_errors": self.write_errors,
            "last_error": self.last_error,
            "storage_initialized": self._initialized,
            "worker_alive": self._worker is not None and self._worker.is_alive(),
            "physical_queue_capacity": self.queue.maxsize,
            "closed": self._closed.is_set(),
        }
