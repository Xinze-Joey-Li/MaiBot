import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AnnotationMachineDetails } from './annotation-machine-details'
import { machineView, privateText } from './annotation-presentation'
import type { ReviewContext } from './annotation-api'

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: React.ReactNode }) => <a href="/decision-replay">{children}</a>,
}))

describe('annotation presentation boundaries', () => {
  it('escapes regex metacharacters and protects already generated aliases', () => {
    const text = privateText({
      A: '群友 AB12',
      '[某人]': '群友 CD34',
      小明: '群友 EF56',
      小明同学: '群友 GH78',
    })
    expect(text('A与[某人]提及小明同学、小明')).toBe('群友 AB12与群友 CD34提及群友 GH78、群友 EF56')
    expect(text(text('A'))).toBe('群友 AB12')
  })
  it.each(['', 'unknown_code', 'not_observed'])(
    'does not fabricate a reason or result for %s',
    (code) => {
      const view = machineView({ gate_result: code, decision_reason: null, execution_result: code })
      expect(view).toEqual({
        gate: '门控结果未知',
        planner: '无法确认',
        gateReason: '未记录具体门控原因',
        plannerReason: '未记录可确认的 Planner 判断原因',
        execution:
          code === 'not_observed'
            ? '未观察到发送（不代表确定未发送）'
            : '结果未知（未确认实际发送结果）',
      })
    }
  )
  it('translates deferred judgment and explicit platform evidence separately', () => {
    expect(
      machineView({
        gate_result: 'deferred',
        decision_reason: 'group_wait_active',
        execution_result: 'platform_confirmed',
      })
    ).toEqual({
      gate: '门控延后',
      planner: '无法确认',
      gateReason: '群聊仍处于等待状态。',
      plannerReason: '未记录可确认的 Planner 判断原因',
      execution: '已发送（至少一次平台确认）',
    })
  })
  it('sanitizes nested technical facts, names in keys, limitations, and group metadata', async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          opportunity: { session_id: 'g', session_name: '小明的群' },
          groups: {
            evaluations: {
              items: [
                {
                  evaluation_id: 'e1',
                  decision_reason: '小明发问',
                  evidence: { 小明: '给小明回复' },
                },
              ],
              total: 51,
              available: true,
            },
          },
          replay: [{ logical_turn_id: 'turn1', status: 'available' }],
          limitations: ['小明的记录'],
        }),
        { headers: { 'Content-Type': 'application/json' } }
      )
    )
    vi.stubGlobal('fetch', fetch)
    const target = { opportunity_id: 'o1', target_type: 'evaluation' as const, evaluation_id: 'e1' }
    const { container } = render(
      <AnnotationMachineDetails target={target} text={privateText({ 小明: '群友 A17' })} />
    )
    expect(await screen.findByText('当前回复判断')).toBeInTheDocument()
    expect(container).not.toHaveTextContent('小明')
    expect(container).toHaveTextContent('群友 A17发问')
    expect(screen.getByRole('link', { name: /打开只读回放/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '细节下一页' }))
    expect(await screen.findByText('第 2 页 · 各组独立分页')).toBeInTheDocument()
  })
})

describe('independent gate, Planner and delivery semantics', () => {
  const view = (
    planner?: NonNullable<ReviewContext['machine']>['planner'],
    execution_result = 'unknown',
    gate_result = 'pass',
    decision_reason: string | null = null
  ) => machineView({ gate_result, decision_reason, planner, execution_result })
  const observed = (...decisions: string[]) => ({
    status: 'observed',
    evidence_status: 'tool_selection',
    rounds: decisions.map((decision) => ({ decision })),
    not_started_attempts: 0,
  })
  it.each([
    ['pass', '通过门控，允许进入后续规划'],
    ['reject', '门控拒绝'],
    ['defer', '门控延后'],
    ['deferred', '门控延后'],
    ['invalid', '门控结果未知'],
  ])('translates gate %s without using normative annotation labels', (raw, expected) => {
    const result = view(undefined, 'unknown', raw)
    expect(result.gate).toBe(expected)
    expect(result.gate).not.toMatch(/应该回复|不应该回复/)
    expect(result.planner).toBe('无法确认')
  })
  it('gate pass allows Planner to choose waiting or other tools independently', () => {
    expect(view(observed('wait')).planner).toBe('决定等待（已记录等待工具选择）')
    expect(view(observed('other')).planner).toBe('其他实际决策（选择其他工具或未选择工具）')
    expect(view(observed('reply')).planner).toBe('决定回复（已记录回复工具选择）')
  })
  it('limits nonexecution to explicitly recorded scheduling attempts', () => {
    expect(
      view({
        status: 'not_started',
        evidence_status: 'explicit_not_started',
        rounds: [],
        not_started_attempts: 1,
      }).planner
    ).toBe('未执行（已记录的 1 次调度尝试）')
    expect(
      view({
        status: 'not_started',
        evidence_status: 'missing_records',
        rounds: [],
        not_started_attempts: 0,
      }).planner
    ).toBe('无法确认')
  })
  it('does not flatten multiple Planner rounds into a final decision', () => {
    const result = view(observed('wait', 'reply'))
    expect(result.planner).toContain('已观察 2 轮')
    expect(result.planner).toContain('回复 1 轮、等待 1 轮')
    expect(result.planner).toContain('不代表最终决定')
  })
  it.each(['shared_cycle', 'incomplete_lineage', 'missing_records'])(
    'degrades unreliable association %s',
    (evidence_status) => {
      expect(
        view({ status: 'unknown', evidence_status, rounds: [], not_started_attempts: 0 }).planner
      ).toBe('无法确认')
    }
  )
  it.each([
    ['platform_confirmed', '已发送（至少一次平台确认）'],
    ['definitive_failure', '发送失败（有明确失败证据）'],
    ['not_observed', '未观察到发送（不代表确定未发送）'],
    ['unknown', '结果未知（未确认实际发送结果）'],
  ])('keeps execution status %s separate from a reply tool selection', (raw, expected) => {
    const result = view(observed('reply'), raw)
    expect(result.execution).toBe(expected)
    expect(result.planner).toBe('决定回复（已记录回复工具选择）')
  })
  it('labels reasons by phase and never invents Planner reasoning', () => {
    expect(view(observed('reply'), 'unknown', 'pass', 'dynamic_trigger').gateReason).toBe(
      '动态门控允许进入后续回复规划。'
    )
    expect(view().gateReason).toBe('未记录具体门控原因')
    expect(view(undefined, 'unknown', 'pass', 'unknown_nickname_reason').gateReason).toBe(
      '已记录原因码，详见完整决策细节。'
    )
    expect(view(observed('reply')).plannerReason).toBe('未记录可确认的 Planner 判断原因')
  })
})
