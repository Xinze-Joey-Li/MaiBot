"""Display-only identities for the annotation workbench; never writes chat data."""

from typing import Any, Dict, FrozenSet, List, Optional, Tuple
import hashlib
import json
import re
import sqlite3


# These reasons are emitted immediately before returning/continuing without a
# Planner request. Generic missing_start reasons are deliberately excluded.
NOT_STARTED_REASONS = frozenset({
    "owner_soft_paused", "wait_still_active", "paused_or_stale_after_quiet",
    "expired_trigger", "focus_unavailable", "prepare_skipped", "silent_consumption",
    "runtime_stopped_before_dequeue",
})


def planner_summary(connection: sqlite3.Connection, opportunity_id: str, evaluation_id: Optional[str],
                    boundary: float) -> Dict[str, Any]:
    result: Dict[str, Any] = {"status": "unknown", "evidence_status": "missing_records",
                              "rounds": [], "not_started_attempts": 0}
    rows = connection.execute(
        "SELECT * FROM execution_events WHERE opportunity_id=? AND evaluation_id IS ? "
        "AND stage IN ('planner_started','tool_selection','not_started') ORDER BY occurred_at,event_id LIMIT 513",
        (opportunity_id, evaluation_id),
    ).fetchall()
    if len(rows) > 512:
        result["evidence_status"] = "record_limit"
        return result
    if not rows:
        return result
    cycles: Dict[Tuple[str, str, str], List[sqlite3.Row]] = {}
    skipped: Dict[str, List[sqlite3.Row]] = {}
    for row in rows:
        if not row["execution_id"] or row["occurred_at"] < boundary:
            result["evidence_status"] = "incomplete_lineage"
            return result
        if row["stage"] == "not_started":
            skipped.setdefault(row["execution_id"], []).append(row)
            continue
        if not row["logical_turn_id"] or not row["cycle_id"]:
            result["evidence_status"] = "incomplete_lineage"
            return result
        key = (row["execution_id"], row["logical_turn_id"], row["cycle_id"])
        cycles.setdefault(key, []).append(row)
    active_executions = {key[0] for key in cycles}
    for execution_id, events in skipped.items():
        if execution_id in active_executions:
            continue  # A skipped attempt never overrides actual Planner records.
        if any(row["reason"] not in NOT_STARTED_REASONS or row["status"] != "not_observed" for row in events):
            result["evidence_status"] = "incomplete_lineage"
            return result
        result["not_started_attempts"] += 1
    rounds = []
    for (execution_id, turn_id, cycle_id), events in cycles.items():
        # One turn can consume several evaluation references. Never treat a
        # shared tool selection as this target's independent Planner decision.
        shared = connection.execute(
            "SELECT 1 FROM execution_events e JOIN opportunities o USING(opportunity_id) "
            "WHERE o.session_id=(SELECT session_id FROM opportunities WHERE opportunity_id=?) "
            "AND e.execution_id=? AND e.logical_turn_id=? AND e.cycle_id=? "
            "AND e.stage IN ('planner_started','tool_selection') "
            "AND (e.opportunity_id<>? OR e.evaluation_id IS NOT ?) LIMIT 1",
            (opportunity_id, execution_id, turn_id, cycle_id, opportunity_id, evaluation_id),
        ).fetchone()
        if shared:
            result["evidence_status"] = "shared_cycle"
            return result
        starts = [row for row in events if row["stage"] == "planner_started"]
        selections = [row for row in events if row["stage"] == "tool_selection"]
        if (len(starts) != 1 or len(selections) != 1 or starts[0]["status"] != "started"
                or starts[0]["reason"] != "request_entered" or selections[0]["status"] != "observed"
                or selections[0]["reason"] != "planner_response"
                or starts[0]["occurred_at"] > selections[0]["occurred_at"]):
            result["evidence_status"] = "incomplete_lineage"
            return result
        selection = selections[0]
        try:
            tools = json.loads(selection["tool_names"])
        except (TypeError, ValueError):
            tools = None
        if not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools):
            result["evidence_status"] = "invalid_tool_record"
            return result
        decision = "mixed" if "reply" in tools and "wait" in tools else (
            "reply" if "reply" in tools else "wait" if "wait" in tools else "other"
        )
        rounds.append({"execution_id": execution_id, "logical_turn_id": turn_id, "cycle_id": cycle_id,
                       "start_event_id": starts[0]["event_id"], "selection_event_id": selection["event_id"],
                       "occurred_at": selection["occurred_at"], "decision": decision})
    result["rounds"] = rounds
    result["status"] = "observed" if rounds else "not_started"
    result["evidence_status"] = "tool_selection" if rounds else "explicit_not_started"
    return result


def machine_summary(connection: sqlite3.Connection, opportunity_id: str, evaluation_id: Optional[str],
                    evaluation: sqlite3.Row) -> Dict[str, Any]:
    """Gate, selected Planner tools and platform receipts are independent facts."""
    result = {
        "gate_result": evaluation["gate_result"] if evaluation_id else None,
        "decision_reason": evaluation["decision_reason"] if evaluation_id else None,
        "planner": {"status": "unknown", "evidence_status": "missing_records", "rounds": [],
                    "not_started_attempts": 0},
        "execution_result": "unknown",
    }
    available = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "execution_events" in available:
        result["planner"] = planner_summary(connection, opportunity_id, evaluation_id, evaluation["evaluated_at"])
    if "reply_events" not in available:
        return result
    attempted = connection.execute(
        "SELECT 1 FROM reply_events WHERE opportunity_id=? AND evaluation_id IS ? "
        "AND stage='driver_send' AND status='send_attempted' LIMIT 1", (opportunity_id, evaluation_id),
    ).fetchone()
    if not attempted:
        # This describes the retained observations, never a definitive non-send.
        other_attempt = connection.execute(
            "SELECT 1 FROM reply_events WHERE opportunity_id=? AND stage='driver_send' "
            "AND status='send_attempted' LIMIT 1", (opportunity_id,),
        ).fetchone()
        result["execution_result"] = "unknown" if other_attempt else "not_observed"
        return result
    if not {"delivery_links", "delivery_receipts"} <= available:
        return result
    attempts = connection.execute(
        "SELECT r.attempt_id,r.outbound_id,MAX(d.conclusion='platform_confirmed') confirmed,"
        "MAX(d.conclusion='definitive_failure') failed,COUNT(d.receipt_id) receipts "
        "FROM reply_events r LEFT JOIN delivery_links l ON l.parent_event_id=r.event_id "
        "LEFT JOIN delivery_receipts d ON d.receipt_id=l.receipt_id "
        "AND d.session_id=(SELECT session_id FROM opportunities WHERE opportunity_id=r.opportunity_id) "
        "WHERE r.opportunity_id=? AND r.evaluation_id IS ? AND r.stage='driver_send' "
        "AND r.status='send_attempted' GROUP BY r.attempt_id,r.outbound_id",
        (opportunity_id, evaluation_id),
    ).fetchall()
    if any(row["confirmed"] and not row["failed"] for row in attempts):
        result["execution_result"] = "platform_confirmed"
    elif attempts and all(row["failed"] and not row["confirmed"] for row in attempts):
        result["execution_result"] = "definitive_failure"
    return result


class ReviewPrivacy:
    def __init__(self, chat: sqlite3.Connection, session_id: str, opportunity_id: str,
                 bot_accounts: FrozenSet[str] = frozenset()) -> None:
        self.scope = session_id + ":" + opportunity_id
        self.aliases: Dict[str, str] = {}
        names = {}
        # Include all recorded aliases, not only the current page's display name.
        # The read connection has a query deadline; failure must not expose raw text.
        rows = chat.execute(
            "SELECT DISTINCT user_id,user_nickname,user_cardname FROM mai_messages WHERE session_id=? AND platform='qq'",
            (session_id,),
        ).fetchall()
        users = sorted({str(row["user_id"]) for row in rows if row["user_id"]} | set(bot_accounts))
        for uid in users:
            self.aliases[uid] = "群友 " + hashlib.sha256((self.scope + ":user:" + uid).encode()).hexdigest()[:10].upper()
        # Explicit collision detection rather than assuming a shortened digest is unique.
        if len(set(self.aliases.values())) != len(users):
            self.aliases = {uid: "群友 " + hashlib.sha256((self.scope + ":user:" + uid).encode()).hexdigest().upper()
                            for uid in users}
        for uid in bot_accounts:
            self.aliases[uid] = "大肥鱼"
        for row in rows:
            alias = self.aliases.get(str(row["user_id"]), "群友（身份缺失）")
            for name in (row["user_nickname"], row["user_cardname"], row["user_id"]):
                if name:
                    names.setdefault(str(name), set()).add(alias)
        for uid in bot_accounts:
            names.setdefault(uid, set()).add("大肥鱼")
        self.replacements = {
            name: next(iter(aliases)) if len(aliases) == 1 else "群友（同名，身份不确定）"
            for name, aliases in names.items()
        }
        self.pattern = re.compile("|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))) if names else None

    def sender(self, user_id: Optional[str], event_id: str) -> str:
        if user_id:
            return self.aliases[str(user_id)]
        # Missing identities are per-message, never merged by a shared nickname.
        return "未知发送者 " + hashlib.sha256((self.scope + ":event:" + event_id).encode()).hexdigest()[:12].upper()

    def text(self, value: str) -> str:
        return self.pattern.sub(lambda match: self.replacements[match.group()], value) if self.pattern else value
