"""Bounded observation writer, independent of chat and Decision Replay databases.

configure/delete/close are control-plane operations: call them off the event loop.
submit never takes the database/commit lock. Revocation and deletion invalidate
queued generations before waiting for an in-flight transaction to finish.
"""

from collections import OrderedDict
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, RLock, Thread
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple
import json
import logging
import sqlite3
import time
import uuid

from . import annotation_storage, delivery_store
from .models import Batch, Policy, ReceiptProbe, SCHEMA_VERSION, identity

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
        self._annotation_authorization_lock = RLock()
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
        self.delivery_bindings: OrderedDict = OrderedDict()
        self.delivery_unmatched = 0
        self.delivery_ambiguous = 0
        self.delivery_inline_receipt: Optional[Tuple[Any, Any]] = None
        self.delivery_current_only_sessions: Set[str] = set()

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
                if not policy.enabled and self.path.exists():
                    with self._commit_lock, closing(sqlite3.connect(
                        self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.1
                    )) as connection, connection:
                        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_scopes'").fetchone():
                            delivery_store.revoke(connection, set())
                return
            if self._closed.is_set():
                raise RuntimeError("Observation store is closed; create a new instance")
            # Publish invalidation before waiting for SQLite. submit has no I/O locks.
            previous = self.policy
            allowed = policy.authorized_session_ids if policy.enabled else frozenset()
            self.delivery_current_only_sessions.update(previous.authorized_session_ids - allowed)
            with self._annotation_authorization_lock:
                self._policy_state = (self._policy_state[0] + 1, policy)
            with self._commit_lock:
                if self.path.exists():
                    try:
                        with closing(self._connect()) as connection, connection:
                            current_only = {
                                sid: identity("delivery_scope", self.run_id, sid, str(self.generation(sid)))
                                for sid in self.delivery_current_only_sessions if self.generation(sid) is not None
                            }
                            delivery_store.revoke(connection,
                                                  set(policy.authorized_session_ids) if policy.enabled else set(),
                                                  current_only)
                            if previous.enabled:
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

    def _valid(self, item: Any) -> bool:
        if isinstance(item, ReceiptProbe):
            return bool(item.authorizations) and all(self.generation(sid) == gen for sid, gen in item.authorizations)
        return self.generation(item.session_id) == item.generation

    def submit_probe(self, probe: ReceiptProbe) -> bool:
        """Only bounded queue admission; all historical joins happen on the existing writer."""
        if not self._accepting or not self._valid(probe):
            self.revoked += 1
            return False
        try:
            if self.queue.qsize() >= self.policy.queue_capacity:
                raise Full
            self.queue.put_nowait(probe)
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

    def _connect(self, *, existing_only: bool = False, initialize_run: bool = True) -> sqlite3.Connection:
        if not existing_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.1)
                      if existing_only else sqlite3.connect(self.path, timeout=0.1))
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if existing_only and version == 0:
                raise ValueError("Observation database is uninitialized")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            if version not in (0, 1, 2, 3, 4, 5, SCHEMA_VERSION):
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
            if version < 2:
                connection.executescript("""
                BEGIN;
                CREATE TABLE execution_events (
                    event_id TEXT PRIMARY KEY,
                    opportunity_id TEXT NOT NULL REFERENCES opportunities ON DELETE CASCADE,
                    evaluation_id TEXT REFERENCES evaluations ON DELETE CASCADE,
                    execution_id TEXT NOT NULL, occurred_at REAL NOT NULL,
                    stage TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                    logical_turn_id TEXT, cycle_id TEXT, replay_status TEXT NOT NULL,
                    message_event_ids TEXT NOT NULL, tool_names TEXT NOT NULL,
                    link_source TEXT NOT NULL, wait_tool_call_id TEXT, parent_execution_id TEXT);
                CREATE INDEX execution_opportunity ON execution_events(opportunity_id,occurred_at);
                CREATE INDEX execution_turn ON execution_events(logical_turn_id,cycle_id);
                PRAGMA user_version=2;
                COMMIT;
                """)
            if version < 3:
                connection.executescript("""
                BEGIN;
                ALTER TABLE opportunities ADD COLUMN account_id TEXT;
                CREATE TABLE opportunity_transfers (
                    transfer_id TEXT PRIMARY KEY,
                    source_opportunity_id TEXT NOT NULL REFERENCES opportunities ON DELETE CASCADE,
                    target_opportunity_id TEXT NOT NULL REFERENCES opportunities ON DELETE CASCADE,
                    message_event_id TEXT NOT NULL REFERENCES message_events ON DELETE CASCADE,
                    execution_id TEXT NOT NULL, relation_type TEXT NOT NULL,
                    occurred_at REAL NOT NULL, account_id TEXT,
                    UNIQUE(source_opportunity_id,target_opportunity_id,message_event_id,execution_id,relation_type));
                CREATE INDEX transfers_target ON opportunity_transfers(target_opportunity_id);
                CREATE INDEX transfers_source ON opportunity_transfers(source_opportunity_id);
                CREATE TABLE priority_claim_checks (
                    check_id TEXT PRIMARY KEY,
                    message_event_id TEXT NOT NULL REFERENCES message_events ON DELETE CASCADE,
                    session_id TEXT NOT NULL, account_id TEXT, run_id TEXT NOT NULL,
                    checked_at REAL NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE INDEX checks_session ON priority_claim_checks(session_id,checked_at);
                PRAGMA user_version=3;
                COMMIT;
                """)
            if version < 4:
                connection.executescript("""
                BEGIN;
                CREATE TABLE reply_events (
                    event_id TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL,
                    parent_event_id TEXT NOT NULL REFERENCES execution_events ON DELETE CASCADE,
                    opportunity_id TEXT NOT NULL REFERENCES opportunities ON DELETE CASCADE,
                    evaluation_id TEXT REFERENCES evaluations ON DELETE CASCADE,
                    execution_id TEXT NOT NULL, flow_id TEXT NOT NULL, occurred_at REAL NOT NULL,
                    stage TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                    logical_turn_id TEXT, cycle_id TEXT, tool_call_id TEXT, reply_id TEXT,
                    generation_group_id TEXT, request_id TEXT, outbound_id TEXT, attempt_id TEXT,
                    detail TEXT NOT NULL);
                CREATE INDEX reply_opportunity ON reply_events(opportunity_id,occurred_at);
                CREATE INDEX reply_flow ON reply_events(flow_id,occurrence_id);
                CREATE INDEX reply_outbound ON reply_events(outbound_id,attempt_id);
                PRAGMA user_version=4;
                COMMIT;
                """)
            if version < 5:
                connection.executescript(delivery_store.MIGRATION)
            if version < 6:
                connection.executescript(annotation_storage.MIGRATION)
            if initialize_run and not self._initialized:
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

    def _facts(self, connection: sqlite3.Connection, batch: Batch) -> None:
        for message in batch.messages:
            connection.execute(
                "INSERT INTO message_events VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(message_event_id) DO UPDATE SET "
                "received_timestamp=COALESCE(message_events.received_timestamp,excluded.received_timestamp)",
                tuple(asdict(message).values()),
            )
        opportunity = batch.opportunity
        if opportunity is None:
            return
        connection.execute(
            "INSERT INTO opportunities VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(opportunity_id) DO UPDATE SET "
            "status=excluded.status,finalized_at=excluded.finalized_at "
            "WHERE opportunities.status='pending'",
            tuple(asdict(opportunity).values()),
        )
        connection.execute(
            "INSERT OR IGNORE INTO outcomes(opportunity_id,linkage_status) VALUES(?,'execution_not_observed')",
            (opportunity.opportunity_id,),
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
        if opportunity.source_type == "priority_at":
            connection.execute("UPDATE outcomes SET planner_status='not_applicable' WHERE opportunity_id=?",
                               (opportunity.opportunity_id,))

    def _write(self, batches: List[Any]) -> None:
        # The control plane waits on this lock in a thread, never on the message loop.
        with self._commit_lock:
            accepted = [b for b in batches if self._valid(b)]
            self.revoked += len(batches) - len(accepted)
            if not accepted:
                return
            with closing(self._connect()) as connection, connection:
                for batch in accepted:
                    if isinstance(batch, ReceiptProbe):
                        delivery_store.probe(self, connection, batch)
                        continue
                    if batch.related_reference is not None:
                        related = batch.related_reference
                        if related.session_id != batch.session_id or related.generation != batch.generation:
                            raise ValueError("Cross-scope transfer parent")
                        self._facts(connection, Batch(related.session_id, related.generation, related.messages,
                                                      related.opportunity, related.evaluation))
                    self._facts(connection, batch)
                    delivery_store.write_batch(connection, batch)
                    if batch.claim_check is not None:
                        if (batch.claim_check.session_id != batch.session_id
                                or batch.claim_check.message_event_id not in {m.message_event_id for m in batch.messages}):
                            raise ValueError("Claim check parent mismatch")
                        connection.execute("INSERT OR IGNORE INTO priority_claim_checks VALUES(?,?,?,?,?,?,?,?)",
                                           tuple(asdict(batch.claim_check).values()))
                    opportunity = batch.opportunity
                    if opportunity is None:
                        continue
                    if batch.execution is not None:
                        event = batch.execution
                        if event.opportunity_id != opportunity.opportunity_id or (
                            event.evaluation_id is not None and (
                                batch.evaluation is None or event.evaluation_id != batch.evaluation.evaluation_id
                            )
                        ):
                            raise ValueError("Execution parent mismatch")
                        connection.execute(
                            "INSERT OR IGNORE INTO execution_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            tuple(asdict(event).values()),
                        )
                        # Append-only events are authoritative for many-to-many identity and outcomes.
                        # Do not collapse several turns into the legacy scalar replay_turn_id.
                        connection.execute(
                            "UPDATE outcomes SET linkage_status='explicit_events', "
                            "planner_status=CASE WHEN ?='planner_started' THEN 'started' ELSE planner_status END "
                            "WHERE opportunity_id=?",
                            (event.stage, opportunity.opportunity_id),
                        )
                        if event.stage == "priority_replyer_entered":
                            connection.execute(
                                "UPDATE outcomes SET replyer_status='entry_observed' WHERE opportunity_id=?",
                                (opportunity.opportunity_id,),
                        )
                    if batch.reply_event is not None:
                        event = batch.reply_event
                        parent = batch.execution
                        if (parent is None or event.parent_event_id != parent.event_id
                                or event.opportunity_id != parent.opportunity_id
                                or event.evaluation_id != parent.evaluation_id
                                or event.execution_id != parent.execution_id):
                            raise ValueError("Reply event parent mismatch")
                        connection.execute(
                            "INSERT OR IGNORE INTO reply_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            tuple(asdict(event).values()),
                        )
                        if event.stage == "driver_send" and batch.delivery_scope is not None:
                            delivery_store.relink(connection, batch.delivery_scope.scope_id, event.outbound_id)
                    if batch.transfer is not None:
                        transfer = batch.transfer
                        related = batch.related_reference
                        if (related is None or transfer.source_opportunity_id != opportunity.opportunity_id
                                or transfer.target_opportunity_id != related.opportunity.opportunity_id
                                or opportunity.account_id != transfer.account_id
                                or opportunity.source_type != "priority_at"
                                or related.opportunity.source_type != "ordinary_pending"
                                or related.opportunity.session_id != opportunity.session_id
                                or related.opportunity.account_id not in (None, opportunity.account_id)
                                or transfer.message_event_id not in {m.message_event_id for m in batch.messages}
                                or transfer.message_event_id not in {m.message_event_id for m in related.messages}):
                            raise ValueError("Transfer parent mismatch")
                        connection.execute("INSERT OR IGNORE INTO opportunity_transfers VALUES(?,?,?,?,?,?,?,?)",
                                           tuple(asdict(transfer).values()))
                # A policy update can be published while this transaction is in flight.
                if any(not self._valid(b) for b in accepted):
                    connection.rollback()
                    self.revoked += len(accepted)
                    return
            self.committed += len(accepted)

    def _run(self) -> None:
        while not self._closed.is_set() or not self.queue.empty():
            batches: List[Any] = []
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
            with self._annotation_authorization_lock:
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
                connection.execute("DELETE FROM delivery_receipts WHERE observed_at<?", (cutoff,))
                connection.execute("DELETE FROM opportunities WHERE created_at<?", (cutoff,))
                connection.execute(
                    "DELETE FROM delivery_scopes WHERE created_at<? "
                    "AND NOT EXISTS (SELECT 1 FROM delivery_receipts d WHERE d.scope_id=delivery_scopes.scope_id) "
                    "AND NOT EXISTS (SELECT 1 FROM reply_events r "
                    "WHERE json_extract(r.detail,'$.collection_scope_id')=delivery_scopes.scope_id)", (cutoff,),
                )
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
            with self._annotation_authorization_lock:
                self._deleted_sessions.add(session_id)
                self._session_epochs[session_id] = self._session_epochs.get(session_id, 0) + 1
            self.delivery_inline_receipt = None
            for key, binding in tuple(self.delivery_bindings.items()):
                if binding.scope.session_id == session_id:
                    self.delivery_bindings.pop(key, None)
            with self._commit_lock:
                if not self.path.exists():
                    return
                with closing(self._connect()) as connection, connection:
                    connection.execute("DELETE FROM delivery_scopes WHERE session_id=?", (session_id,))
                    connection.execute("DELETE FROM opportunities WHERE session_id=?", (session_id,))
                    connection.execute("DELETE FROM message_events WHERE session_id=?", (session_id,))

    def annotation_generation(self, session_id: str) -> Optional[Tuple[int, int]]:
        """Historical annotation permission deliberately does not depend on collection enabled."""
        with self._annotation_authorization_lock:
            epoch, policy = self._policy_state
            if self._closed.is_set() or session_id in self._deleted_sessions or session_id not in policy.authorized_session_ids:
                return None
            return epoch, self._session_epochs.get(session_id, 0)

    def read_authorization_snapshot(self) -> Tuple[int, Dict[str, int]]:
        """Capture historical read permission atomically with policy invalidation.

        Collection being disabled does not revoke historical read authorization,
        matching annotation_generation; deletion tombstones still exclude sessions.
        """
        with self._annotation_authorization_lock:
            epoch, policy = self._policy_state
            return epoch, {
                sid: self._session_epochs.get(sid, 0)
                for sid in policy.authorized_session_ids
                if sid not in self._deleted_sessions
            }

    def read_authorization_valid(self, epoch: int, session_epochs: Dict[str, int]) -> bool:
        """Bounded metadata-only checkpoint; no SQLite, writer lock or network I/O."""
        with self._annotation_authorization_lock:
            return (
                not self._closed.is_set()
                and self._policy_state[0] == epoch
                and all(
                    sid not in self._deleted_sessions and self._session_epochs.get(sid, 0) == generation
                    for sid, generation in session_epochs.items()
                )
            )

    @contextmanager
    def annotation_transaction(self, session_id: str, generation: Tuple[int, int]) -> Iterator[sqlite3.Connection]:
        """Confirmed control-plane transaction, never the lossy observation queue.

        Policy invalidation is published before waiting for the write lock. Its
        publication and this final commit are mutually exclusive, establishing
        a precise revocation boundary without locking normal submit/generation.
        """
        if not self._commit_lock.acquire(timeout=0.5):
            raise TimeoutError("Annotation writer busy")
        connection = None
        try:
            if self.annotation_generation(session_id) != generation:
                raise PermissionError("Annotation authorization changed")
            if not self.path.exists():
                raise FileNotFoundError("Observation database missing")
            connection = self._connect(existing_only=True, initialize_run=False)
            connection.row_factory = sqlite3.Row
            deadline = time.monotonic() + 3
            connection.set_progress_handler(lambda: int(time.monotonic()>deadline), 1000)
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            with self._annotation_authorization_lock:
                if self.annotation_generation(session_id) != generation:
                    raise PermissionError("Annotation authorization changed")
                connection.commit()
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
            self._commit_lock.release()

    def query(self, session_id: str, limit: int = 100) -> Dict[str, Any]:
        """Read only: no file creation, migration, cleanup or guessed cross-session links."""
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        result: Dict[str, Any] = {"messages": [], "opportunities": [], "evaluations": [], "outcomes": [],
                                  "execution_events": [], "transfers": [], "claim_checks": [], "reply_events": [],
                                  "delivery_receipts": [], "delivery_links": []}
        result.update(delivery_store.empty_projection())
        if not self.path.exists():
            return result
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as connection:
            connection.row_factory = sqlite3.Row
            result.update(delivery_store.query(connection, session_id, limit))
            has_execution = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_events'"
            ).fetchone() is not None
            has_transfers = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_transfers'"
            ).fetchone() is not None
            has_replies = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_events'"
            ).fetchone() is not None
            if has_transfers:
                result["claim_checks"] = [dict(row) for row in connection.execute(
                    "SELECT * FROM priority_claim_checks WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
                    (session_id, limit),
                )]
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
                if has_replies:
                    opportunity["reply_event_count"] = connection.execute(
                        "SELECT COUNT(*) FROM reply_events WHERE opportunity_id=?", (oid,)
                    ).fetchone()[0]
                    opportunity["reply_events_truncated"] = opportunity["reply_event_count"] > limit
                    for row in connection.execute(
                        "SELECT * FROM reply_events WHERE opportunity_id=? ORDER BY rowid LIMIT ?", (oid, limit)
                    ):
                        item = dict(row)
                        item["detail"] = json.loads(item["detail"])
                        result["reply_events"].append(item)
                opportunity["message_event_ids"] = [
                    r[0]
                    for r in connection.execute(
                        "SELECT message_event_id FROM opportunity_messages WHERE opportunity_id=? ORDER BY message_event_id",
                        (oid,),
                    )
                ]
                opportunity["evaluation_count"] = connection.execute(
                    "SELECT COUNT(*) FROM evaluations WHERE opportunity_id=?", (oid,)
                ).fetchone()[0]
                opportunity["evaluations_truncated"] = opportunity["evaluation_count"] > limit
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
                if has_transfers:
                    # Each outgoing transfer is returned once, even when both opportunities are in the page.
                    rows = connection.execute(
                        "SELECT * FROM opportunity_transfers WHERE source_opportunity_id=? ORDER BY rowid LIMIT ?",
                        (oid, limit),
                    )
                    result["transfers"].extend(dict(row) for row in rows)
                    opportunity["incoming_transfer_ids"] = [row[0] for row in connection.execute(
                        "SELECT transfer_id FROM opportunity_transfers WHERE target_opportunity_id=? ORDER BY rowid LIMIT ?",
                        (oid, limit),
                    )]
                if has_execution:
                    opportunity["execution_event_count"] = connection.execute(
                        "SELECT COUNT(*) FROM execution_events WHERE opportunity_id=?", (oid,)
                    ).fetchone()[0]
                    opportunity["execution_events_truncated"] = opportunity["execution_event_count"] > limit
                    for row in connection.execute(
                        "SELECT * FROM execution_events WHERE opportunity_id=? ORDER BY rowid LIMIT ?",
                        (oid, limit),
                    ):
                        item = dict(row)
                        item["message_event_ids"] = json.loads(item["message_event_ids"])
                        item["tool_names"] = json.loads(item["tool_names"])
                        result["execution_events"].append(item)
        return result

    def health(self) -> Dict[str, Any]:
        return {
            "enabled": self.policy.enabled,
            "delivery_unmatched": self.delivery_unmatched,
            "delivery_ambiguous": self.delivery_ambiguous,
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
