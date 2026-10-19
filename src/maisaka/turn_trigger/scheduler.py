"""Maisaka 消息触发调度。"""

from typing import TYPE_CHECKING

from src.services.owner_admin.trigger_policy import pending_trigger_messages

from src.common.logger import get_logger
from src.decision_replay.recorder import probe
from src.maisaka.focus import focus_mode_manager
from src.maisaka.mode_policy import is_dynamic_reply_trigger_enabled

from .gates import DynamicReplyTurnGate, FrequencyThresholdTurnGate

if TYPE_CHECKING:
    from src.maisaka.runtime import MaisakaHeartFlowChatting

logger = get_logger("maisaka_turn_scheduler")


class MessageTurnScheduler:
    """决定外部消息何时进入 Maisaka 内部循环。"""

    def __init__(self, runtime: "MaisakaHeartFlowChatting") -> None:
        self._runtime = runtime
        self._replay_signature = None
        self._dynamic_reply_gate = DynamicReplyTurnGate(runtime)
        self._frequency_threshold_gate = FrequencyThresholdTurnGate(runtime)

    def record_reply(self) -> None:
        """记录 Planner 实际调用了一次 reply，供动态门控统计回复次数。"""

        self._dynamic_reply_gate.record_reply()

    @probe
    def _replay(self, decision: str, reason: str, **detail) -> None:
        from src.decision_replay.recorder import new_trace, record, enabled
        if not enabled():
            return
        runtime = self._runtime
        ids = [m.message_id for m in pending_trigger_messages(runtime)]
        signature = (tuple(ids), decision, reason)
        if not ids or signature == self._replay_signature:
            return
        self._replay_signature = signature
        trace = new_trace(runtime.session_id)
        detail.update({'pending_count': len(ids), 'session_name': runtime.session_name})
        record('message', 'message.received', 'buffered', 'pending_batch', trace=trace, message_ids=ids, detail={'pending_count':len(ids)})
        record('scheduler', 'turn.started', 'observed', 'external_trigger', trace=trace,
               message_ids=ids, detail=detail)
        record('gate', 'gate.decided', decision, reason, trace=trace, message_ids=ids, detail=detail)
        if decision == 'pass':
            runtime._decision_replay_turn_id = trace.logical_turn_id
        else:
            record('scheduler', 'turn.ended', 'waiting', reason, trace=trace)

    def schedule_message_turn(self) -> None:
        runtime = self._runtime
        from src.services.owner_admin.system_runtime import session_paused
        from src.maisaka.planner_lifecycle import lifecycle_mode

        if session_paused(runtime.session_id):
            self._replay("reject", "owner_soft_paused")
            return

        if lifecycle_mode() == "optimized" and runtime._agent_state == runtime._STATE_RUNNING:
            self._replay('deferred', 'active_turn_buffers_messages')
            return
        if not focus_mode_manager.can_decide(
            runtime.session_id,
            is_group_chat=runtime.chat_stream.is_group_session,
        ):
            self._replay("reject", "focus_unavailable")
            logger.debug(f"{runtime.log_prefix} 当前不在 focus 状态，跳过 Maisaka 决策调度")
            return

        if runtime._agent_state == runtime._STATE_WAIT:
            if not runtime._is_reply_frequency_silent():
                if runtime.chat_stream.is_group_session:
                    self._replay("deferred", "group_wait_active")
                    return
                logger.info(f"{runtime.log_prefix} 私聊 wait 期间收到新消息，结束等待并进入 Planner")
                runtime._enter_running_state()
            else:
                runtime._enter_stop_state()

        if runtime._message_turn_scheduled:
            return

        pending_count = runtime._get_pending_message_count()
        if pending_count <= 0:
            return

        effective_frequency = runtime._get_effective_reply_frequency()
        formatted_frequency = f"{effective_frequency:.3f}"
        if runtime._is_reply_frequency_silent():
            from src.services.owner_admin.overrides import override_manager

            snapshot = override_manager.snapshot(runtime.session_id)
            silent_reason = ("owner_autonomous_suppressed" if not snapshot.autonomous_allowed and effective_frequency > 0
                             else "silent_frequency")
            logger.info(
                f"{runtime.log_prefix} 回复频率调度: 频率={formatted_frequency} "
                f"pending={pending_count} 判定=静默消费"
            )
            self._replay("reject", silent_reason, frequency=effective_frequency, owner_override_ids=snapshot.override_ids)
            runtime._enqueue_message_turn()
            return

        if runtime._has_forced_turn_trigger():
            self._replay("pass", "forced_trigger", frequency=effective_frequency)
            # @ 强制触发的回复同样占用动态门控的回复额度
            self._dynamic_reply_gate.record_forced_turn(pending_trigger_messages(runtime))
            logger.info(
                f"{runtime.log_prefix} 回复频率调度: 频率={formatted_frequency} "
                f"pending={pending_count} 判定=强制触发"
            )
            runtime._enqueue_message_turn()
            return

        if runtime._idle_backoff.should_delay(pending_count):
            self._replay("deferred", "idle_backoff", frequency=effective_frequency)
            return

        if is_dynamic_reply_trigger_enabled():
            dynamic_result = self._dynamic_reply_gate.evaluate(
                pending_messages=pending_trigger_messages(runtime),
                frequency=effective_frequency,
                allow_observation=runtime._agent_state == "stop",
            )
            logger.info(
                f"{runtime.log_prefix} 回复频率调度: [频率: {formatted_frequency}][{pending_count} 消息] "
                f"{dynamic_result.detail}"
            )
            self._replay('pass' if dynamic_result.should_trigger else 'reject',
                         'dynamic_' + dynamic_result.decision, frequency=effective_frequency,
                         delay_seconds=dynamic_result.delay_seconds,
                         **(dynamic_result.replay_detail or {}))
            if dynamic_result.should_trigger:
                runtime._enqueue_message_turn()
            elif dynamic_result.delay_seconds is not None:
                runtime._defer_message_turn_check(dynamic_result.delay_seconds)
            return

        # frequency 仅在旧模式被显式选择时计算；dynamic 完全由权重评分和动态额度判定。
        trigger_threshold = runtime._get_message_trigger_threshold()
        schedule_detail = f"[频率: {formatted_frequency}][{pending_count}/{trigger_threshold} 消息]"
        logger.info(f"{runtime.log_prefix} 回复频率调度: {schedule_detail}")
        frequency_result = self._frequency_threshold_gate.evaluate(
            pending_count=pending_count,
            trigger_threshold=trigger_threshold,
        )
        logger.info(f"{runtime.log_prefix} 回复频率调度: {frequency_result.detail}")
        self._replay('pass' if frequency_result.should_trigger else 'reject',
                     'frequency_' + frequency_result.decision, frequency=effective_frequency,
                     threshold=trigger_threshold, delay_seconds=frequency_result.delay_seconds)
        if frequency_result.should_trigger:
            runtime._enqueue_message_turn()
            return

        if frequency_result.decision == "delay" and frequency_result.delay_seconds is not None:
            runtime._defer_message_turn_check(frequency_result.delay_seconds)
