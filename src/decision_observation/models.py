"""Versioned contracts. No chat bodies, prompts or model requests belong here."""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple
import hashlib
import json
import uuid

SCHEMA_VERSION = 6
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
    account_id: Optional[str] = None


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
    execution: Optional["ExecutionEvent"] = None
    transfer: Optional["OpportunityTransfer"] = None
    related_reference: Optional["Reference"] = None
    claim_check: Optional["ClaimCheck"] = None
    reply_event: Optional["ReplyEvent"] = None
    delivery_scope: Optional["DeliveryScope"] = None
    receipt: Optional["ReceiptFact"] = None


@dataclass(frozen=True)
class DeliveryScope:
    scope_id: str
    session_id: str
    run_id: str
    policy_version: str
    created_at: float


@dataclass(frozen=True)
class ReceiptFact:
    platform: str
    account_id: Optional[str]
    route_scope: Optional[str]
    outbound_id: str
    driver_id: Optional[str]
    driver_kind: Optional[str]
    driver_status: str
    platform_message_id: Optional[str]
    delivery_id: Optional[str]
    acknowledged: bool
    retcode: Optional[int]
    observed_at: float
    source: str
    attempt_id: Optional[str] = None
    ambiguous: bool = False
    failure_evidence: Optional[str] = None


@dataclass(frozen=True)
class ReceiptProbe:
    """A callback with no live capability; only the writer may resolve its stable key."""

    receipt: ReceiptFact
    authorizations: Tuple[Tuple[str, Tuple[int, int]], ...]


@dataclass(frozen=True)
class ReplyEvent:
    """One occurrence may link several opportunities; count DISTINCT occurrence_id."""

    event_id: str
    occurrence_id: str
    parent_event_id: str
    opportunity_id: str
    evaluation_id: Optional[str]
    execution_id: str
    flow_id: str
    occurred_at: float
    stage: str
    status: str
    reason: str
    logical_turn_id: Optional[str]
    cycle_id: Optional[str]
    tool_call_id: Optional[str]
    reply_id: Optional[str]
    generation_group_id: Optional[str]
    request_id: Optional[str]
    outbound_id: Optional[str]
    attempt_id: Optional[str]
    detail: str


@dataclass(frozen=True)
class Reference:
    """An immutable, self-contained receipt; never resolved using latest/time heuristics."""

    session_id: str
    generation: Tuple[int, int]
    messages: Tuple[MessageEvent, ...]
    opportunity: Opportunity
    evaluation: Optional[Evaluation] = None
    eligible: bool = True
    link_source: str = "gate_dispatch"
    wait_tool_call_id: Optional[str] = None
    parent_execution_id: Optional[str] = None
    execution_id: str = ""


@dataclass(frozen=True)
class OpportunityTransfer:
    transfer_id: str
    source_opportunity_id: str
    target_opportunity_id: str
    message_event_id: str
    execution_id: str
    relation_type: str
    occurred_at: float
    account_id: Optional[str]


@dataclass(frozen=True)
class ClaimCheck:
    check_id: str
    message_event_id: str
    session_id: str
    account_id: Optional[str]
    run_id: str
    checked_at: float
    status: str
    reason: str


@dataclass(frozen=True)
class ExecutionEvent:
    event_id: str
    opportunity_id: str
    evaluation_id: Optional[str]
    execution_id: str
    occurred_at: float
    stage: str
    status: str
    reason: str
    logical_turn_id: Optional[str]
    cycle_id: Optional[str]
    replay_status: str
    message_event_ids: str
    tool_names: str
    link_source: str
    wait_tool_call_id: Optional[str]
    parent_execution_id: Optional[str]


class MessageTrigger(str):
    """Keep legacy string comparisons, FIFO ordering and replay source unchanged."""

    def __new__(cls, references: Tuple[Reference, ...]):
        value = super().__new__(cls, "message")
        value.references = references
        return value


class ConsumedMessages(list):
    """A normal message list with explicit receipts for its actual consumption."""

    def __init__(self, messages: list, references: Tuple[Reference, ...]):
        super().__init__(messages)
        self.references = references


@dataclass
class PendingWorkset:
    """Owned by a Runtime; repeated checks retain one identity until consumption."""

    generation: Optional[Tuple[int, int]] = None
    opportunity: Optional[Opportunity] = None
    evaluation_sequence: int = 0
    messages: Dict[str, MessageEvent] = field(default_factory=dict)
    latest_evaluation: Optional[Evaluation] = None
    priority_fallbacks: Dict[str, Reference] = field(default_factory=dict)

    def clear(self) -> None:
        self.generation = None
        self.opportunity = None
        self.evaluation_sequence = 0
        self.messages.clear()
        self.latest_evaluation = None
        self.priority_fallbacks.clear()
