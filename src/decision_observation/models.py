"""Versioned contracts. No chat bodies, prompts or model requests belong here."""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple
import hashlib
import json
import uuid

SCHEMA_VERSION = 1
FEATURE_VERSION = "reply-likelihood-v1"


def identity(kind: str, *parts: str) -> str:
    return kind + ":" + uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(parts)).hex


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    authorized_session_ids: FrozenSet[str] = frozenset()
    retention_days: int = 30
    queue_capacity: int = 2048

    def __post_init__(self) -> None:
        if not 1 <= self.retention_days <= 365 or not 1 <= self.queue_capacity <= 65536:
            raise ValueError("Invalid observation retention or queue capacity")

    @property
    def version(self) -> str:
        raw = (self.enabled, sorted(self.authorized_session_ids), self.retention_days, self.queue_capacity)
        return hashlib.sha256(json.dumps(raw).encode()).hexdigest()


@dataclass(frozen=True)
class MessageEvent:
    message_event_id: str
    session_id: str
    platform_message_id: str
    sender_id: str
    event_timestamp: float
    received_timestamp: Optional[float]
    run_id: str
    source_type: str = "inbound"
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    session_id: str
    created_at: float
    run_id: str
    policy_version: str
    source_type: str = "ordinary_pending"
    finalized_at: Optional[float] = None
    status: str = "pending"


@dataclass(frozen=True)
class Evaluation:
    evaluation_id: str
    opportunity_id: str
    evaluation_sequence: int
    evaluated_at: float
    feature_version: str
    feature_snapshot: str
    effective_threshold: Optional[float]
    calculated_probability: Optional[float]
    gate_result: str
    decision_reason: str
    weights_hash: Optional[str]
    evidence: str


@dataclass(frozen=True)
class Batch:
    session_id: str
    generation: Tuple[int, int]
    messages: Tuple[MessageEvent, ...]
    opportunity: Optional[Opportunity] = None
    evaluation: Optional[Evaluation] = None


@dataclass
class PendingWorkset:
    """Owned by a Runtime; repeated checks retain one identity until consumption."""

    generation: Optional[Tuple[int, int]] = None
    opportunity: Optional[Opportunity] = None
    evaluation_sequence: int = 0
    messages: Dict[str, MessageEvent] = field(default_factory=dict)

    def clear(self) -> None:
        self.generation = None
        self.opportunity = None
        self.evaluation_sequence = 0
        self.messages.clear()
