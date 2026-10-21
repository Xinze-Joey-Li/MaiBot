import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DecisionReplayPage } from './decision-replay'

const get = vi.hoisted(() => vi.fn())
vi.mock('@/lib/http', () => ({ backendApi: { get } }))
vi.mock('@tanstack/react-router', async (original) => ({ ...await original<typeof import('@tanstack/react-router')>(), useSearch: () => ({}) }))
const turn = {
  logical_turn_id: 'test-turn',
  session_id: 'test-group',
  session_name: '测试群',
  source: 'message',
  started_at: 100,
  duration_ms: 10,
  message_ids: ['m1'],
  status: 'complete',
  spoken: 0,
  failed: 0,
  outcome: 'dynamic_wait',
  gate_result: 'reject',
  planner_result: null,
  emoji_result: null,
  usage: { model_calls: 0, input_tokens: null, output_tokens: null, cost: null },
  gate_detail: { probability: 0.2, threshold: 0.4 },
}
const list = {
  items: [turn],
  total: 1,
  enabled: true,
  retention_days: 7,
  health: { dropped_events: 0, write_errors: 0, queue_size: 0 },
  summary: {
    triggers: 1,
    gate_passed: 0,
    gate_pass_rate: 0,
    model_calls: 0,
    input_tokens: null,
    output_tokens: null,
    cost: null,
  },
}
describe('Decision Replay', () => {
  beforeEach(() => {
    get.mockReset().mockImplementation((url: string) => {
      if (url.includes('/statistics?'))
        return Promise.resolve({
          counts: {},
          labels: {},
          current_states_of_window_replies: {},
          records: [],
          all_replies: [],
          effective_since: null,
          enabled: true,
          health: { dropped_events: 0, write_errors: 0 },
        })
      if (url.includes('/sessions'))
        return Promise.resolve([{ session_id: 'test-group', name: '测试群' }])
      if (url.includes('/test-turn?'))
        return Promise.resolve({
          ...turn,
          events: [
            {
              event_id: 'e1',
              timestamp: 100,
              sequence: 1,
              stage: 'gate',
              event_type: 'gate.decided',
              decision: 'reject',
              reason_code: 'dynamic_wait',
              cycle_id: null,
              request_id: null,
              message_ids: ['m1'],
              detail: { contributions: { intercept: -1.2 } },
              duration_ms: null,
            },
          ],
          requests: [],
          next_cursor: 1,
          has_more: false,
        })
      return Promise.resolve(list)
    })
  })
  it('loads real API rows and details without offering execution', async () => {
    render(<DecisionReplayPage />)
    await screen.findByText(/测试群 · message/)
    fireEvent.click(screen.getByRole('button', { name: /测试群 · message/ }))
    await screen.findByText(/权重贡献单位为 Logit/)
    expect(get).toHaveBeenCalledWith(expect.stringContaining('/test-turn?session_id=test-group'))
    expect(screen.queryByRole('button', { name: '重放执行' })).not.toBeInTheDocument()
  })
  it('applies filters to the server query', async () => {
    render(<DecisionReplayPage />)
    await screen.findByText(/测试群 · message/)
    fireEvent.change(screen.getByLabelText('门控'), { target: { value: 'reject' } })
    await waitFor(() => expect(get).toHaveBeenCalledWith(expect.stringContaining('gate=reject')))
  })
  it('shows API errors instead of fabricated rows', async () => {
    get.mockRejectedValue(new Error('failed to load'))
    render(<DecisionReplayPage />)
    await screen.findAllByRole('alert')
    expect(screen.queryByText(/测试群 · message/)).not.toBeInTheDocument()
  })
})
