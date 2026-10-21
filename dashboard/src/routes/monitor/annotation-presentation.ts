import type { ReviewContext, TargetMetadata } from './annotation-api'

export function privateText(replacements: Record<string, string> = {}) {
  // Already anonymized text (including edited historical reasons) is stable.
  const aliases = new Set(Object.values(replacements))
  const names = [...new Set([...Object.keys(replacements), ...aliases])].sort(
    (a, b) => b.length - a.length
  )
  const pattern = names.length
    ? new RegExp(names.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g')
    : null
  return (value: string) =>
    pattern
      ? value.replace(pattern, (name) => (aliases.has(name) ? name : replacements[name]))
      : value
}

export function reviewTargets(target: TargetMetadata | undefined, context: ReviewContext | null) {
  // Snapshot membership defines a judgment's candidate range, never "last message".
  // Opportunity associations are explicitly a different (non-snapshot) range.
  const ids = new Set(target?.message_event_ids ?? [])
  const valid = !!target && (target.target_type === 'opportunity' || target.membership_available)
  const items = (context?.target_items ?? []).filter((item) => ids.has(item.message_event_id))
  const complete =
    valid &&
    ids.size > 0 &&
    items.length === ids.size &&
    items.every(
      (item) =>
        item.status === 'available' &&
        item.membership ===
          (target?.target_type === 'evaluation' ? 'evaluation_member' : 'opportunity_associated')
    ) &&
    new Set(items.map((item) => item.message_event_id)).size === ids.size
  return { ids: complete ? ids : new Set<string>(), items: complete ? items : [], count: ids.size }
}

const reasons: Record<string, string> = {
  dynamic_trigger: '动态门控允许进入后续回复规划。',
  dynamic_wait: '动态门控决定继续等待。',
  dynamic_delay: '动态门控决定延后再判断。',
  focus_unavailable: '当前会话未处于可决策状态。',
  silent_frequency: '当前回复频率设置要求保持静默。',
  owner_autonomous_suppressed: '当前会话设置禁止自主发言。',
  group_wait_active: '群聊仍处于等待状态。',
  active_turn_buffers_messages: '当前回复回合运行中，新消息暂存等待处理。',
  idle_backoff: '空闲退避策略要求稍后再判断。',
  priority_fallback_forced_trigger: '独立提醒转入普通消息路径后触发规划。',
  frequency_trigger: '待处理消息数量达到当前回复频率要求。',
  frequency_wait: '待处理消息数量尚未达到回复频率要求。',
  frequency_delay: '回复频率策略要求稍后再判断。',
}
export function machineView(machine: ReviewContext['machine']) {
  const planner = machine?.planner
  let plannerDecision = '无法确认'
  if (
    planner?.status === 'not_started' &&
    planner.evidence_status === 'explicit_not_started' &&
    planner.not_started_attempts > 0
  ) {
    plannerDecision = `未执行（已记录的 ${planner.not_started_attempts} 次调度尝试）`
  } else if (
    planner?.status === 'observed' &&
    planner.evidence_status === 'tool_selection' &&
    planner.rounds.length
  ) {
    const decisions: Record<string, string> = {
      reply: '决定回复（已记录回复工具选择）',
      wait: '决定等待（已记录等待工具选择）',
      other: '其他实际决策（选择其他工具或未选择工具）',
      mixed: '同时选择回复与等待工具',
    }
    if (planner.rounds.length === 1) {
      plannerDecision = decisions[planner.rounds[0].decision] ?? '无法确认'
    } else if (planner.rounds.every((round) => round.decision in decisions)) {
      plannerDecision = `已观察 ${planner.rounds.length} 轮：${[
        ['reply', '回复'],
        ['wait', '等待'],
        ['other', '其他'],
        ['mixed', '回复与等待'],
      ]
        .map(
          ([code, name]) =>
            `${name} ${planner.rounds.filter((round) => round.decision === code).length} 轮`
        )
        .join('、')}（不代表最终决定）`
    }
  }
  return {
    gate:
      (
        {
          pass: '通过门控，允许进入后续规划',
          reject: '门控拒绝',
          defer: '门控延后',
          deferred: '门控延后',
        } as Record<string, string>
      )[machine?.gate_result ?? ''] ?? '门控结果未知',
    planner: plannerDecision,
    gateReason:
      reasons[machine?.decision_reason ?? ''] ??
      (machine?.decision_reason ? '已记录原因码，详见完整决策细节。' : '未记录具体门控原因'),
    plannerReason: '未记录可确认的 Planner 判断原因',
    execution:
      (
        {
          platform_confirmed: '已发送（至少一次平台确认）',
          definitive_failure: '发送失败（有明确失败证据）',
          not_observed: '未观察到发送（不代表确定未发送）',
        } as Record<string, string>
      )[machine?.execution_result ?? ''] ?? '结果未知（未确认实际发送结果）',
  }
}
