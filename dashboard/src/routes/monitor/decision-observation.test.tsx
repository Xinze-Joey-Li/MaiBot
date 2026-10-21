import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DecisionObservationDetailPage, DecisionObservationPage } from './decision-observation'

const get = vi.hoisted(() => vi.fn())
vi.mock('@/lib/http', () => ({ backendApi: { get } }))
vi.mock('@tanstack/react-router', () => ({
  useParams: () => ({ opportunityId: 'opportunity-a' }),
  Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
    <a href={to}>{children}</a>
  ),
}))
const opportunity = {
  opportunity_id: 'opportunity-a',
  session_id: 'group-a',
  session_name: '隔离测试群',
  created_at: 1700000000,
  source_type: 'ordinary_pending',
  message_count: 2,
  gate: 'pass',
  planner: 'started',
  generation: 'generation_completed',
  send: 'send_attempted',
  confirmation: 'unknown',
}
describe('Decision Observation read-only UI', () => {
  beforeEach(() => {
    get.mockReset()
    get.mockImplementation((url: string) => {
      if (url.includes('/context?'))
        return Promise.resolve({
          total: 2,
          membership_available: true,
          context_status: 'partial_reconstruction',
          limitations: ['参考上下文不等于完整Prompt'],
          items: [
            {
              message_event_id: 'a',
              membership: 'evaluation_member',
              status: 'available',
              name: '甲',
              text: '明确关联消息',
              event_timestamp: 100,
              received_at: 100,
            },
            {
              message_event_id: 'b',
              membership: 'reference_only',
              status: 'available',
              name: '乙',
              text: '仅作参考',
              event_timestamp: 99,
              received_at: 99,
            },
          ],
        })
      if (url.endsWith('/health'))
        return Promise.resolve({
          database_exists: false,
          schema_version: null,
          process: { enabled: false, queue_size: 0, queue_capacity: 128 },
          stored_counts: {},
          sessions: [{ session_id: 'group-a', name: '隔离测试群' }],
          authorized_group_count: 1,
          authorization_model: '单管理员认证',
        })
      if (url.includes('/opportunity-a?'))
        return Promise.resolve({
          opportunity,
          limitations: ['没有平台确认不得声称发送成功'],
          replay: [{ logical_turn_id: 'expired', status: 'expired_or_missing' }],
          groups: {
            evaluations: {
              total: 1,
              available: true,
              items: [{ evaluation_id: 'eval-a', gate_result: 'pass', evaluated_at: 100 }],
            },
            reply_events: { total: 51, available: true, items: [] },
          },
        })
      return Promise.resolve({
        items: [opportunity],
        total: 26,
        summary: { generation_calls: 1, driver_attempts: 3, platform_unique_messages: null },
        units: '按occurrence去重',
      })
    })
  })
  it('shows health, real rows, unknown units and server pagination', async () => {
    render(<DecisionObservationPage />)
    await screen.findByRole('link', { name: 'opportunity-a' })
    expect(screen.getByText(/尚无观察数据库/)).toBeInTheDocument()
    expect(screen.getByText('已确认平台唯一消息')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await waitFor(() => expect(get).toHaveBeenCalledWith(expect.stringContaining('page=2')))
    fireEvent.change(screen.getByLabelText('gate'), { target: { value: 'reject' } })
    await waitFor(() => expect(get).toHaveBeenCalledWith(expect.stringContaining('gate=reject')))
    expect(screen.queryByRole('button', { name: /保存|执行|开启采集/ })).not.toBeInTheDocument()
  })
  it('distinguishes snapshot members from reference history and expired replay', async () => {
    render(<DecisionObservationDetailPage />)
    await screen.findByText(/expired_or_missing/)
    fireEvent.change(screen.getByLabelText('评估 ID'), { target: { value: 'eval-a' } })
    await screen.findByText('明确关联消息')
    expect(screen.getByText('A · 本次评估成员')).toBeInTheDocument()
    expect(screen.getByText('B · 参考上下文')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '打开现有回放' })).not.toBeInTheDocument()
  })
  it('renders errors without fabricated records', async () => {
    get.mockRejectedValue(new Error('503 unavailable'))
    render(<DecisionObservationPage />)
    await screen.findAllByRole('alert')
    expect(screen.queryByRole('link', { name: 'opportunity-a' })).not.toBeInTheDocument()
    expect(screen.queryByText(/此范围暂无观察记录/)).not.toBeInTheDocument()
  })
})
