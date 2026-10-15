import { useCallback, useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { backendApi } from '@/lib/http'

type ReplyRecord = {
  reply_id: string
  decision_id: string
  session_id: string
  source: string
  session_type: string
  state: string
  generated: boolean
  closed: boolean
  sent_text: number
  sent_emoji: number
  messages: {
    outbound_message_id: string
    status: string
    content_type: string
    planned_content_type: string
    has_text: boolean
    has_emoji: boolean
    reason: string
    optional: boolean
    attempts: string[]
  }[]
}
type Statistics = {
  counts: Record<string, number>
  labels: Record<string, string>
  current_states_of_window_replies: Record<string, number>
  all_replies: ReplyRecord[]
  records: {
    event_id: string
    decision_id: string
    reply_id: string
    event_type: string
    metrics: string[]
    reason: string
    detail: Record<string, unknown>
  }[]
  effective_since: number | null
  enabled: boolean
  scope: string
  health: { dropped_events: number; write_errors: number }
}
const replyStates: Record<string, string> = {
  generation_only: '已生成但未创建发送动作',
  all_success: '全部成功',
  partial_success: '部分成功',
  all_failed: '全部失败',
  all_skipped: '全部跳过',
  awaiting_confirmation: '仍待确认',
}
export function ReplyStatistics({
  sessionId,
  start,
  end,
}: {
  sessionId: string
  start: number
  end?: number
}) {
  const serial = useRef(0)
  const [data, setData] = useState<Statistics | null>(null)
  const [error, setError] = useState('')
  const [source, setSource] = useState('')
  const [sessionType, setSessionType] = useState('')
  const [contentType, setContentType] = useState('')
  const [system, setSystem] = useState('')
  const load = useCallback(async () => {
    const request = ++serial.current
    setData(null)
    const query = new URLSearchParams({ start: String(start), session_id: sessionId })
    if (end !== undefined) query.set('end', String(end))
    if (source) query.set('source', source)
    if (sessionType) query.set('session_type', sessionType)
    if (contentType) query.set('content_type', contentType)
    if (system) query.set('system', system)
    try {
      const result = await backendApi.get<Statistics>(
        `/api/webui/decision-replay/statistics?${query}`
      )
      if (request === serial.current) {
        setData(result)
        setError('')
      }
    } catch (e) {
      if (request === serial.current) {
        setData(null)
        setError(String(e))
      }
    }
  }, [sessionId, start, end, source, sessionType, contentType, system])
  useEffect(() => {
    let cancelled = false
    queueMicrotask(() => {
      if (!cancelled) void load()
    })
    return () => {
      cancelled = true
      serial.current += 1
    }
  }, [load])
  const download = () => {
    if (!data) return
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    )
    const link = document.createElement('a')
    link.href = url
    link.download = 'reply-statistics-v2.json'
    link.click()
    URL.revokeObjectURL(url)
  }
  return (
    <section aria-label="回复统计" className="space-y-3 rounded border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">回复统计 · 新口径</h2>
        <div className="flex gap-2">
          <Button onClick={() => void load()}>刷新回复统计</Button>
          <Button disabled={!data} onClick={download}>
            导出当前统计与关联记录
          </Button>
        </div>
      </div>
      <p className="text-muted-foreground text-xs">
        共享上方会话与时间范围。以下来源、会话类型、内容类型和系统筛选只作用于统计及其关联记录；门控等筛选只作用于下方回合列表。事件时间按北京时间，使用左闭右开区间
        [开始,结束)。成功回复数是至少一条用户可见消息获平台确认的逻辑回复数，平台确认不表示已读。
      </p>
      <div className="flex flex-wrap gap-3 text-xs">
        <label>
          统计来源
          <select
            aria-label="统计来源"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            className="bg-background ml-1 rounded border p-2"
          >
            {[
              ['', '全部'],
              ['priority_at', '被 @ 优先'],
              ['planner', 'Planner 回复'],
              ['silence_emoji', '暖群'],
              ['auto_repeat', '自动复读'],
              ['system', '系统'],
              ['independent_send', '独立发送'],
              ['direct_generation', '独立生成'],
            ].map(([v, t]) => (
              <option key={v} value={v}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          会话类型
          <select
            aria-label="统计会话类型"
            value={sessionType}
            onChange={(e) => setSessionType(e.target.value)}
            className="bg-background ml-1 rounded border p-2"
          >
            {[
              ['', '全部'],
              ['group', '群聊'],
              ['private', '私聊'],
              ['unknown', '未知'],
            ].map(([v, t]) => (
              <option key={v} value={v}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          内容类型
          <select
            aria-label="统计内容类型"
            value={contentType}
            onChange={(e) => setContentType(e.target.value)}
            className="bg-background ml-1 rounded border p-2"
          >
            {[
              ['', '全部'],
              ['text', '纯文本'],
              ['emoji', '独立表情'],
              ['mixed', '混合消息'],
              ['other', '其他'],
            ].map(([v, t]) => (
              <option key={v} value={v}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          系统消息
          <select
            aria-label="统计系统消息"
            value={system}
            onChange={(e) => setSystem(e.target.value)}
            className="bg-background ml-1 rounded border p-2"
          >
            {[
              ['', '包含系统'],
              ['false', '仅聊天'],
              ['true', '仅系统'],
            ].map(([v, t]) => (
              <option key={v} value={v}>
                {t}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error && <p role="alert">{error}</p>}
      {data && (
        <>
          <p className="text-muted-foreground text-xs">
            {data.enabled ? '记录已启用' : '记录已关闭，数字不会继续更新'}；新口径生效时间：
            {data.effective_since
              ? new Date(data.effective_since * 1000).toLocaleString('zh-CN', {
                  timeZone: 'Asia/Shanghai',
                })
              : '尚无记录'}
            。仅统计保留期内新口径事件，旧事件不能精确补分。纯文本/表情/混合/其他的平台总量互斥；混合消息可同时贡献文本和表情维度，平台总量只计一次。
          </p>
          {(data.health.dropped_events > 0 || data.health.write_errors > 0) && (
            <p role="alert">
              记录存在缺口：丢弃 {data.health.dropped_events}，写入错误 {data.health.write_errors}
              ；这些统计不能视为完整审计。
            </p>
          )}
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            {Object.entries(data.counts).map(([key, value]) => (
              <div key={key} className="bg-muted rounded p-2 text-sm">
                <span>{data.labels[key] ?? key}</span>
                <strong className="ml-2">{value}</strong>
              </div>
            ))}
          </div>
          <p className="text-xs">
            窗口增量按事件时间计数；下列是窗口内新建逻辑回复的当前状态，延迟确认可更新状态，不再次增加逻辑回复次数：
            {Object.entries(data.current_states_of_window_replies)
              .map(([key, value]) => `${replyStates[key] ?? key} ${value}`)
              .join(' · ')}
          </p>
          <details>
            <summary>关联回复、发送计划与实际结果</summary>
            {data.all_replies
              .filter((reply) =>
                data.records.some(
                  (record) =>
                    record.reply_id === reply.reply_id && record.detail.reply_id === reply.reply_id
                )
              )
              .map((reply) => (
                <details
                  key={`${reply.session_id}:${reply.reply_id}`}
                  className="my-2 rounded border p-2 text-xs"
                >
                  <summary>
                    {reply.source} · {reply.session_type} ·{' '}
                    {replyStates[reply.state] ?? reply.state} · 文本成功 {reply.sent_text} /
                    表情成功 {reply.sent_emoji}
                  </summary>
                  <p>
                    决定 {reply.decision_id} · 回复 {reply.reply_id} · 已生成{' '}
                    {reply.generated ? '是' : '否'} · 已执行发送{' '}
                    {reply.messages.some((m) => m.attempts.length) ? '是' : '否'} · 计划已确定{' '}
                    {reply.closed ? '是' : '否'}
                  </p>
                  <p>
                    计划文本{' '}
                    {
                      reply.messages.filter((m) =>
                        ['text', 'mixed'].includes(m.planned_content_type)
                      ).length
                    }{' '}
                    · 计划表情 {reply.messages.filter((m) => m.has_emoji).length}
                  </p>
                  {reply.messages.map((message) => (
                    <p key={message.outbound_message_id}>
                      {message.outbound_message_id} · {message.content_type} · {message.status} ·{' '}
                      {message.reason || '无跳过/失败原因'} · 尝试 {message.attempts.length}
                    </p>
                  ))}
                </details>
              ))}
          </details>
          <details>
            <summary>计数贡献事件（与导出相同）</summary>
            {data.records.map((record) => (
              <details key={record.event_id} className="my-1 rounded border p-2 text-xs">
                <summary>
                  {record.event_type} ·{' '}
                  {record.metrics.map((key) => data.labels[key] ?? key).join('、') || '仅更新状态'}{' '}
                  · {record.reason}
                </summary>
                <pre className="overflow-auto whitespace-pre-wrap">
                  {JSON.stringify(record, null, 2)}
                </pre>
              </details>
            ))}
          </details>
        </>
      )}
    </section>
  )
}
