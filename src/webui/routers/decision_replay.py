"""Authenticated read-only decision replay. Sync endpoints use the WebUI thread pool."""

from contextlib import closing
from typing import Optional

import json

from fastapi.responses import Response
from fastapi import APIRouter, Depends, HTTPException, Query

from src.decision_replay.recorder import enabled, retention_days, store
from src.webui.dependencies import require_auth

router = APIRouter(prefix="/api/webui/decision-replay", tags=["decision-replay"], dependencies=[Depends(require_auth)])


@router.get("")
def list_turns(
    session_id: str = "",
    start: float = Query(0, ge=0),
    end: Optional[float] = Query(None, ge=0),
    source: str = "",
    gate: str = "",
    planner: str = "",
    emoji: str = "",
    spoken: Optional[bool] = None,
    failed: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ascending: bool = False,
):
    if end is not None and end < start:
        raise HTTPException(422, "结束时间不能早于开始时间")
    store.retention_days = retention_days()
    result = store.query(
        session_id=session_id,
        start=start,
        end=end,
        source=source,
        gate=gate,
        planner=planner,
        emoji=emoji,
        spoken=spoken,
        failed=failed,
        page=page,
        page_size=page_size,
        ascending=ascending,
    )
    result["enabled"] = enabled()
    result["retention_days"] = retention_days()
    return result


@router.get("/sessions")
def sessions():
    from src.chat.message_receive.chat_manager import chat_manager

    with closing(store.connect()) as connection:
        rows = connection.execute(
            "SELECT session_id,MAX(session_name) name FROM turns GROUP BY session_id LIMIT 1000"
        ).fetchall()
    return [
        {
            "session_id": row["session_id"],
            "name": chat_manager.get_session_name(row["session_id"]) or row["name"] or "未知会话",
        }
        for row in rows
    ]


def reply_statistics_result(session_id: str, start: float, end: Optional[float], source: str,
                            session_type: str, content_type: str, system: Optional[bool]):
    from src.decision_replay.reply_metrics import statistics
    if end is not None and end < start:
        raise HTTPException(422, "结束时间不能早于开始时间")
    result = statistics(store, session_id=session_id, start=start, end=end, source=source,
                        session_type=session_type, content_type=content_type, system=system)
    result["enabled"] = enabled()
    return result


@router.get("/statistics")
def reply_statistics(session_id: str = "", start: float = Query(0, ge=0), end: Optional[float] = Query(None, ge=0),
                     source: str = "", session_type: str = "", content_type: str = "", system: Optional[bool] = None):
    return reply_statistics_result(session_id, start, end, source, session_type, content_type, system)


@router.get("/statistics/export")
def export_reply_statistics(session_id: str = "", start: float = Query(0, ge=0), end: Optional[float] = Query(None, ge=0),
                            source: str = "", session_type: str = "", content_type: str = "", system: Optional[bool] = None):
    result = reply_statistics_result(session_id, start, end, source, session_type, content_type, system)
    return Response(json.dumps(result, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="reply-statistics-v2.json"'})


@router.get("/{turn_id}")
def turn_detail(turn_id: str, session_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    store.retention_days = retention_days()
    result = store.detail(turn_id, session_id, after=after, limit=limit)
    if result is None:
        raise HTTPException(404, "未找到此会话的决策回合")
    from src.decision_replay.reply_metrics import statistics
    metrics = statistics(store, session_id=session_id)
    result["reply_statistics"] = [reply for reply in metrics["all_replies"] if reply["decision_id"] == turn_id]
    result["metric_records"] = [event for event in metrics["records"] if event["decision_id"] == turn_id]
    return result


@router.get("/{turn_id}/requests")
def request_page(turn_id: str, session_id: str, page: int = Query(1, ge=1), limit: int = Query(100, ge=1, le=200)):
    return store.request_page(turn_id, session_id, page, limit)
