"""Per-engine explicit lineage. No ContextVar, timestamps, message lookup or model calls."""

from dataclasses import replace
from typing import Any, Optional, Sequence, Set, Tuple
import uuid

from . import recorder
from .models import ConsumedMessages, MessageTrigger, Reference


class ExecutionTracker:
    def __init__(self) -> None:
        self.references: Tuple[Reference, ...] = ()
        self.execution_id = ""
        self.logical_turn_id: Optional[str] = None
        self.cycle_id: Optional[str] = None
        self.planned_opportunities: Set[str] = set()
        self.consumed_opportunities: Set[str] = set()
        self.cycle_references: Tuple[Reference, ...] = ()
        self.request_message_ids: Tuple[str, ...] = ()
        self.wait_binding: Optional[Tuple[str, str, Tuple[Reference, ...]]] = None

    @recorder.isolated
    def reset(self, trigger: str) -> None:
        self.references = ()
        self.execution_id = uuid.uuid4().hex
        self.logical_turn_id = None
        self.cycle_id = None
        self.planned_opportunities.clear()
        self.consumed_opportunities.clear()
        self.cycle_references = ()
        self.request_message_ids = ()
        self.add_trigger(trigger)

    @recorder.isolated
    def add_trigger(self, trigger: str) -> None:
        if isinstance(trigger, MessageTrigger):
            self._merge(trigger.references)

    def _merge(self, references: Tuple[Reference, ...]) -> None:
        current = {
            (r.opportunity.opportunity_id, r.evaluation.evaluation_id if r.evaluation else None): r
            for r in self.references
        }
        for ref in references:
            if not ref.eligible:
                continue
            key = (ref.opportunity.opportunity_id, ref.evaluation.evaluation_id if ref.evaluation else None)
            if key not in current and len(current) >= 512:
                recorder.store.dropped += 1
                recorder.store._warning("execution_reference_limit")
                continue
            current[key] = ref
        self.references = tuple(current.values())

    @recorder.isolated
    def consumed(self, messages: Sequence, runtime: Any) -> None:
        if not isinstance(messages, ConsumedMessages):
            return
        for consumed in messages.references:
            matched = False
            for ref in self.references:
                if ref.opportunity.opportunity_id == consumed.opportunity.opportunity_id:
                    self._merge((replace(ref, messages=consumed.messages, opportunity=consumed.opportunity),))
                    matched = True
            if not matched:
                self._merge((consumed,))
            if any(r.opportunity.opportunity_id == consumed.opportunity.opportunity_id for r in self.references):
                self.consumed_opportunities.add(consumed.opportunity.opportunity_id)
        self.record(
            runtime, "batch_linked", "observed", "actual_consumed_batch", message_ids=[m.message_id for m in messages]
        )

    @recorder.isolated
    def resume_wait(self, tool_call_id: str, logical_turn_id: str) -> None:
        binding = self.wait_binding
        self.wait_binding = None
        if binding is not None and binding[:2] == (tool_call_id, logical_turn_id):
            self._merge(
                tuple(replace(r, link_source="wait_continuation", wait_tool_call_id=tool_call_id) for r in binding[2])
            )
            self.consumed_opportunities.update(r.opportunity.opportunity_id for r in binding[2])

    @recorder.isolated
    def retain_wait(self, runtime: Any) -> None:
        if not recorder.store.policy.enabled or not self.references:
            self.wait_binding = None
            return
        tool_call_id = runtime._pending_wait_tool_call_id
        logical_turn_id = runtime._pending_wait_logical_turn_id
        self.wait_binding = (
            (
                tool_call_id,
                logical_turn_id,
                tuple(
                    replace(r, parent_execution_id=self.execution_id)
                    for r in self.references
                    if r.opportunity.opportunity_id in self.planned_opportunities
                ),
            )
            if tool_call_id and logical_turn_id == self.logical_turn_id
            else None
        )

    @recorder.isolated
    def bind_cycle(self, cycle_id: str) -> None:
        self.cycle_id = cycle_id
        self.cycle_references = tuple(
            r for r in self.references if r.opportunity.opportunity_id in self.consumed_opportunities
        )

    @recorder.isolated
    def request_batch(self, messages: Sequence) -> None:
        if recorder.store.policy.enabled:
            self.request_message_ids = tuple(m.message_id for m in messages)

    @recorder.isolated
    def accept_context(self, context: Any) -> None:
        if recorder.store.policy.enabled:
            self.references = context.observation_references

    @recorder.isolated
    def selected(self, runtime: Any, response: Any) -> None:
        if not recorder.store.policy.enabled or not self.references:
            return
        self.record(
            runtime,
            "tool_selection",
            "observed",
            "planner_response",
            tool_names=[call.func_name for call in response.tool_calls],
        )

    @recorder.isolated
    def missing_start(self, runtime: Any, reason: str) -> None:
        missing = tuple(r for r in self.references if r.opportunity.opportunity_id not in self.planned_opportunities)
        for ref in missing:
            recorder.execution(
                (ref,),
                "not_started",
                "not_observed",
                reason
                if ref.opportunity.opportunity_id in self.consumed_opportunities
                else "queued_batch_not_consumed",
                session_id=runtime.session_id,
                execution_id=self.execution_id,
                logical_turn_id=self.logical_turn_id,
            )

    @recorder.isolated
    def record(
        self,
        runtime: Any,
        stage: str,
        status: str,
        reason: str,
        *,
        message_ids: Sequence[str] = (),
        tool_names: Sequence[str] = (),
    ) -> None:
        if not recorder.store.policy.enabled or not self.references:
            return
        session_id = runtime.session_id
        # The engine owns the logical/cycle identity; ContextVar only verifies Replay binding.
        from src.decision_replay import recorder as replay

        trace = replay.current_trace.get()
        replay_status = "disabled" if not replay.enabled() else "not_observed"
        if (
            replay.enabled()
            and trace is not None
            and (
                trace.session_id == session_id
                and trace.logical_turn_id == self.logical_turn_id
                and trace.cycle_id == self.cycle_id
            )
        ):
            replay_status = "trace_bound_persistence_unknown"
        if stage == "planner_started":
            self.planned_opportunities.update(r.opportunity.opportunity_id for r in self.cycle_references)
        references = (
            self.cycle_references
            if stage
            in {
                "planner_started",
                "tool_selection",
                "cycle_ended",
                "cycle_finalized",
            }
            else self.references
        )
        recorder.execution(
            references,
            stage,
            status,
            reason,
            session_id=session_id,
            execution_id=self.execution_id,
            logical_turn_id=self.logical_turn_id,
            cycle_id=self.cycle_id,
            message_ids=message_ids,
            tool_names=tool_names,
            replay_status=replay_status,
        )
