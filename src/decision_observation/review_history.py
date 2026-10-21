"""Bounded, read-only historical sends. Business timestamps never admit a row."""

from datetime import datetime
from typing import Any, Dict, FrozenSet, List, Set, Tuple
import hashlib
import sqlite3


def bot_history(
    observation: sqlite3.Connection,
    chat: sqlite3.Connection,
    session_id: str,
    boundary: float,
    accounts: FrozenSet[str],
    observation_predicate: str,
    observation_args: List[Any],
) -> Tuple[List[Dict[str, Any]], str]:
    if not accounts:
        return [], "identity_missing"
    available = {row[0] for row in observation.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "delivery_receipts" not in available:
        return [], "send_evidence_unavailable"
    # Both receipt arrival and persistence must be strictly before the decision.
    # A backdated business timestamp or a late ACK cannot introduce future replies.
    placeholders = ",".join("?" for _ in accounts)
    receipts = observation.execute(
        "SELECT * FROM delivery_receipts WHERE session_id=? AND platform='qq' "
        f"AND account_id IN ({placeholders}) AND conclusion='platform_confirmed' AND ambiguous=0 "
        "AND observed_at>=? AND observed_at<? AND recorded_at<? AND observed_at<=recorded_at "
        "ORDER BY observed_at,receipt_id LIMIT 513",
        [session_id, *sorted(accounts), boundary - 600, boundary, boundary],
    ).fetchall()
    if len(receipts) > 512:
        # No truncated subset is presented as a complete merged timeline.
        return [], "read_limit"
    external_ids: Dict[Tuple[str, str], Set[str]] = {}
    for receipt in receipts:
        if receipt["platform_message_id"]:
            external_ids.setdefault((receipt["account_id"], receipt["outbound_id"]), set()).add(receipt["platform_message_id"])
    items: Dict[str, Dict[str, Any]] = {}
    for receipt in receipts:
        conflicting = observation.execute(
            "SELECT 1 FROM delivery_receipts WHERE session_id=? AND account_id=? AND outbound_id=? "
            "AND (ambiguous=1 OR conclusion='definitive_failure') AND recorded_at<? LIMIT 1",
            (session_id, receipt["account_id"], receipt["outbound_id"], boundary),
        ).fetchone()
        if conflicting:
            continue
        external_id = receipt["platform_message_id"]
        if not external_id:
            mappings = external_ids.get((receipt["account_id"], receipt["outbound_id"]), set())
            if len(mappings) > 1:
                continue  # No unique mapping for this ID-less confirmation.
            external_id = next(iter(mappings)) if mappings else None
        identifiers = [value for value in (receipt["outbound_id"], external_id) if value]
        if not identifiers:
            continue
        slots = ",".join("?" for _ in identifiers)
        # External ID can replace the internal outbound ID before chat storage.
        # Fetch at most two rows; duplicates or two different ID matches are not
        # resolved by taking the newest row or comparing message text.
        matches = chat.execute(
            "SELECT rowid AS history_row_id,message_id,user_id,timestamp,processed_plain_text FROM mai_messages "
            f"WHERE session_id=? AND platform='qq' AND message_id IN ({slots}) LIMIT 2",
            [session_id, *identifiers],
        ).fetchall()
        if len(matches) == 1 and matches[0]["user_id"] != receipt["account_id"]:
            continue
        canonical = ("chat-row:" + str(matches[0]["history_row_id"]) if len(matches) == 1 else
                     "send-id:" + (external_id or receipt["outbound_id"]))
        # Do not duplicate a message already admitted by the original observation
        # predicate (including snapshot candidates on another context page).
        observed = observation.execute(
            f"SELECT 1 FROM message_events WHERE ({observation_predicate}) "
            f"AND platform_message_id IN ({slots}) LIMIT 1",
            [*observation_args, *identifiers],
        ).fetchone()
        if observed:
            continue
        identity = hashlib.sha256(f"{session_id}:{receipt['account_id']}:{canonical}".encode()).hexdigest()
        key = "bot_history:" + identity
        if key in items:
            continue
        body_available = len(matches) == 1 and matches[0]["user_id"] == receipt["account_id"]
        if body_available:
            try:
                created_at = datetime.fromisoformat(matches[0]["timestamp"]).timestamp()
                body_available = created_at <= receipt["observed_at"]
            except (TypeError, ValueError, OverflowError):
                body_available = False
            # This can only reject contradictory text. A business timestamp
            # alone never proves the send happened, or when the row was stored.
        text = matches[0]["processed_plain_text"] if body_available else None
        items[key] = {
            "message_event_id": key,
            "membership": "bot_history_reference",
            "status": "available" if body_available else "body_unavailable",
            "name": "大肥鱼",
            "is_bot": True,
            "text": (text or "")[:4000],
            "text_truncated": len(text or "") > 4000,
            "event_timestamp": receipt["observed_at"],
            "received_timestamp": receipt["observed_at"],
            "time_basis": "platform_confirmation",
            "receipt_id": receipt["receipt_id"],
            "confirmation_recorded_at": receipt["recorded_at"],
            "_history": True,
        }
    return list(items.values()), "available"
