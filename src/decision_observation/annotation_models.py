"""Human-label contract v1; labels are never instructions to production policy."""

from dataclasses import asdict, dataclass
from typing import Optional
import hashlib
import json
import re

CONTRACT_VERSION = "annotation-v1"
ACTOR_ID = "webui:shared-admin"
LABELS = ("should_reply", "should_not_reply", "uncertain")
QUALITIES = ("identity_context_available", "partial_context", "critical_context_missing")


class AnnotationError(Exception):
    def __init__(self, status: int, code: str, current_revision: Optional[int] = None) -> None:
        super().__init__(code)
        self.status, self.code, self.current_revision = status, code, current_revision


@dataclass(frozen=True)
class AnnotationCommand:
    opportunity_id: str
    target_type: str
    evaluation_id: Optional[str]
    label: str
    reason: Optional[str]
    context_quality: str
    expected_revision: int
    idempotency_key: str
    context_mode: str = "decision_context_v1"

    def __post_init__(self) -> None:
        if (
            self.target_type not in ("evaluation", "opportunity")
            or self.label not in LABELS
            or self.context_quality not in QUALITIES
        ):
            raise AnnotationError(422, "invalid_annotation_contract")
        if not self.opportunity_id or len(self.opportunity_id) > 255:
            raise AnnotationError(422, "invalid_opportunity_id")
        if (self.target_type == "evaluation") != bool(self.evaluation_id) or (
            self.evaluation_id is not None and len(self.evaluation_id) > 255
        ):
            raise AnnotationError(422, "invalid_evaluation_target")
        if self.target_type == "opportunity" and self.evaluation_id is not None:
            raise AnnotationError(422, "opportunity_target_requires_null_evaluation")
        if type(self.expected_revision) is not int or not 0 <= self.expected_revision <= 2147483647:
            raise AnnotationError(422, "invalid_expected_revision")
        if self.reason is not None and (
            not isinstance(self.reason, str) or len(self.reason) > 2000 or "\x00" in self.reason
        ):
            raise AnnotationError(422, "invalid_reason")
        if not re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", self.idempotency_key):
            raise AnnotationError(422, "invalid_idempotency_key")
        if self.context_mode != "decision_context_v1":
            raise AnnotationError(422, "unsupported_context_mode")

    @property
    def request_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
