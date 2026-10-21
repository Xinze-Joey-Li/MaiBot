"""Bounded, read-only projections. Never initializes any of the three databases."""

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, Optional
import json
import sqlite3
import time

from .read_authorization import ReadAuthorization, authorized_read


@contextmanager
def read_database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + 3
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def tables(connection: sqlite3.Connection) -> FrozenSet[str]:
    return frozenset(row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def decoded(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    for key in ("feature_snapshot", "evidence", "detail", "message_event_ids", "tool_names"):
        if key in item:
            item[key] = json.loads(item[key])
    # Stable IDs are useful; raw QQ account/sender identifiers are not an admin UI requirement.
    for key in ("sender_id", "account_id", "route_scope"):
        item.pop(key, None)
    if isinstance(item.get("detail"), dict):
        item["detail"] = {k: v for k, v in item["detail"].items() if k not in {"account_id", "route_scope"}}
    return item


class DashboardReader:
    def __init__(
        self, path: Path, chat_path: Path, replay_path: Path, authorized: FrozenSet[str],
        *, read_authorization: Optional[ReadAuthorization] = None,
    ) -> None:
        self.path, self.chat_path, self.replay_path = path, chat_path, replay_path
        self.read_authorization = read_authorization
        # Static scopes remain useful for offline SQLite diagnostics. HTTP readers
        # always bind the request's epoch rather than treating that scope as proof.
        self.authorized = read_authorization.sessions if read_authorization is not None else authorized

    def _scope(self, session_id: str = "") -> tuple:
        if session_id and session_id not in self.authorized:
            raise PermissionError("Session outside current observation authorization")
        ids = sorted({session_id} if session_id else self.authorized)
        return "o.session_id IN (" + (",".join("?" for _ in ids) or "NULL") + ")", ids

    @authorized_read
    def sessions(self) -> list:
        if not self.chat_path.exists() or not self.authorized:
            return []
        with read_database(self.chat_path) as connection:
            if "chat_sessions" not in tables(connection):
                return []
            # Only real registered QQ groups, never synthesized hashes or private sessions.
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT session_id,COALESCE(NULLIF(group_name,''),'未知群聊') name FROM chat_sessions "
                    "WHERE platform='qq' AND group_id IS NOT NULL AND session_id IN ("
                    + ",".join("?" for _ in self.authorized)
                    + ") ORDER BY name,session_id",
                    sorted(self.authorized),
                )
            ]

    @authorized_read
    def health(self) -> dict:
        result = {"database_exists": self.path.exists(), "schema_version": None, "stored_counts": {}}
        if self.path.exists():
            with read_database(self.path) as connection:
                result["schema_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
                available = tables(connection)
                scope, args = self._scope()
                for name in (
                    "opportunities",
                    "message_events",
                    "evaluations",
                    "execution_events",
                    "reply_events",
                    "delivery_receipts",
                ):
                    if name not in available:
                        result["stored_counts"][name] = None
                    elif name in {"opportunities", "message_events", "delivery_receipts"}:
                        result["stored_counts"][name] = connection.execute(
                            f"SELECT COUNT(*) FROM {name} o WHERE {scope}", args
                        ).fetchone()[0]
                    else:
                        result["stored_counts"][name] = connection.execute(
                            f"SELECT COUNT(*) FROM {name} r JOIN opportunities o USING(opportunity_id) WHERE {scope}",
                            args,
                        ).fetchone()[0]
        return result

    @staticmethod
    def _projection(available: FrozenSet[str]) -> str:
        gate = "COALESCE((SELECT gate_result FROM evaluations e WHERE e.opportunity_id=o.opportunity_id ORDER BY evaluation_sequence DESC LIMIT 1),'not_observed')"
        planner = "'not_observed'"
        if "execution_events" in available:
            planner = "CASE WHEN EXISTS(SELECT 1 FROM execution_events e WHERE e.opportunity_id=o.opportunity_id AND e.stage='planner_started') THEN 'started' WHEN o.source_type='priority_at' THEN 'not_applicable' ELSE 'not_observed' END"
        generation, send = "'not_observed'", "'not_observed'"
        if "reply_events" in available:
            generation = "COALESCE((SELECT status FROM reply_events r WHERE r.opportunity_id=o.opportunity_id AND r.stage='generation' ORDER BY occurred_at DESC,event_id DESC LIMIT 1),(SELECT status FROM reply_events r WHERE r.opportunity_id=o.opportunity_id AND r.stage='direct_emoji' AND r.status='not_applicable' LIMIT 1),'not_observed')"
            send = "CASE WHEN EXISTS(SELECT 1 FROM reply_events r WHERE r.opportunity_id=o.opportunity_id AND r.stage='driver_send' AND r.status='send_attempted') THEN 'send_attempted' ELSE 'not_observed' END"
        confirmation = "'not_observed'"
        if {"delivery_links", "delivery_receipts", "reply_events"} <= available:
            # Each status is a presence summary, not a claim that every segment succeeded.
            confirmation = "COALESCE((SELECT d.conclusion FROM delivery_receipts d JOIN delivery_links l USING(receipt_id) JOIN reply_events r ON r.event_id=l.parent_event_id WHERE r.opportunity_id=o.opportunity_id ORDER BY CASE d.conclusion WHEN 'platform_confirmed' THEN 0 WHEN 'definitive_failure' THEN 1 ELSE 2 END,d.observed_at DESC LIMIT 1),'not_observed')"
        return f"SELECT o.*,{gate} gate,{planner} planner,{generation} generation,{send} send,{confirmation} confirmation,(SELECT COUNT(DISTINCT message_event_id) FROM opportunity_messages m WHERE m.opportunity_id=o.opportunity_id) message_count FROM opportunities o"

    @authorized_read
    def listing(
        self, *, session_id: str = "", start: float, end: float, page: int = 1, page_size: int = 25, **filters: str
    ) -> dict:
        scope, args = self._scope(session_id)
        if not (0 <= start <= end and end - start <= 31 * 86400 and 1 <= page <= 10000 and 1 <= page_size <= 100):
            raise ValueError("查询时间范围最多31天，页码1..10000，每页1..100")
        result = {
            "items": [],
            "total": 0,
            "summary": {},
            "window": {"start": start, "end": end},
            "page": page,
            "page_size": page_size,
            "units": "所选机会创建时间范围；事件按 occurrence / turn / attempt 去重；存在确认不代表全部分段确认",
        }
        if not self.path.exists():
            return result
        with read_database(self.path) as connection:
            available = tables(connection)
            base = self._projection(available) + f" WHERE {scope} AND o.created_at>=? AND o.created_at<=?"
            args += [start, end]
            predicates = []
            for key in ("source_type", "gate", "planner", "generation", "send", "confirmation"):
                if filters.get(key):
                    predicates.append(f"{key}=?")
                    args.append(filters[key])
            sql = "WITH selected AS (" + base + "), filtered AS (SELECT * FROM selected"
            sql += (" WHERE " + " AND ".join(predicates) if predicates else "") + ") "
            result["total"] = connection.execute(sql + "SELECT COUNT(*) FROM filtered", args).fetchone()[0]
            result["items"] = [
                decoded(row)
                for row in connection.execute(
                    sql + "SELECT * FROM filtered ORDER BY created_at DESC,opportunity_id DESC LIMIT ? OFFSET ?",
                    [*args, page_size, (page - 1) * page_size],
                )
            ]

            def count(expression: str) -> int:
                return connection.execute(sql + expression, args).fetchone()[0]

            summary = {"opportunities": result["total"]}
            summary["message_events"] = count(
                "SELECT COUNT(DISTINCT m.message_event_id) FROM opportunity_messages m JOIN filtered o USING(opportunity_id)"
            )
            for status in ("pass", "reject", "defer"):
                summary["gate_" + status] = count(
                    "SELECT COUNT(*) FROM evaluations e JOIN filtered o USING(opportunity_id) WHERE e.gate_result='"
                    + status
                    + "'"
                )
            summary["evaluations"] = count("SELECT COUNT(*) FROM evaluations e JOIN filtered o USING(opportunity_id)")
            summary["planner_turns"] = (
                count(
                    "SELECT COUNT(DISTINCT o.session_id||':'||e.logical_turn_id) FROM execution_events e JOIN filtered o USING(opportunity_id) WHERE e.stage='planner_started'"
                )
                if "execution_events" in available
                else None
            )
            summary["planner_starts"] = (
                count(
                    "SELECT COUNT(DISTINCT o.session_id||':'||e.execution_id) FROM execution_events e JOIN filtered o USING(opportunity_id) WHERE e.stage='planner_started'"
                )
                if "execution_events" in available
                else None
            )
            for name, condition in (
                ("generation_calls", "r.stage='generation' AND r.status='generation_started'"),
                ("driver_attempts", "r.stage='driver_send' AND r.status='send_attempted'"),
            ):
                summary[name] = (
                    count(
                        "SELECT COUNT(DISTINCT r.occurrence_id) FROM reply_events r JOIN filtered o USING(opportunity_id) WHERE "
                        + condition
                    )
                    if "reply_events" in available
                    else None
                )
            for status in ("platform_confirmed", "definitive_failure", "unknown", "not_observed"):
                summary[status + "_opportunities"] = (
                    count("SELECT COUNT(*) FROM filtered WHERE confirmation='" + status + "'")
                    if "delivery_receipts" in available
                    else None
                )
            summary["platform_unique_messages"] = None
            if {"reply_events", "delivery_receipts", "delivery_links"} <= available:
                attempts = "SELECT o.session_id,r.attempt_id,r.outbound_id,MAX(d.conclusion='platform_confirmed') confirmed,MAX(d.conclusion='definitive_failure') failed,MAX(d.conclusion='not_applicable') local,COUNT(DISTINCT d.receipt_id) receipts FROM reply_events r JOIN filtered o USING(opportunity_id) LEFT JOIN delivery_links l ON l.parent_event_id=r.event_id LEFT JOIN delivery_receipts d ON d.receipt_id=l.receipt_id AND d.session_id=o.session_id WHERE r.stage='driver_send' AND r.status='send_attempted' GROUP BY o.session_id,r.attempt_id,r.outbound_id"
                for name, condition in (
                    ("confirmed_attempts", "confirmed=1"),
                    ("definitive_failure_attempts", "COALESCE(confirmed,0)=0 AND failed=1"),
                    (
                        "unknown_attempts",
                        "COALESCE(confirmed,0)=0 AND COALESCE(failed,0)=0 AND COALESCE(local,0)=0 AND receipts>0",
                    ),
                    ("unobserved_attempts", "receipts=0"),
                    ("local_attempts", "COALESCE(confirmed,0)=0 AND COALESCE(failed,0)=0 AND local=1"),
                    ("conflicting_attempts", "confirmed=1 AND failed=1"),
                ):
                    summary[name] = count("SELECT COUNT(*) FROM (" + attempts + ") WHERE " + condition)
                # Same tuple as Reply Metrics: platform, account, real session, external message ID.
                summary["platform_unique_messages"] = count(
                    "SELECT COUNT(*) FROM (SELECT d.platform,COALESCE(d.account_id,''),d.session_id,CASE WHEN COALESCE(d.platform_message_id,'')!='' THEN d.platform_message_id ELSE 'delivery:'||d.delivery_id END confirmed_identity FROM delivery_receipts d JOIN delivery_links l USING(receipt_id) JOIN reply_events r ON r.event_id=l.parent_event_id JOIN filtered o ON o.opportunity_id=r.opportunity_id AND o.session_id=d.session_id WHERE d.conclusion='platform_confirmed' AND (COALESCE(d.platform_message_id,'')!='' OR (d.acknowledged=1 AND COALESCE(d.delivery_id,'')!='')) GROUP BY d.platform,COALESCE(d.account_id,''),d.session_id,confirmed_identity)"
                )
            result["summary"] = summary
        names = {row["session_id"]: row["name"] for row in self.sessions()}
        for item in result["items"]:
            item["session_name"] = names.get(item["session_id"], "群聊名称不可用")
        return result

    @authorized_read
    def detail(self, opportunity_id: str, *, page: int = 1, page_size: int = 50) -> Optional[dict]:
        if not self.path.exists():
            return None
        if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            raise ValueError("Invalid timeline page")
        scope, args = self._scope()
        with read_database(self.path) as connection:
            available = tables(connection)
            row = connection.execute(
                self._projection(available) + f" WHERE {scope} AND o.opportunity_id=?", [*args, opportunity_id]
            ).fetchone()
            if row is None:
                return None
            result = {"opportunity": decoded(row), "groups": {}, "replay": [], "page": page, "page_size": page_size}
            for name, table in (
                ("delivery_receipts", "delivery_receipts"),
                ("attempt_states", "delivery_receipts"),
                ("session_unlinked_receipts", "delivery_receipts"),
                ("transfers", "opportunity_transfers"),
            ):
                result["groups"][name] = {"items": [], "total": None, "available": table in available}
            result["opportunity"]["session_name"] = next(
                (s["name"] for s in self.sessions() if s["session_id"] == row["session_id"]), "群聊名称不可用"
            )
            message_sql = " FROM message_events m WHERE m.session_id=? AND EXISTS(SELECT 1 FROM opportunity_messages om WHERE om.message_event_id=m.message_event_id AND om.opportunity_id=?)"
            message_args = [row["session_id"], opportunity_id]
            result["groups"]["message_events"] = {
                "items": [
                    decoded(r)
                    for r in connection.execute(
                        "SELECT m.*"
                        + message_sql
                        + " ORDER BY COALESCE(received_timestamp,event_timestamp),message_event_id LIMIT ? OFFSET ?",
                        [*message_args, page_size, (page - 1) * page_size],
                    )
                ],
                "total": connection.execute("SELECT COUNT(*)" + message_sql, message_args).fetchone()[0],
                "available": True,
            }
            for name, order in (
                ("evaluations", "evaluated_at,rowid"),
                ("execution_events", "occurred_at,rowid"),
                ("reply_events", "occurred_at,rowid"),
            ):
                if name not in available:
                    result["groups"][name] = {"items": [], "total": None, "available": False}
                    continue
                total = connection.execute(
                    f"SELECT COUNT(*) FROM {name} WHERE opportunity_id=?", (opportunity_id,)
                ).fetchone()[0]
                rows = connection.execute(
                    f"SELECT rowid AS record_sequence,* FROM {name} WHERE opportunity_id=? ORDER BY {order} LIMIT ? OFFSET ?",
                    (opportunity_id, page_size, (page - 1) * page_size),
                )
                result["groups"][name] = {"items": [decoded(r) for r in rows], "total": total, "available": True}
            if "delivery_receipts" in available:
                sql = " FROM delivery_receipts d WHERE d.session_id=? AND EXISTS(SELECT 1 FROM delivery_links l JOIN reply_events r ON r.event_id=l.parent_event_id WHERE l.receipt_id=d.receipt_id AND r.opportunity_id=?)"
                args = [row["session_id"], opportunity_id]
                total = connection.execute("SELECT COUNT(*)" + sql, args).fetchone()[0]
                receipts = [
                    decoded(r)
                    for r in connection.execute(
                        "SELECT d.*" + sql + " ORDER BY observed_at,receipt_id LIMIT ? OFFSET ?",
                        [*args, page_size, (page - 1) * page_size],
                    )
                ]
                for receipt in receipts:
                    receipt["platform_confirmed_at"] = None
                    receipt["links_total"] = connection.execute(
                        "SELECT COUNT(*) FROM delivery_links l JOIN reply_events r ON l.parent_event_id=r.event_id WHERE l.receipt_id=? AND r.opportunity_id=?",
                        (receipt["receipt_id"], opportunity_id),
                    ).fetchone()[0]
                    receipt["links_truncated"] = receipt["links_total"] > 100
                    receipt["links"] = [
                        dict(link)
                        for link in connection.execute(
                            "SELECT l.parent_event_id,l.linked_at,r.attempt_id,r.outbound_id FROM delivery_links l JOIN reply_events r ON l.parent_event_id=r.event_id WHERE l.receipt_id=? AND r.opportunity_id=? LIMIT 100",
                            (receipt["receipt_id"], opportunity_id),
                        )
                    ]
                result["groups"]["delivery_receipts"] = {"items": receipts, "total": total, "available": True}
                attempts_sql = "SELECT r.attempt_id,r.outbound_id,MIN(r.occurred_at) occurred_at,COUNT(DISTINCT d.receipt_id) receipt_count,MAX(d.conclusion='platform_confirmed') confirmed,MAX(d.conclusion='definitive_failure') failed,MAX(d.conclusion='not_applicable') local FROM reply_events r LEFT JOIN delivery_links l ON l.parent_event_id=r.event_id LEFT JOIN delivery_receipts d ON d.receipt_id=l.receipt_id AND d.session_id=? WHERE r.opportunity_id=? AND r.stage='driver_send' AND r.status='send_attempted' GROUP BY r.attempt_id,r.outbound_id"
                attempt_count = connection.execute("SELECT COUNT(*) FROM (" + attempts_sql + ")", args).fetchone()[0]
                states = []
                for attempt in connection.execute(
                    attempts_sql + " ORDER BY occurred_at,r.attempt_id,r.outbound_id LIMIT ? OFFSET ?",
                    [*args, page_size, (page - 1) * page_size],
                ):
                    state = dict(attempt)
                    state["status"] = (
                        "platform_confirmed"
                        if state["confirmed"]
                        else "definitive_failure"
                        if state["failed"]
                        else "not_applicable"
                        if state["local"]
                        else "unknown"
                        if state["receipt_count"]
                        else "not_observed"
                    )
                    state["conflicting_evidence"] = bool(state["confirmed"] and state["failed"])
                    states.append(state)
                result["groups"]["attempt_states"] = {"items": states, "total": attempt_count, "available": True}
                # Session diagnostics are deliberately not opportunity links. An ambiguous
                # positive receipt remains unknown for every unassigned attempt.
                unlinked_sql = " FROM delivery_receipts d WHERE d.session_id=? AND NOT EXISTS(SELECT 1 FROM delivery_links l WHERE l.receipt_id=d.receipt_id)"
                unlinked = []
                for receipt in connection.execute(
                    "SELECT d.*" + unlinked_sql + " ORDER BY observed_at,receipt_id LIMIT ? OFFSET ?",
                    (row["session_id"], page_size, (page - 1) * page_size),
                ):
                    item = decoded(receipt)
                    item["raw_conclusion"] = item.pop("conclusion")
                    item["status"] = "unknown"
                    item["associated_with_this_opportunity"] = False
                    item["linkage_status"] = "unlinked_or_ambiguous"
                    unlinked.append(item)
                result["groups"]["session_unlinked_receipts"] = {
                    "items": unlinked,
                    "total": connection.execute("SELECT COUNT(*)" + unlinked_sql, (row["session_id"],)).fetchone()[0],
                    "available": True,
                }
            if "opportunity_transfers" in available:
                transfers = connection.execute(
                    "SELECT * FROM opportunity_transfers WHERE source_opportunity_id=? OR target_opportunity_id=? ORDER BY occurred_at,transfer_id LIMIT ? OFFSET ?",
                    (opportunity_id, opportunity_id, page_size, (page - 1) * page_size),
                )
                result["groups"]["transfers"] = {
                    "items": [decoded(r) for r in transfers],
                    "total": connection.execute(
                        "SELECT COUNT(*) FROM opportunity_transfers WHERE source_opportunity_id=? OR target_opportunity_id=?",
                        (opportunity_id, opportunity_id),
                    ).fetchone()[0],
                    "available": True,
                }
            if "execution_events" in available:
                result["replay_total"] = connection.execute(
                    "SELECT COUNT(DISTINCT logical_turn_id) FROM execution_events WHERE opportunity_id=?",
                    (opportunity_id,),
                ).fetchone()[0]
                result["replay_truncated"] = result["replay_total"] > 100
                turns = connection.execute(
                    "SELECT DISTINCT logical_turn_id FROM execution_events WHERE opportunity_id=? AND logical_turn_id IS NOT NULL ORDER BY logical_turn_id LIMIT 100",
                    (opportunity_id,),
                ).fetchall()
                present_turns = set()
                if turns and self.replay_path.exists():
                    with read_database(self.replay_path) as replay:
                        if "turns" in tables(replay):
                            present_turns = {
                                r[0]
                                for r in replay.execute(
                                    "SELECT logical_turn_id FROM turns WHERE session_id=? AND logical_turn_id IN ("
                                    + ",".join("?" for _ in turns)
                                    + ")",
                                    [row["session_id"], *[t[0] for t in turns]],
                                )
                            }
                for turn in turns:
                    state = (
                        "available"
                        if turn[0] in present_turns
                        else "expired_or_missing"
                        if self.replay_path.exists()
                        else "replay_unavailable"
                    )
                    result["replay"].append({"logical_turn_id": turn[0], "status": state})
            result["limitations"] = [
                "分组独立分页；按时间与稳定ID排序。",
                "回执为已关联事实；driver返回True或SENT本身不代表平台确认。",
                "未关联/歧义回执不归属到本机会；不能按时间补链。",
                "生成内容、Prompt与推理内容不保存。",
            ]
            return result

    @authorized_read
    def context(self, opportunity_id: str, evaluation_id: str, *, page: int = 1, page_size: int = 25,
                annotation_view: bool = False, bot_accounts: Optional[FrozenSet[str]] = frozenset()) -> Optional[dict]:
        if not self.path.exists():
            return None
        if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            raise ValueError("Invalid context page")
        scope, args = self._scope()
        with read_database(self.path) as connection:
            if evaluation_id:
                row = connection.execute(
                    f"SELECT e.*,o.session_id FROM evaluations e JOIN opportunities o USING(opportunity_id) WHERE {scope} AND o.opportunity_id=? AND e.evaluation_id=?",
                    [*args, opportunity_id, evaluation_id],
                ).fetchone()
            else:
                # Priority candidates need no gate evaluation; these are association rows,
                # explicitly NOT an immutable evaluation snapshot or a Planner input claim.
                row = connection.execute(
                    f"SELECT o.session_id,o.created_at AS evaluated_at,'{{}}' AS feature_snapshot FROM opportunities o WHERE {scope} AND o.opportunity_id=?",
                    [*args, opportunity_id],
                ).fetchone()
            if row is None:
                return None
            snapshot = json.loads(row["feature_snapshot"])
            ids = snapshot.get("message_event_ids")
            if not evaluation_id:
                ids = [
                    r[0]
                    for r in connection.execute(
                        "SELECT DISTINCT message_event_id FROM opportunity_messages WHERE opportunity_id=? LIMIT 513",
                        (opportunity_id,),
                    )
                ]
            if isinstance(ids, list) and len(ids) > 512:
                raise ValueError("关联消息超过安全读取上限；请缩小查询，不能伪造完整上下文")
            result = {
                "evaluation_id": evaluation_id,
                "evaluated_at": row["evaluated_at"],
                "items": [],
                "total": 0,
                "membership_available": bool(evaluation_id) and isinstance(ids, list),
                "association_only": not bool(evaluation_id),
                "page": page,
                "page_size": page_size,
                "limitations": [
                    "评估快照成员是门控关联消息，不等于完整Planner Prompt。",
                    "参考消息仅限本会话有可靠接收时间且不晚于评估的观察记录；未采集消息不展示。",
                    "主聊天库无接收时刻，不能仅靠平台时间补齐历史或断言完整顺序。",
                    "无证据不标记唯一触发消息；不解码原始消息/模型内容。",
                    "未选择评估时仅展示机会累计关联，不代表决策快照；参考窗口以机会创建时刻为界。",
                    "正文来自当前主聊天库，不是采集时的正文快照；相同时刻的稳定排序不证明完整因果顺序。",
                ],
            }
            # The candidate list itself is SQL paginated; future-received messages never become reference context.
            ids = ids if isinstance(ids, list) else []
            result["expected_associated_count"] = len(set(ids))
            if annotation_view:
                from .review_presentation import machine_summary

                result["machine"] = machine_summary(connection, opportunity_id, evaluation_id or None, row)
            result["unavailable_message_event_ids"] = [
                mid
                for mid in ids
                if connection.execute(
                    "SELECT 1 FROM message_events WHERE session_id=? AND message_event_id=?", (row["session_id"], mid)
                ).fetchone()
                is None
            ]
            placeholders = ",".join("?" for _ in ids) or "NULL"
            predicate = f"session_id=? AND (message_event_id IN ({placeholders}) OR (received_timestamp IS NOT NULL AND received_timestamp<=? AND event_timestamp<=? AND received_timestamp>=?))"
            args = [row["session_id"], *ids, row["evaluated_at"], row["evaluated_at"], row["evaluated_at"] - 600]
            result["total"] = connection.execute(
                "SELECT COUNT(*) FROM message_events WHERE " + predicate, args
            ).fetchone()[0]
            messages = connection.execute(
                "SELECT * FROM message_events WHERE "
                + predicate
                + " ORDER BY COALESCE(received_timestamp,event_timestamp),message_event_id LIMIT ? OFFSET ?",
                [*args, page_size, (page - 1) * page_size],
            ).fetchall()
            if not self.chat_path.exists():
                result["context_status"] = "chat_database_unavailable"
                return result
            with read_database(self.chat_path) as chat:
                if "mai_messages" not in tables(chat):
                    result["context_status"] = "chat_table_unavailable"
                    return result
                if annotation_view:
                    from .review_history import bot_history
                    from .review_presentation import ReviewPrivacy

                    accounts = bot_accounts if bot_accounts is not None else frozenset()
                    privacy = ReviewPrivacy(chat, row["session_id"], opportunity_id, accounts)
                    result["limitations"][1] = (
                        "群友参考消息仅限本会话有可靠接收时间且不晚于评估的观察记录；"
                        "机器人历史发言另以决策前平台确认恢复，不代表模型输入。"
                    )
                    result["privacy_replacements"] = privacy.replacements
                    history, history_status = bot_history(connection, chat, row["session_id"], row["evaluated_at"],
                                                     accounts, predicate, args)
                    result["bot_history_status"] = ("identity_unavailable" if bot_accounts is None else
                                                    "identity_missing" if not accounts else
                                                    history_status)
                    if history:
                        # All omitted observation rows precede this page even if
                        # every bot row sorts before them. Read a bounded overlap,
                        # merge once, then paginate the unified set (no page-local merge).
                        start = (page - 1) * page_size
                        base = max(0, start - len(history))
                        observed_page = connection.execute(
                            "SELECT * FROM message_events WHERE " + predicate
                            + " ORDER BY COALESCE(received_timestamp,event_timestamp),message_event_id LIMIT ? OFFSET ?",
                            [*args, page_size + len(history), base],
                        ).fetchall()
                        merged = [dict(message) for message in observed_page] + history
                        merged.sort(key=lambda message: (message["received_timestamp"] if message["received_timestamp"]
                                                         is not None else message["event_timestamp"],
                                                         message["message_event_id"]))
                        messages = merged[start - base:start - base + page_size]
                        result["total"] += len(history)
                    result["limitations"].append(
                        "机器人历史发言仅使用10分钟内、决策前已记录的平台确认；确认时刻不是精确发送时刻。"
                        "正文来自当前聊天库，不证明决策前已入库、未被修改或Planner已读取；相同时间不证明因果先后。"
                    )
                    result["target_items"] = []
                    # Fetch snapshot candidates separately so the target remains visible
                    # even when its ordinary context page has not been opened.
                    candidates = connection.execute(
                        f"SELECT * FROM message_events WHERE session_id=? AND message_event_id IN ({placeholders}) "
                        "ORDER BY COALESCE(received_timestamp,event_timestamp),message_event_id",
                        [row["session_id"], *ids],
                    ).fetchall()
                    messages = [(message, False) for message in messages] + [(message, True) for message in candidates]
                else:
                    messages = [(message, False) for message in messages]
                for message, target_preview in messages:
                    if annotation_view and isinstance(message, dict) and message.get("_history"):
                        item = {key: value for key, value in message.items() if key not in {"_history", "received_timestamp"}}
                        item["text"] = privacy.text(item["text"])
                        result["items"].append(item)
                        continue
                    # Exact session/platform ID identity only. Duplicates are ambiguous, never newest-row guesses.
                    matches = chat.execute(
                        "SELECT timestamp,user_id,user_nickname,user_cardname,processed_plain_text,is_at,is_mentioned,reply_to FROM mai_messages WHERE session_id=? AND platform='qq' AND message_id=? LIMIT 2",
                        (row["session_id"], message["platform_message_id"]),
                    ).fetchall()
                    if (annotation_view and len(matches) == 1 and message["sender_id"]
                            and str(matches[0]["user_id"]) != str(message["sender_id"])):
                        # A matching message ID cannot override contradictory sender evidence.
                        matches = []
                    item = {
                        "message_event_id": message["message_event_id"],
                        "membership": ("evaluation_member" if evaluation_id else "opportunity_associated")
                        if message["message_event_id"] in ids
                        else "reference_only",
                        "received_at": message["received_timestamp"],
                        "event_timestamp": message["event_timestamp"],
                        "status": "available" if len(matches) == 1 else "missing_or_ambiguous",
                    }
                    if len(matches) == 1:
                        stored = matches[0]
                        name = stored["user_cardname"] or stored["user_nickname"] or "匿名成员"
                        if name == stored["user_id"] or name.isdigit():
                            name = "匿名成员"
                        item.update(
                            {
                                "name": name,
                                "text": (stored["processed_plain_text"] or "")[:4000],
                                "text_truncated": len(stored["processed_plain_text"] or "") > 4000,
                                "is_at": bool(stored["is_at"]),
                                "is_mentioned": bool(stored["is_mentioned"]),
                                "reply_to": stored["reply_to"],
                            }
                        )
                    if annotation_view:
                        item["is_bot"] = len(matches) == 1 and matches[0]["user_id"] in accounts
                        item["name"] = privacy.sender(
                            matches[0]["user_id"] if len(matches) == 1 else None, message["message_event_id"]
                        )
                        if "text" in item:
                            item["text"] = privacy.text(item["text"])
                    result["target_items" if target_preview else "items"].append(item)
                result["context_status"] = "partial_reconstruction"
            return result
