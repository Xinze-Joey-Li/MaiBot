"""M1 的只读数据适配，复用统计投影，绝不向本机 HTTP 发送管理请求。"""

from contextlib import closing
from typing import Any, Dict, List
import json

from sqlalchemy import text
from sqlmodel import select

from src.common.database.database import get_db_session
from src.common.database.database_model import ChatSession


def known_chats(account_id: str, *, group_id: str = "", limit: int = 501) -> List[ChatSession]:
    """只读取真实聊天流，不计算替代 session_id；不把历史记录当成在线连接。"""
    statement = select(ChatSession).where(ChatSession.platform == "qq", ChatSession.account_id == account_id)
    if group_id:
        statement = statement.where(ChatSession.group_id == group_id)
    with get_db_session(auto_commit=False) as session:
        return list(session.exec(statement.order_by(ChatSession.last_active_timestamp.desc()).limit(limit)).all())


def replay_counts(store: Any, start: float, end: float, sessions: List[str]) -> Dict[str, int]:
    with closing(store.connect_readonly()) as connection:
        rows = connection.execute(
            "SELECT event_type,decision,json_extract(detail,'$.task') task,COUNT(*) count FROM events "
            "WHERE timestamp>=? AND timestamp<? AND session_id IN (SELECT value FROM json_each(?)) "
            "AND (event_type='gate.decided' OR (event_type='model.request' AND decision='started')) "
            "GROUP BY event_type,decision,task", (start, end, json.dumps(sessions)),
        ).fetchall()
    return {
        "gate_checks": sum(row["count"] for row in rows if row["event_type"] == "gate.decided"),
        "planner_requests": sum(row["count"] for row in rows if row["event_type"] == "model.request"
                                and row["task"] == "planner"),
    }


def structured_errors(store: Any, start: float, end: float, sessions: List[str]) -> List[Dict[str, Any]]:
    """仅输出结构化类型与小时计数，不读取堆栈、错误正文、URL 或凭据。"""
    with closing(store.connect_readonly()) as connection:
        rows = connection.execute(
            "SELECT CAST(timestamp/3600 AS INTEGER)*3600 hour,stage,event_type,COUNT(*) count "
            "FROM events WHERE timestamp>=? AND timestamp<? AND decision IN ('failed','error') "
            "AND session_id IN (SELECT value FROM json_each(?)) GROUP BY hour,stage,event_type "
            "ORDER BY hour DESC LIMIT 501", (start, end, json.dumps(sessions)),
        ).fetchall()
    return [dict(row) for row in rows]


def monitor_errors(start: float, end: float, sessions: List[str]) -> List[Dict[str, Any]]:
    with get_db_session(auto_commit=False) as session:
        rows = session.exec(text(
            "SELECT CAST(timestamp/3600 AS INTEGER)*3600 hour,event_type,COUNT(*) count "
            "FROM maisaka_monitor_events WHERE timestamp>=:start AND timestamp<:end "
            "AND event_type='llm.error' AND session_id IN (SELECT value FROM json_each(:sessions)) "
            "GROUP BY hour,event_type ORDER BY hour DESC LIMIT 501"
        ), params={"start": start, "end": end, "sessions": json.dumps(sessions)}).all()
    return [{"hour": row[0], "event_type": row[1], "count": row[2]} for row in rows]


def message_replay(store: Any, *, sessions: List[str], start: float, end: float,
                   message_id: str = "") -> List[Dict[str, Any]]:
    """精确消息 ID 关联，存在多个逻辑轮时返回多项，不能猜一个决策原因。"""
    with closing(store.connect_readonly()) as connection:
        rows = connection.execute(
            "SELECT logical_turn_id,session_id,message_ids,started_at FROM turns WHERE started_at>=? AND started_at<? "
            "AND session_id IN (SELECT value FROM json_each(?)) "
            "AND (?='' OR EXISTS(SELECT 1 FROM json_each(turns.message_ids) WHERE value=?)) "
            "ORDER BY started_at DESC LIMIT 11", (start, end, json.dumps(sessions), message_id, message_id),
        ).fetchall()
    if not message_id:
        rows = rows[:1]
    details = []
    for row in rows:
        detail = store.detail(row["logical_turn_id"], row["session_id"], limit=100, read_only=True)
        # 候选与详情读取之间可能发生正常保留期清理；缺失时不能构造虚假详情。
        if detail is not None:
            details.append(detail)
    return details


def database_probe() -> str:
    with get_db_session(auto_commit=False) as session:
        return "SELECT 1 成功（仅连接可用，不等于完整性检查）" if session.exec(text("SELECT 1")).scalar() == 1 else "异常"
