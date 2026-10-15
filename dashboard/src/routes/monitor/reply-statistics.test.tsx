import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ReplyStatistics } from './reply-statistics'
const get = vi.hoisted(() => vi.fn())
vi.mock('@/lib/http', () => ({ backendApi: { get } }))
const data = {
  counts: {
    logical_replies: 1,
    text_messages: 3,
    emoji_messages: 0,
    send_skips: 1,
    partial_replies: 0,
  },
  labels: {
    logical_replies: '逻辑回复次数',
    text_messages: '文本气泡成功发送数',
    emoji_messages: '表情成功发送数',
    send_skips: '发送跳过次数',
    partial_replies: '部分成功逻辑回复数',
  },
  current_states_of_window_replies: { all_success: 1 },
  all_replies: [
    {
      reply_id: 'r',
      decision_id: 'd',
      session_id: 'g',
      source: 'priority_at',
      session_type: 'group',
      state: 'all_success',
      generated: true,
      closed: true,
      sent_text: 3,
      sent_emoji: 0,
      messages: [
        {
          outbound_message_id: 'emoji',
          status: 'skipped',
          content_type: 'emoji',
          optional: true,
          reason: 'cooldown_blocked',
          attempts: [],
        },
      ],
    },
  ],
  records: [
    {
      event_id: 'e',
      decision_id: 'd',
      reply_id: 'r',
      event_type: 'reply_stats.send_skipped',
      metrics: ['send_skips'],
      reason: 'cooldown_blocked',
      detail: { reply_id: 'r' },
    },
  ],
  effective_since: 100,
  enabled: true,
  health: { dropped_events: 0, write_errors: 0 },
}
beforeEach(() => {
  get.mockReset().mockResolvedValue(data)
})
describe('unified reply statistics', () => {
  it('shows physical and skipped results separately and applies filters', async () => {
    render(<ReplyStatistics sessionId="g" start={100} end={200} />)
    await screen.findByText('逻辑回复次数')
    expect(screen.getByText('文本气泡成功发送数')).toBeInTheDocument()
    expect(screen.getByText(/cooldown_blocked · 尝试 0/)).toBeInTheDocument()
    expect(screen.getByText(/全部成功 1/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('统计会话类型'), { target: { value: 'private' } })
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith(expect.stringContaining('session_type=private'))
    )
    expect(get).toHaveBeenCalledWith(expect.stringContaining('end=200'))
  })
  it('exports the exact displayed snapshot', async () => {
    const create = vi.fn(() => 'blob:stats')
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: create })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<ReplyStatistics sessionId="g" start={100} end={200} />)
    await screen.findByText('逻辑回复次数')
    fireEvent.click(screen.getByRole('button', { name: '导出当前统计与关联记录' }))
    expect(create).toHaveBeenCalledOnce()
    expect(click).toHaveBeenCalledOnce()
    expect(get).toHaveBeenCalledTimes(1)
    click.mockRestore()
  })
})
