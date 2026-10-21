"""Authenticated human judgments; intentionally no Dashboard editing UI."""

from pathlib import Path
from typing import Annotated, Literal, Optional
import sqlite3
import time

from fastapi.responses import JSONResponse
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.decision_observation.annotation_models import AnnotationCommand, AnnotationError
from src.decision_observation.annotations import AnnotationService
from src.webui.dependencies import require_auth
from src.webui.observation_read import ObservationReadRoute

router = APIRouter(
    prefix="/api/webui/decision-observation", tags=["decision-annotations"], dependencies=[Depends(require_auth)],
    route_class=ObservationReadRoute,
)


class AnnotationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_type: Literal["evaluation", "opportunity"]
    evaluation_id: Optional[str] = Field(default=None, min_length=1, max_length=255)
    label: Literal["should_reply", "should_not_reply", "uncertain"]
    reason: Optional[str] = Field(default=None, max_length=2000)
    context_quality: Literal["identity_context_available", "partial_context", "critical_context_missing"]
    expected_revision: int = Field(ge=0, le=2147483647)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")
    context_mode: Literal["decision_context_v1"] = "decision_context_v1"


def annotation_service() -> AnnotationService:
    from src.decision_observation.recorder import store

    return AnnotationService(store, Path(__file__).resolve().parents[3] / "data/MaiBot.db")


Service = Annotated[AnnotationService, Depends(annotation_service)]


def invoke(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except AnnotationError as exc:
        detail = {"code": exc.code}
        if exc.current_revision is not None:
            detail["current_revision"] = exc.current_revision
        raise HTTPException(exc.status, detail) from None
    except (sqlite3.Error, OSError, TimeoutError, ValueError):
        # No body/reason/token/SQL is included in logs or HTTP storage errors.
        raise HTTPException(503, {"code": "annotation_storage_unavailable", "retry": "same_idempotency_key"}) from None


@router.get("/annotation-targets")
def annotation_targets(
    service: Service,
    session_id: str = Query("", max_length=255),
    start: Optional[float] = Query(None, ge=0),
    end: Optional[float] = Query(None, ge=0),
    annotation_state: Literal["", "unannotated", "annotated", "uncertain"] = "",
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(25, ge=1, le=100),
):
    end = time.time() if end is None else end
    start = end - 86400 if start is None else start
    return invoke(
        service.targets,
        session_id=session_id,
        start=start,
        end=end,
        annotation_state=annotation_state,
        page=page,
        page_size=page_size,
    )


@router.get("/annotations/{opportunity_id}")
def current(
    opportunity_id: str,
    service: Service,
    target_type: Literal["evaluation", "opportunity"],
    evaluation_id: Optional[str] = Query(None, min_length=1, max_length=255),
):
    return invoke(service.current, opportunity_id, target_type, evaluation_id)


@router.put("/annotations/{opportunity_id}")
def save(opportunity_id: str, body: AnnotationInput, service: Service):
    command = invoke(AnnotationCommand, opportunity_id=opportunity_id, **body.model_dump())
    result = invoke(service.save, command)
    return JSONResponse(
        result, status_code=201 if result["annotation"]["revision"] == 1 and not result["idempotent_replay"] else 200
    )


@router.get("/annotations/{opportunity_id}/history")
def history(
    opportunity_id: str,
    service: Service,
    target_type: Literal["evaluation", "opportunity"],
    evaluation_id: Optional[str] = Query(None, min_length=1, max_length=255),
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(25, ge=1, le=100),
):
    return invoke(service.history, opportunity_id, target_type, evaluation_id, page, page_size)
