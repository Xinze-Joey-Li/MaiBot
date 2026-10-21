"""Receipt joins and evidence projections. Called only on the observation writer/control plane."""

from dataclasses import asdict, replace
from typing import Any, Dict, List, Optional, Set, Tuple
import json
import time

from .models import Batch, DeliveryScope, ReceiptFact, ReceiptProbe, identity


MIGRATION = """
BEGIN;
CREATE TABLE delivery_scopes (
    scope_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
    policy_version TEXT NOT NULL, created_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE INDEX delivery_scope_session ON delivery_scopes(session_id,active);
CREATE TABLE delivery_receipts (
    receipt_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES delivery_scopes ON DELETE CASCADE,
    session_id TEXT NOT NULL, platform TEXT NOT NULL, account_id TEXT, route_scope TEXT,
    outbound_id TEXT NOT NULL, driver_id TEXT, driver_kind TEXT, driver_status TEXT NOT NULL,
    platform_message_id TEXT, delivery_id TEXT, acknowledged INTEGER NOT NULL, retcode INTEGER,
    observed_at REAL NOT NULL, source TEXT NOT NULL, attempt_id TEXT, ambiguous INTEGER NOT NULL,
    failure_evidence TEXT, recorded_at REAL NOT NULL, conclusion TEXT NOT NULL, evidence_strength TEXT NOT NULL);
CREATE INDEX delivery_receipt_key ON delivery_receipts(scope_id,outbound_id,driver_id);
CREATE INDEX delivery_receipt_session ON delivery_receipts(session_id,observed_at);
CREATE TABLE delivery_links (
    receipt_id TEXT NOT NULL REFERENCES delivery_receipts ON DELETE CASCADE,
    parent_event_id TEXT NOT NULL REFERENCES reply_events ON DELETE CASCADE,
    linked_at REAL NOT NULL, PRIMARY KEY(receipt_id,parent_event_id));
CREATE INDEX delivery_link_parent ON delivery_links(parent_event_id);
PRAGMA user_version=5;
COMMIT;
"""


def register_scope(connection: Any, scope: DeliveryScope) -> None:
    connection.execute("INSERT OR IGNORE INTO delivery_scopes VALUES(?,?,?,?,?,1)", tuple(asdict(scope).values()))


def conclusion(fact: ReceiptFact) -> Tuple[str, str]:
    # Exactly the positive receipt predicate used by replies.receipt; no has_success inference.
    if fact.driver_kind == "local":
        return "not_applicable", "local_render"
    if fact.driver_status == "sent" and fact.platform_message_id:
        return "platform_confirmed", "platform_message_id"
    if fact.driver_status == "sent" and fact.acknowledged and fact.delivery_id:
        return "platform_confirmed", "explicit_delivery_id"
    if fact.driver_status == "failed" and fact.failure_evidence:
        return "definitive_failure", fact.failure_evidence
    # Gateway metadata has no common contract proving that retcode is a platform
    # rejection rather than a post-dispatch/RPC error. Preserve it without upgrading certainty.
    return "unknown", "driver_report_only"


def candidates(connection: Any, fact: ReceiptFact, scope_id: Optional[str] = None) -> List[Any]:
    if not fact.driver_id or fact.platform != "qq":
        return []
    return connection.execute(
        "SELECT r.event_id,r.attempt_id,s.scope_id,s.session_id,s.policy_version,s.run_id,s.created_at "
        "FROM reply_events r JOIN delivery_scopes s "
        "ON json_extract(r.detail,'$.collection_scope_id')=s.scope_id "
        "JOIN opportunities o ON o.opportunity_id=r.opportunity_id AND o.session_id=s.session_id "
        "WHERE s.active=1 AND r.stage='driver_send' AND r.status='send_attempted' "
        "AND r.outbound_id=? AND json_extract(r.detail,'$.driver_id')=? "
        "AND json_extract(r.detail,'$.platform')=? "
        "AND json_extract(r.detail,'$.account_id') IS ? "
        "AND json_extract(r.detail,'$.route_scope') IS ? "
        "AND (? IS NULL OR s.scope_id=?) AND (? IS NULL OR r.attempt_id=?)",
        (fact.outbound_id, fact.driver_id, fact.platform, fact.account_id, fact.route_scope,
         scope_id, scope_id, fact.attempt_id, fact.attempt_id),
    ).fetchall()


def link(connection: Any, receipt_id: str, scope_id: str, fact: ReceiptFact) -> None:
    if fact.ambiguous:
        return
    parents = candidates(connection, fact, scope_id)
    if len({r[1] for r in parents}) != 1:
        return
    connection.executemany("INSERT OR IGNORE INTO delivery_links VALUES(?,?,?)",
                           [(receipt_id, row[0], time.time()) for row in parents])


def save(connection: Any, scope: DeliveryScope, fact: ReceiptFact) -> None:
    active = connection.execute("SELECT active FROM delivery_scopes WHERE scope_id=?", (scope.scope_id,)).fetchone()
    if active is None or not active[0]:
        return
    raw = asdict(fact)
    # Times and arrival boundary do not make a duplicate trusted receipt a new fact.
    key = {k: v for k, v in raw.items() if k not in {"observed_at", "source", "failure_evidence"}}
    receipt_id = identity("delivery_receipt", scope.scope_id, json.dumps(key, sort_keys=True))
    state, strength = conclusion(fact)
    connection.execute("INSERT OR IGNORE INTO delivery_receipts VALUES(" + ",".join("?" * 22) + ")",
                       (receipt_id, scope.scope_id, scope.session_id, *raw.values(), time.time(), state, strength))
    link(connection, receipt_id, scope.scope_id, fact)


def write_batch(connection: Any, batch: Batch) -> None:
    scope = batch.delivery_scope
    if scope is None:
        return
    if scope.session_id != batch.session_id:
        raise ValueError("Receipt authorization scope mismatch")
    register_scope(connection, scope)
    if batch.receipt is not None:
        save(connection, scope, batch.receipt)


def relink(connection: Any, scope_id: str, outbound_id: str) -> None:
    names = list(ReceiptFact.__dataclass_fields__)
    for row in connection.execute(
        "SELECT receipt_id," + ",".join(names) + " FROM delivery_receipts WHERE scope_id=? AND outbound_id=?",
        (scope_id, outbound_id),
    ).fetchall():
        link(connection, row[0], scope_id, ReceiptFact(*row[1:]))


def probe(store: Any, connection: Any, item: ReceiptProbe) -> None:
    allowed = {sid for sid, generation in item.authorizations if store.generation(sid) == generation}
    parents = [r for r in candidates(connection, item.receipt) if r[3] in allowed and (
                   r[3] not in store.delivery_current_only_sessions
                   or r[2] == identity("delivery_scope", store.run_id, r[3], str(store.generation(r[3])))
               )]
    scopes = {r[2] for r in parents}
    if len(scopes) != 1:
        store.delivery_unmatched += 1
        if len(scopes) > 1:
            store.delivery_ambiguous += 1
        return  # Cannot authorize unscoped evidence after deletion or across sessions.
    row = parents[0]
    scope = DeliveryScope(row[2], row[3], row[5], row[4], row[6])
    attempts = {r[1] for r in parents}
    fact = replace(item.receipt, attempt_id=next(iter(attempts)) if len(attempts) == 1 else None,
                   ambiguous=len(attempts) != 1)
    save(connection, scope, fact)


def revoke(connection: Any, authorized: Set[str], current_only: Optional[Dict[str, str]] = None) -> None:
    current_only = current_only or {}
    for scope_id, session_id in connection.execute("SELECT scope_id,session_id FROM delivery_scopes WHERE active=1").fetchall():
        if session_id not in authorized or (session_id in current_only and scope_id != current_only[session_id]):
            connection.execute("UPDATE delivery_scopes SET active=0 WHERE scope_id=?", (scope_id,))


def empty_projection() -> Dict[str, Any]:
    return {"delivery_receipts": [], "delivery_links": [], "delivery_attempt_states": [],
            "delivery_schema_available": False, "delivery_receipt_count": None,
            "delivery_receipts_truncated": False, "delivery_attempt_count": None,
            "delivery_attempt_states_truncated": False}


def query(connection: Any, session_id: str, limit: int) -> Dict[str, Any]:
    result = empty_projection()
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_receipts'").fetchone() is None:
        return result
    result["delivery_schema_available"] = True
    for row in connection.execute(
        "SELECT d.*,(SELECT COUNT(*) FROM delivery_links l WHERE l.receipt_id=d.receipt_id) AS link_count "
        "FROM delivery_receipts d WHERE d.session_id=? ORDER BY d.rowid DESC LIMIT ?", (session_id, limit),
    ):
        item = dict(row)
        item["linkage_status"] = "linked" if item["link_count"] else (
            "ambiguous" if item["ambiguous"] else "attempt_not_observed_or_ambiguous")
        item["platform_confirmed_at"] = None  # Receipt has no trusted platform clock timestamp.
        item["attempt_conclusion"] = item["conclusion"] if item["link_count"] else "unknown"
        result["delivery_receipts"].append(item)
        result["delivery_links"].extend(dict(r) for r in connection.execute(
            "SELECT l.*,r.opportunity_id,r.evaluation_id,r.execution_id,r.flow_id,r.reply_id,r.outbound_id,"
            "r.attempt_id,r.logical_turn_id,r.cycle_id FROM delivery_links l JOIN reply_events r "
            "ON l.parent_event_id=r.event_id WHERE l.receipt_id=?", (item["receipt_id"],),
        ))
    count = connection.execute("SELECT COUNT(*) FROM delivery_receipts WHERE session_id=?", (session_id,)).fetchone()[0]
    result["delivery_receipt_count"] = count
    result["delivery_receipts_truncated"] = count > limit
    # Compute state from all retained linked facts, not just the limited receipt page.
    for row in connection.execute(
        "SELECT r.attempt_id,r.outbound_id,MAX(json_extract(r.detail,'$.driver_id')) AS driver_id,"
        "COUNT(DISTINCT d.receipt_id) AS receipt_count,"
        "MAX(CASE WHEN d.conclusion='platform_confirmed' THEN 1 ELSE 0 END) AS confirmed,"
        "MAX(CASE WHEN d.conclusion='definitive_failure' THEN 1 ELSE 0 END) AS definitive_failure,"
        "MAX(CASE WHEN d.conclusion='not_applicable' THEN 1 ELSE 0 END) AS not_applicable,"
        "GROUP_CONCAT(DISTINCT d.driver_status) AS driver_statuses "
        "FROM reply_events r JOIN opportunities o ON r.opportunity_id=o.opportunity_id "
        "LEFT JOIN delivery_links l ON l.parent_event_id=r.event_id "
        "LEFT JOIN delivery_receipts d ON d.receipt_id=l.receipt_id "
        "WHERE o.session_id=? AND r.stage='driver_send' AND r.status='send_attempted' "
        "GROUP BY r.attempt_id,r.outbound_id ORDER BY MAX(r.rowid) DESC LIMIT ?", (session_id, limit),
    ):
        state = dict(row)
        state["attempted"] = True
        state["status"] = ("platform_confirmed" if state["confirmed"] else "definitive_failure"
                           if state["definitive_failure"] else "not_applicable" if state["not_applicable"]
                           else "unknown" if state["receipt_count"] else "not_observed")
        state["conflicting_evidence"] = bool(state["confirmed"] and state["definitive_failure"])
        state["driver_statuses"] = state["driver_statuses"].split(",") if state["driver_statuses"] else []
        result["delivery_attempt_states"].append(state)
    state_count = connection.execute(
        "SELECT COUNT(DISTINCT r.attempt_id) FROM reply_events r JOIN opportunities o "
        "ON o.opportunity_id=r.opportunity_id WHERE o.session_id=? AND r.stage='driver_send'", (session_id,),
    ).fetchone()[0]
    result["delivery_attempt_count"] = state_count
    result["delivery_attempt_states_truncated"] = state_count > limit
    return result
