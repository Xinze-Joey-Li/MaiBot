"""Admin authenticated observation reads; no collector startup or DB migration."""

from pathlib import Path
from typing import Annotated, Optional
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.decision_observation.dashboard import DashboardReader
from src.decision_observation.read_authorization import ReadAuthorizationExpired
from src.webui.dependencies import require_auth
from src.webui.observation_read import ObservationReadRoute

router = APIRouter(
    prefix="/api/webui/decision-observation", tags=["decision-observation"], dependencies=[Depends(require_auth)],
    route_class=ObservationReadRoute,
)


def reader(request: Request) -> DashboardReader:
    from src.decision_observation.recorder import store

    root = Path(__file__).resolve().parents[3]
    return DashboardReader(
        store.path, root / "data/MaiBot.db", root / "data/decision_replay.db", store.policy.authorized_session_ids,
        read_authorization=request.state.observation_read_authorization,
    )


Reader = Annotated[DashboardReader, Depends(reader)]


def invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ReadAuthorizationExpired:
        if function.__name__ in {"detail", "context"}:
            raise HTTPException(404, "机会不存在或不在授权范围") from None
        raise HTTPException(403, "会话不在当前观察授权范围") from None
    except PermissionError:
        raise HTTPException(403, "会话不在当前观察授权范围") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except (sqlite3.Error, OSError):
        # Do not leak database paths, SQL or message contents in HTTP errors.
        raise HTTPException(503, "只读数据暂不可用或查询超时，请缩小时间范围后重试") from None


@router.get("/health")
def health(service: Reader):
    from src.decision_observation.recorder import store

    result = invoke(service.health)
    result["process"] = store.health()
    result["counter_scope"] = "process_lifetime_only"
    result["authorized_group_count"] = len(service.authorized)
    result["authorization_model"] = "现有WebUI单管理员会话认证；无多用户RBAC；限制当前观察授权群"
    result["sessions"] = invoke(service.sessions)
    return result


def listing(
    service: DashboardReader,
    session_id: str,
    start: Optional[float],
    end: Optional[float],
    page: int,
    page_size: int,
    source_type: str,
    gate: str,
    planner: str,
    generation: str,
    send: str,
    confirmation: str,
):
    end = time.time() if end is None else end
    start = end - 86400 if start is None else start
    return invoke(
        service.listing,
        session_id=session_id,
        start=start,
        end=end,
        page=page,
        page_size=page_size,
        source_type=source_type,
        gate=gate,
        planner=planner,
        generation=generation,
        send=send,
        confirmation=confirmation,
    )


@router.get("/opportunities")
@router.get("/summary")
def opportunities(
    service: Reader,
    session_id: str = Query("", max_length=255),
    start: Optional[float] = Query(None, ge=0),
    end: Optional[float] = Query(None, ge=0),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(25, ge=1, le=100),
    source_type: str = Query("", max_length=64),
    gate: str = Query("", max_length=64),
    planner: str = Query("", max_length=64),
    generation: str = Query("", max_length=64),
    send: str = Query("", max_length=64),
    confirmation: str = Query("", max_length=64),
):
    return listing(
        service, session_id, start, end, page, page_size, source_type, gate, planner, generation, send, confirmation
    )


@router.get("/opportunities/{opportunity_id}")
def detail(
    opportunity_id: str, service: Reader, page: int = Query(1, ge=1, le=10000), page_size: int = Query(50, ge=1, le=100)
):
    result = invoke(service.detail, opportunity_id, page=page, page_size=page_size)
    if result is None:
        raise HTTPException(404, "机会不存在或不在授权范围")
    return result


@router.get("/opportunities/{opportunity_id}/context")
def context(
    opportunity_id: str,
    service: Reader,
    evaluation_id: str = Query("", max_length=255),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(25, ge=1, le=100),
):
    result = invoke(service.context, opportunity_id, evaluation_id, page=page, page_size=page_size)
    if result is None:
        raise HTTPException(404, "评估与机会不匹配或不在授权范围")
    return result
