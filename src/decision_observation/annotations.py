"""Reliable, revisioned human supervision; no production decisions are modified."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import hashlib
import json
import sqlite3
import time

from .annotation_models import ACTOR_ID, CONTRACT_VERSION, QUALITIES, AnnotationCommand, AnnotationError
from .dashboard import read_database, tables
from .models import SCHEMA_VERSION, identity
from .store import ObservationStore


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AnnotationService:
    def __init__(self, store: ObservationStore, chat_path: Path) -> None:
        self.store, self.chat_path = store, chat_path

    def _target(
        self,
        connection: sqlite3.Connection,
        opportunity_id: str,
        target_type: str,
        evaluation_id: Optional[str],
        *,
        for_write: bool = False,
    ) -> Tuple[Dict[str, Any], Tuple[int, int]]:
        if (
            target_type not in ("evaluation", "opportunity")
            or (target_type == "evaluation") != bool(evaluation_id)
            or (target_type == "opportunity" and evaluation_id is not None)
        ):
            raise AnnotationError(422, "invalid_target_identity")
        row = connection.execute("SELECT * FROM opportunities WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        if row is None or (generation := self.store.annotation_generation(row["session_id"])) is None:
            raise AnnotationError(404, "annotation_target_not_found_or_authorized")
        metadata = {
            "target_type": target_type,
            "opportunity_id": opportunity_id,
            "evaluation_id": evaluation_id,
            "session_id": row["session_id"],
            "source_type": row["source_type"],
            "context_boundary_at": row["created_at"],
            "evaluation_sequence": None,
            "feature_version": None,
            "gate_result": None,
            "feature_snapshot_hash": None,
            "evaluation_facts_hash": None,
            "membership_available": False,
            "message_event_ids": [],
        }
        if evaluation_id:
            evaluation = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id=? AND opportunity_id=?", (evaluation_id, opportunity_id)
            ).fetchone()
            if evaluation is None:
                raise AnnotationError(404, "evaluation_not_in_opportunity")
            snapshot = json.loads(evaluation["feature_snapshot"])
            ids = snapshot.get("message_event_ids") if isinstance(snapshot, dict) else None
            metadata.update(
                evaluation_sequence=evaluation["evaluation_sequence"],
                feature_version=evaluation["feature_version"],
                context_boundary_at=evaluation["evaluated_at"],
                gate_result=evaluation["gate_result"],
                feature_snapshot_hash=digest(snapshot),
                evaluation_facts_hash=digest(dict(evaluation)),
                membership_available=isinstance(ids, list),
                message_event_ids=ids if isinstance(ids, list) else [],
            )
        else:
            available = tables(connection)
            existing = (
                "annotation_targets" in available
                and connection.execute(
                    "SELECT 1 FROM annotation_targets WHERE opportunity_id=? AND target_type='opportunity'",
                    (opportunity_id,),
                ).fetchone()
            )
            if (
                for_write
                and not existing
                and connection.execute(
                    "SELECT 1 FROM evaluations WHERE opportunity_id=? LIMIT 1", (opportunity_id,)
                ).fetchone()
            ):
                raise AnnotationError(422, "evaluation_target_required")
            metadata["message_event_ids"] = [
                r[0]
                for r in connection.execute(
                    "SELECT DISTINCT message_event_id FROM opportunity_messages WHERE opportunity_id=? ORDER BY message_event_id LIMIT 513",
                    (opportunity_id,),
                )
            ]
        ids = metadata["message_event_ids"]
        if len(ids) > 512 or any(not isinstance(mid, str) for mid in ids):
            raise AnnotationError(503, "invalid_or_oversized_snapshot_membership")
        metadata["annotation_id"] = identity("annotation", target_type, opportunity_id, evaluation_id or "")
        return metadata, generation

    def _prepare(
        self, opportunity_id: str, target_type: str, evaluation_id: Optional[str], *, for_write: bool = False
    ) -> Tuple[Dict[str, Any], Tuple[int, int]]:
        if not self.store.path.exists():
            raise AnnotationError(404, "observation_database_not_found")
        with read_database(self.store.path) as connection:
            return self._target(connection, opportunity_id, target_type, evaluation_id, for_write=for_write)

    def _evidence(self, metadata: Dict[str, Any], command: AnnotationCommand) -> Dict[str, Any]:
        ids = metadata["message_event_ids"]
        missing = []
        messages = []
        with read_database(self.store.path) as connection:
            for mid in dict.fromkeys(ids):
                row = connection.execute(
                    "SELECT platform_message_id FROM message_events WHERE session_id=? AND message_event_id=?",
                    (metadata["session_id"], mid),
                ).fetchone()
                if row is None:
                    missing.append(mid)
                else:
                    messages.append((mid, row[0]))
        usable = []
        unavailable = []
        chat_status = "database_missing"
        if self.chat_path.exists():
            try:
                with read_database(self.chat_path) as chat:
                    if "mai_messages" in tables(chat):
                        chat_status = "current_main_chat_read"
                        for mid, pid in messages:
                            rows = chat.execute(
                                "SELECT processed_plain_text FROM mai_messages WHERE session_id=? AND platform='qq' AND message_id=? LIMIT 2",
                                (metadata["session_id"], pid),
                            ).fetchall()
                            if len(rows) == 1 and rows[0][0] and rows[0][0].strip():
                                usable.append(mid)
                            else:
                                unavailable.append(mid)
                    else:
                        chat_status = "table_missing"
            except (sqlite3.Error, OSError):
                # Evidence may legitimately be unavailable; never call it sufficient.
                chat_status = "read_unavailable"
                usable = []
        expected = len(set(ids))
        missing_body = sorted(set(ids) - set(usable))
        quality = (
            "critical_context_missing"
            if not expected or not usable
            else "identity_context_available"
            if metadata["membership_available"] and len(usable) == expected and not missing
            else "partial_context"
        )
        effective = QUALITIES[max(QUALITIES.index(quality), QUALITIES.index(command.context_quality))]
        return {
            "target": metadata,
            "checked_at": time.time(),
            "context_mode": command.context_mode,
            "message_identity_digest": digest(ids),
            "expected_member_count": expected,
            "usable_member_count": len(usable),
            "missing_observation_message_ids": missing,
            "unavailable_current_text_message_ids": missing_body,
            "ambiguous_or_empty_message_ids": unavailable,
            "chat_status": chat_status,
            "derived_context_quality": quality,
            "effective_context_quality": effective,
            "body_source": "current_main_chat_not_frozen",
            "full_planner_prompt_recovered": False,
            "reference_context": "not_attested_or_copied",
            "context_view_receipt": "not_collected",
            "training_review_required": effective != "identity_context_available",
            "api_contract_version": CONTRACT_VERSION,
            "collection_schema_version": SCHEMA_VERSION,
        }

    @staticmethod
    def _revision(
        connection: sqlite3.Connection, annotation_id: str, revision: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        sql = "SELECT t.*,r.* FROM annotation_targets t JOIN annotation_revisions r USING(annotation_id) WHERE t.annotation_id=?"
        args: List[Any] = [annotation_id]
        if revision is not None:
            sql += " AND r.revision=?"
            args.append(revision)
        row = connection.execute(sql + " ORDER BY r.revision DESC LIMIT 1", args).fetchone()
        if row is None:
            return None
        item = dict(row)
        item.pop("request_hash", None)
        item["evidence"] = json.loads(item["evidence"])
        item["updated_at"] = item["revision_created_at"]
        return item

    def _verify(self, session_id: str, generation: Tuple[int, int]) -> None:
        if self.store.annotation_generation(session_id) != generation:
            raise AnnotationError(409, "annotation_authorization_changed")

    def save(self, command: AnnotationCommand) -> Dict[str, Any]:
        metadata, generation = self._prepare(
            command.opportunity_id, command.target_type, command.evaluation_id, for_write=True
        )
        evidence = self._evidence(metadata, command)
        try:
            with self.store.annotation_transaction(metadata["session_id"], generation) as connection:
                current_metadata, _ = self._target(
                    connection, command.opportunity_id, command.target_type, command.evaluation_id, for_write=True
                )
                if digest(metadata) != digest(current_metadata):
                    raise AnnotationError(409, "annotation_evidence_target_changed")
                duplicate = connection.execute(
                    "SELECT annotation_id,revision,request_hash FROM annotation_revisions WHERE idempotency_key=?",
                    (command.idempotency_key,),
                ).fetchone()
                aid = metadata["annotation_id"]
                current = self._revision(connection, aid)
                revision = current["revision"] if current else 0
                if duplicate is not None:
                    if duplicate["annotation_id"] != aid or duplicate["request_hash"] != command.request_hash:
                        raise AnnotationError(409, "annotation_idempotency_conflict")
                    saved = self._revision(connection, aid, duplicate["revision"])
                    replayed = True
                else:
                    if command.expected_revision != revision:
                        raise AnnotationError(409, "annotation_revision_conflict", revision)
                    now = time.time()
                    connection.execute(
                        "INSERT OR IGNORE INTO annotation_targets VALUES(?,?,?,?,?)",
                        (aid, command.target_type, command.opportunity_id, command.evaluation_id, now),
                    )
                    connection.execute(
                        "INSERT INTO annotation_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            aid,
                            revision + 1,
                            command.label,
                            command.reason,
                            ACTOR_ID,
                            now,
                            evidence["effective_context_quality"],
                            command.context_quality,
                            json.dumps(evidence, ensure_ascii=False),
                            SCHEMA_VERSION,
                            CONTRACT_VERSION,
                            command.idempotency_key,
                            command.request_hash,
                        ),
                    )
                    saved = self._revision(connection, aid, revision + 1)
                    revision += 1
                    replayed = False
            # The context manager has committed successfully before any success is returned.
            return {"annotation": saved, "current_revision": revision, "idempotent_replay": replayed, "persisted": True}
        except PermissionError:
            raise AnnotationError(409, "annotation_authorization_changed") from None
        except FileNotFoundError:
            raise AnnotationError(404, "observation_database_not_found") from None

    def current(self, opportunity_id: str, target_type: str, evaluation_id: Optional[str]) -> Dict[str, Any]:
        metadata, generation = self._prepare(opportunity_id, target_type, evaluation_id)
        with read_database(self.store.path) as connection:
            available = "annotation_revisions" in tables(connection)
            annotation = self._revision(connection, metadata["annotation_id"]) if available else None
        self._verify(metadata["session_id"], generation)
        return {
            "target": metadata,
            "annotation": annotation,
            "current_revision": annotation["revision"] if annotation else 0,
            "annotation_state": "uncertain"
            if annotation and annotation["label"] == "uncertain"
            else "annotated"
            if annotation
            else "unannotated",
            "annotation_schema_available": available,
            "actor_model": ACTOR_ID,
            "contract_version": CONTRACT_VERSION,
        }

    def history(
        self, opportunity_id: str, target_type: str, evaluation_id: Optional[str], page: int = 1, page_size: int = 25
    ) -> Dict[str, Any]:
        if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            raise AnnotationError(422, "invalid_history_page")
        metadata, generation = self._prepare(opportunity_id, target_type, evaluation_id)
        result = {"items": [], "total": 0, "page": page, "page_size": page_size, "target": metadata}
        with read_database(self.store.path) as connection:
            if "annotation_revisions" in tables(connection):
                aid = metadata["annotation_id"]
                result["total"] = connection.execute(
                    "SELECT COUNT(*) FROM annotation_revisions WHERE annotation_id=?", (aid,)
                ).fetchone()[0]
                result["items"] = [
                    self._revision(connection, aid, r[0])
                    for r in connection.execute(
                        "SELECT revision FROM annotation_revisions WHERE annotation_id=? ORDER BY revision DESC LIMIT ? OFFSET ?",
                        (aid, page_size, (page - 1) * page_size),
                    ).fetchall()
                ]
        self._verify(metadata["session_id"], generation)
        return result

    def targets(
        self,
        *,
        start: float,
        end: float,
        session_id: str = "",
        annotation_state: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        if not (
            0 <= start <= end and end - start <= 31 * 86400 and 1 <= page <= 10000 and 1 <= page_size <= 100
        ) or annotation_state not in ("", "unannotated", "annotated", "uncertain"):
            raise AnnotationError(422, "invalid_annotation_filter")
        allowed = {sid: self.store.annotation_generation(sid) for sid in self.store.policy.authorized_session_ids}
        allowed = {sid: gen for sid, gen in allowed.items() if gen is not None}
        if session_id and session_id not in allowed:
            raise AnnotationError(403, "annotation_session_not_authorized")
        result = {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "window": {"start": start, "end": end},
            "contract_version": CONTRACT_VERSION,
        }
        if not self.store.path.exists():
            return result
        ids = [session_id] if session_id else sorted(allowed)
        with read_database(self.store.path) as connection:
            available = "annotation_revisions" in tables(connection)
            legacy_opportunity = (
                " OR EXISTS(SELECT 1 FROM annotation_targets a WHERE a.opportunity_id=o.opportunity_id AND a.target_type='opportunity')"
                if available
                else ""
            )
            candidates = (
                "SELECT o.opportunity_id,o.session_id,o.source_type,e.evaluation_id,e.evaluation_sequence,e.evaluated_at context_boundary_at,'evaluation' target_type FROM opportunities o JOIN evaluations e USING(opportunity_id) UNION ALL SELECT o.opportunity_id,o.session_id,o.source_type,NULL,NULL,o.created_at,'opportunity' FROM opportunities o WHERE NOT EXISTS(SELECT 1 FROM evaluations e WHERE e.opportunity_id=o.opportunity_id)"
                + legacy_opportunity
            )
            current = (
                "SELECT c.*,COALESCE(r.revision,0) current_revision,r.label,CASE WHEN r.label='uncertain' THEN 'uncertain' WHEN r.revision IS NOT NULL THEN 'annotated' ELSE 'unannotated' END annotation_state FROM candidates c LEFT JOIN annotation_targets a ON a.opportunity_id=c.opportunity_id AND a.evaluation_id IS c.evaluation_id AND a.target_type=c.target_type LEFT JOIN annotation_revisions r ON r.annotation_id=a.annotation_id AND r.revision=(SELECT MAX(r2.revision) FROM annotation_revisions r2 WHERE r2.annotation_id=a.annotation_id)"
                if available
                else "SELECT c.*,0 current_revision,NULL label,'unannotated' annotation_state FROM candidates c"
            )
            where = (
                "session_id IN ("
                + (",".join("?" for _ in ids) or "NULL")
                + ") AND context_boundary_at>=? AND context_boundary_at<=?"
            )
            args: List[Any] = [*ids, start, end]
            if annotation_state:
                where += " AND annotation_state=?"
                args.append(annotation_state)
            sql = (
                "WITH candidates AS ("
                + candidates
                + "), current AS ("
                + current
                + "), filtered AS (SELECT * FROM current WHERE "
                + where
                + ") "
            )
            result["total"] = connection.execute(sql + "SELECT COUNT(*) FROM filtered", args).fetchone()[0]
            result["items"] = [
                dict(r)
                for r in connection.execute(
                    sql
                    + "SELECT * FROM filtered ORDER BY context_boundary_at DESC,opportunity_id DESC,COALESCE(evaluation_id,'') DESC LIMIT ? OFFSET ?",
                    [*args, page_size, (page - 1) * page_size],
                )
            ]
        for sid, gen in allowed.items():
            self._verify(sid, gen)
        return result
