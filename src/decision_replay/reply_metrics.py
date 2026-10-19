"""Reply metrics are projections of the existing replay events, never a second counter store."""
from contextlib import closing
from typing import Any, Dict, List, Optional

import json
import time

LABELS = {
    "generation_requests": "回复生成请求次数", "generation_successes": "回复生成成功次数",
    "logical_replies": "逻辑回复次数", "successful_replies": "成功回复数",
    "partial_replies": "部分成功逻辑回复数", "text_messages": "文本气泡成功发送数",
    "emoji_messages": "表情成功发送数", "platform_messages": "平台消息成功发送数",
    "send_attempts": "发送尝试次数", "send_failures": "发送失败次数",
    "send_skips": "发送跳过次数", "send_unknowns": "发送结果未知次数",
}


def project(events: List[Dict[str, Any]], *, start: float = 0, end: Optional[float] = None,
            session_id: str = "", source: str = "", session_type: str = "", content_type: str = "",
            system: Optional[bool] = None) -> Dict[str, Any]:
    """Event deltas use [start,end); reply state is current, including later confirmations."""
    end = time.time() if end is None else end
    events = sorted(events, key=lambda e: (e["timestamp"], e.get("sequence", 0)))
    replies: Dict[str, Dict[str, Any]] = {}
    messages: Dict[str, Dict[str, Any]] = {}
    platform_seen = set()
    facts = []
    seen = set()
    for event in events:
        if event["event_id"] in seen:
            continue
        seen.add(event["event_id"])
        detail = event["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        kind = event["event_type"].removeprefix("reply_stats.")
        if not event["event_type"].startswith("reply_stats."):
            continue
        rid = detail.get("reply_id")
        oid = detail.get("outbound_message_id")
        reply_key = (event["session_id"], rid)
        item = replies.setdefault(reply_key, {"reply_id": rid, "decision_id": event["logical_turn_id"],
            "session_id": event["session_id"], "source": detail.get("source", event.get("source", "unknown")),
            "session_type": detail.get("session_type", "unknown"), "system": detail.get("system", False),
            "started_at": None, "closed": False, "action_failed": False, "messages": [], "generated": False})
        increments = []
        if kind == "action_started":
            item["started_at"] = event["timestamp"]
            if not item["system"]:
                increments.append("logical_replies")
        elif kind == "action_opened":
            item["closed"] = False
        elif kind == "action_closed":
            item["closed"] = True
            item["action_failed"] = item["action_failed"] or detail.get("action_failed", False)
        elif kind == "action_failed":
            item["action_failed"] = True
        elif kind == "reply_visible" and not item["system"]:
            increments.append("successful_replies")
        elif kind == "generation_success":
            item["generated"] = True
            if not item["system"]:
                increments.append("generation_successes")
        elif kind == "generation_request" and not item["system"]:
            increments.append("generation_requests")
        if oid:
            message_key = (event["session_id"], oid)
            message = messages.setdefault(message_key, {"outbound_message_id": oid, "status": "planned",
                "content_type": detail.get("content_type", "other"), "planned_content_type": detail.get("content_type", "other"),
                "has_text": detail.get("has_text", detail.get("content_type") == "text"),
                "has_emoji": detail.get("has_emoji", detail.get("content_type") == "emoji"),
                "optional": detail.get("optional", False),
                "platform_message_ids": [], "reason": "", "attempts": []})
            if message not in item["messages"]:
                item["messages"].append(message)
            if kind == "send_attempt":
                message["attempts"].append(detail["attempt_id"])
                if message["status"] != "sent":
                    message["status"] = "unknown"  # A crash after dispatch must never imply safe failure.
                increments.append("send_attempts")
            elif kind in {"send_failed", "send_skipped", "send_unknown"}:
                if message["status"] != "sent":
                    message["status"] = {"send_failed": "failed", "send_skipped": "skipped", "send_unknown": "unknown"}[kind]
                    message["reason"] = event["reason_code"]
                increments.append({"send_failed": "send_failures", "send_skipped": "send_skips", "send_unknown": "send_unknowns"}[kind])
            elif kind == "send_confirmed":
                message.update({key: detail[key] for key in ("content_type", "has_text", "has_emoji") if key in detail})
                identity = (detail["platform"], detail.get("account_id", ""), event["session_id"], detail["platform_message_id"])
                message["status"] = "sent"
                if detail["platform_message_id"] not in message["platform_message_ids"]:
                    message["platform_message_ids"].append(detail["platform_message_id"])
                if identity not in platform_seen:
                    platform_seen.add(identity)
                    increments.append("platform_messages")
                    if message["has_text"] and not item["system"]:
                        increments.append("text_messages")
                    if message["has_emoji"] and not item["system"]:
                        increments.append("emoji_messages")
        matches = (not session_id or event["session_id"] == session_id) and (not source or item["source"] == source)
        matches = matches and (not session_type or item["session_type"] == session_type)
        matches = matches and (system is None or item["system"] == system)
        matches = matches and (not content_type or detail.get("content_type") == content_type)
        if matches and start <= event["timestamp"] < end:
            facts.append({"event_id": event["event_id"], "timestamp": event["timestamp"], "decision_id": event["logical_turn_id"],
                          "reply_id": rid, "session_id": event["session_id"], "event_type": event["event_type"],
                          "reason": event["reason_code"], "detail": detail, "metrics": increments})
    states = {}
    for item in replies.values():
        statuses = [message["status"] for message in item["messages"]]
        successes = statuses.count("sent")
        if item["started_at"] is None and not statuses and item["generated"]:
            state = "generation_only"
        elif not item["closed"] or "unknown" in statuses:
            state = "awaiting_confirmation"
        elif successes:
            required_incomplete = any(m["status"] != "sent" and not (m["optional"] and m["status"] == "skipped") for m in item["messages"])
            state = "partial_success" if required_incomplete else "all_success"
        elif statuses and all(status == "skipped" for status in statuses):
            state = "all_skipped"
        elif "failed" in statuses or item["action_failed"]:
            state = "all_failed"
        else:
            state = "all_skipped"
        item["state"] = state
        item["partial_completion"] = bool(successes and state != "all_success")
        item["sent_text"] = sum(len(m["platform_message_ids"]) for m in item["messages"] if m["has_text"])
        item["sent_emoji"] = sum(len(m["platform_message_ids"]) for m in item["messages"] if m["has_emoji"])
        states[(item["session_id"], item["reply_id"])] = item
    counts = {key: sum(key in fact["metrics"] for fact in facts) for key in LABELS}
    cohort = { (fact["session_id"], fact["reply_id"]) for fact in facts if "logical_replies" in fact["metrics"] }
    selected = [states[key] for key in cohort]
    counts["partial_replies"] = sum(item["state"] == "partial_success" for item in selected)
    return {"schema_version": 2, "timezone": "Asia/Shanghai", "window": {"start": start, "end": end, "boundary": "[start,end)"},
            "labels": LABELS, "counts": counts, "current_states_of_window_replies": {state: sum(r["state"] == state for r in selected)
                for state in ("all_success", "partial_success", "all_failed", "all_skipped", "awaiting_confirmation")},
            "records": facts, "replies": selected, "all_replies": [item for item in states.values() if (not session_id or item["session_id"] == session_id)
                            and (not source or item["source"] == source) and (not session_type or item["session_type"] == session_type)
                            and (system is None or item["system"] == system)],
            "scope": "retained v2 events; not lifetime totals; legacy events are not reclassified"}


def persist_delivery_state(connection, event: Dict[str, Any]) -> None:
    """Minimal idempotency ledger survives timeline retention; never a source of metric counts."""
    detail = event["detail"]
    oid = detail.get("outbound_message_id")
    if not oid:
        return
    key = (event["session_id"], oid)
    row = connection.execute("SELECT * FROM outbound_dispatches WHERE session_id=? AND outbound_message_id=?", key).fetchone()
    old_status = row["status"] if row else "planned"
    ids = json.loads(row["platform_message_ids"]) if row else []
    status = {"reply_stats.send_attempt": "unknown", "reply_stats.send_unknown": "unknown",
              "reply_stats.send_failed": "failed", "reply_stats.send_skipped": "skipped",
              "reply_stats.send_confirmed": "sent"}.get(event["event_type"], old_status)
    if old_status == "sent":
        status = "sent"
    pid = detail.get("platform_message_id")
    if pid and pid not in ids:
        ids.append(pid)
    connection.execute("INSERT INTO outbound_dispatches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(session_id,outbound_message_id) DO UPDATE SET status=excluded.status, "
        "platform_message_ids=excluded.platform_message_ids,content_type=excluded.content_type,has_text=excluded.has_text,"
        "has_emoji=excluded.has_emoji,last_event_at=MAX(last_event_at,excluded.last_event_at)",
        (*key, detail["reply_id"], event["logical_turn_id"], detail.get("source", "unknown"),
         detail.get("session_type", "unknown"), int(detail.get("system", False)), detail.get("content_type", "other"),
         int(detail.get("has_text", row["has_text"] if row else detail.get("content_type") == "text")),
         int(detail.get("has_emoji", row["has_emoji"] if row else detail.get("content_type") == "emoji")),
         status, json.dumps(ids), event["timestamp"]))


def statistics(store, *, read_only: bool = False, max_events: Optional[int] = None, **filters: Any) -> Dict[str, Any]:
    with closing(store.connect_readonly() if read_only else store.connect()) as connection:
        connection.execute("BEGIN")
        if max_events is not None:
            count = connection.execute("SELECT COUNT(*) FROM events WHERE event_type LIKE 'reply_stats.%'").fetchone()[0]
            if count > max_events:
                raise ValueError("保留事件过多，本次不生成不完整统计；请使用 WebUI 查询")
        rows = [dict(row) for row in connection.execute("SELECT * FROM events WHERE event_type LIKE 'reply_stats.%' ORDER BY timestamp,sequence")]
        legacy = connection.execute("SELECT COUNT(*) FROM events WHERE event_type NOT LIKE 'reply_stats.%'").fetchone()[0]
        activated = connection.execute("SELECT value FROM replay_metadata WHERE key='reply_metrics_started_at'").fetchone()
    result = project(rows, **filters)
    result["effective_since"] = float(activated[0]) if activated else None
    result["legacy_event_count"] = legacy
    result["health"] = {"dropped_events": store.dropped, "write_errors": store.errors, "queue_size": store.queue.qsize()}
    return result
