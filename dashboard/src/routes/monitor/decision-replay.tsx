import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearch } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { backendApi } from '@/lib/http'
import { ReplyStatistics } from './reply-statistics'

type ReplayEvent = {
  event_id: string
  timestamp: number
  sequence: number
  stage: string
  event_type: string
  decision: string
  reason_code: string
  cycle_id: string | null
  request_id: string | null
  detail: Record<string, unknown>
  duration_ms: number | null
  message_ids: string[]
}
type Usage = {
  cache_hit_tokens?: number | null
  cache_miss_tokens?: number | null
  model_calls: number
  input_tokens: number | null
  output_tokens: number | null
  cost: number | null
}
type Turn = {
  logical_turn_id: string
  session_id: string
  session_name: string | null
  source: string
  started_at: number
  duration_ms: number | null
  message_ids: string[]
  status: string
  spoken: number
  failed: number
  outcome: string | null
  gate_result: string | null
  planner_result: string | null
  emoji_result: string | null
  usage: Usage
  gate_detail: { probability?: number; threshold?: number } | null
}
type Detail = Turn & {
  events: ReplayEvent[]
  requests: Record<string, unknown>[]
  next_cursor: number
  has_more: boolean
  requests_total: number
  requests_truncated: boolean
}
type Listing = {
  items: Turn[]
  total: number
  summary: Record<string, number | null>
  enabled: boolean
  retention_days: number
  health: { dropped_events: number; write_errors: number; queue_size: number }
}
const number = (value: number | null | undefined, digits = 0) =>
  value == null ? '未知' : value.toLocaleString(undefined, { maximumFractionDigits: digits })
const time = (stamp: number) =>
  new Date(stamp * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
const stages: Record<string, string> = {
  message: '消息接收',
  scheduler: '调度',
  gate: '权重门控',
  planner: 'Planner',
  tool: '工具',
  replyer: 'Replyer',
  emoji: '表情选择',
  send: '发送',
  memory: '标签记忆',
  model: '模型用量',
}
const labels: Record<string, string> = {
  sent: '发送成功',
  send_failed: '发送失败',
  no_tag: '无表情 Tag',
  probability_miss: '概率未命中',
  no_candidates: '没有候选',
  score_below_threshold: '评分低于门槛',
  cooldown_blocked: '群冷却拦截',
  no_reply: '无回复动作',
  wait: '等待',
  pass: '通过',
  reject: '未放行',
  recall_timeout: '召回超时',
  local_actions_completed: '本地完成结束',
}

function Timeline({ turn }: { turn: Turn }) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [requestPage, setRequestPage] = useState(1)
  const load = useCallback(
    async (after = 0) => {
      setBusy(true)
      try {
        const result = await backendApi.get<Detail>(
          `/api/webui/decision-replay/${turn.logical_turn_id}?session_id=${encodeURIComponent(turn.session_id)}&after=${after}`
        )
        setDetail((previous) =>
          after && previous ? { ...result, events: [...previous.events, ...result.events] } : result
        )
        setError('')
      } catch (e) {
        setError(String(e))
      } finally {
        setBusy(false)
      }
    },
    [turn.logical_turn_id, turn.session_id]
  )
  const loadRequests = async (next: number) => {
    setBusy(true)
    try {
      const response = await backendApi.get<{ items: Record<string, unknown>[] }>(
        `/api/webui/decision-replay/${turn.logical_turn_id}/requests?session_id=${encodeURIComponent(turn.session_id)}&page=${next}`
      )
      setRequestPage(next)
      setDetail((old) => (old ? { ...old, requests: response.items } : old))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  useEffect(() => {
    void load()
  }, [load])
  return (
    <div className="space-y-3 border-t pt-3">
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {busy && <p>加载事件…</p>}
      {detail && (
        <>
          <p className="text-muted-foreground text-xs">
            回合 {turn.logical_turn_id} · 状态 {detail.status} · 正文仅关联消息 ID，未复制
            Prompt。未出现的阶段未执行或未接入，不能据此推断模型意图。
          </p>
          {[...detail.events]
            .sort((a, b) => a.timestamp - b.timestamp || a.sequence - b.sequence)
            .map((event) => (
              <details key={event.event_id} className="rounded border p-3">
                <summary className="cursor-pointer text-sm">
                  {new Date(event.timestamp * 1000).toLocaleTimeString()} ·{' '}
                  {stages[event.stage] ?? event.stage} ·{' '}
                  {labels[event.reason_code] ?? event.reason_code} · {event.decision}
                  {event.cycle_id != null ? ` · 循环 ${event.cycle_id}` : ''}
                </summary>
                <p className="mt-2 text-xs">
                  事件 {event.event_type} · 请求 {event.request_id ?? '不适用'} · 耗时{' '}
                  {number(event.duration_ms)} ms
                </p>
                <p className="text-xs">消息 ID：{event.message_ids.join(', ') || '无'}</p>
                {event.stage === 'gate' && (
                  <p className="text-muted-foreground text-xs">
                    权重贡献单位为 Logit，不是概率百分点。标签兴趣权重尚未接入。
                  </p>
                )}
                <pre className="bg-muted mt-2 max-h-96 overflow-auto rounded p-3 text-xs break-all whitespace-pre-wrap">
                  {JSON.stringify(event.detail, null, 2)}
                </pre>
              </details>
            ))}
          {detail.has_more && (
            <Button disabled={busy} onClick={() => void load(detail.next_cursor)}>
              加载更多事件
            </Button>
          )}
          <details className="rounded border p-3">
            <summary>逐请求 Token 与费用（去重）</summary>
            <p className="text-xs">
              回合合计：输入 {number(detail.usage.input_tokens)} · 输出{' '}
              {number(detail.usage.output_tokens)} · 已报告缓存命中{' '}
              {number(detail.usage.cache_hit_tokens)} / 未命中{' '}
              {number(detail.usage.cache_miss_tokens)} · 费用估算 {number(detail.usage.cost, 6)}
            </p>
            <p className="text-muted-foreground text-xs">
              Token 来自供应商响应；费用为本地价格估算。缺失数据为
              null（未知），失败重试也计一次真实请求。不与原统计库重复相加。
            </p>
            <pre className="max-h-96 overflow-auto text-xs whitespace-pre-wrap">
              {JSON.stringify(detail.requests, null, 2)}
            </pre>
            {detail.requests_total > 100 && (
              <div className="flex gap-2">
                <Button
                  disabled={busy || requestPage <= 1}
                  onClick={() => void loadRequests(requestPage - 1)}
                >
                  上一页请求
                </Button>
                <Button
                  disabled={busy || requestPage * 100 >= detail.requests_total}
                  onClick={() => void loadRequests(requestPage + 1)}
                >
                  下一页请求
                </Button>
              </div>
            )}
          </details>
        </>
      )}
    </div>
  )
}

export function DecisionReplayPage() {
  const search = useSearch({ strict: false })
  const [observationTurn, setObservationTurn] = useState<Turn | null>(null)
  const [listing, setListing] = useState<Listing | null>(null)
  const [sessions, setSessions] = useState<{ session_id: string; name: string }[]>([])
  const [page, setPage] = useState(1)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    setObservationTurn(null)
    if (search.observationTurn && search.observationSession) {
      backendApi.get<Detail>(`/api/webui/decision-replay/${encodeURIComponent(search.observationTurn)}?session_id=${encodeURIComponent(search.observationSession)}`).then((value) => { if (active) setObservationTurn(value) }).catch((e) => { if (active) setError(String(e)) })
    }
    return () => { active = false }
  }, [search.observationTurn, search.observationSession])
  const serial = useRef(0)
  const [filters, setFilters] = useState({
    session_id: '',
    hours: '24',
    start: '',
    end: '',
    gate: '',
    planner: '',
    source: '',
    spoken: '',
    failed: '',
    emoji: '',
    ascending: 'false',
  })
  const update = (key: keyof typeof filters, value: string) => {
    setFilters((old) =>
      key === 'hours' ? { ...old, hours: value, start: '', end: '' } : { ...old, [key]: value }
    )
    setPage(1)
    setExpanded(null)
  }
  const statsStart = useMemo(
    () =>
      filters.start
        ? new Date(filters.start + '+08:00').getTime() / 1000
        : Date.now() / 1000 - Number(filters.hours) * 3600,
    [filters.start, filters.hours]
  )
  const statsEnd = filters.end ? new Date(filters.end + '+08:00').getTime() / 1000 : undefined
  const load = useCallback(async () => {
    const id = ++serial.current
    setBusy(true)
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: '25',
        ascending: filters.ascending,
      })
      for (const key of [
        'session_id',
        'gate',
        'planner',
        'source',
        'spoken',
        'failed',
        'emoji',
      ] as const)
        if (filters[key]) params.set(key, filters[key])
      params.set('start', String(statsStart))
      if (statsEnd !== undefined) params.set('end', String(statsEnd))
      const result = await backendApi.get<Listing>(`/api/webui/decision-replay?${params}`)
      if (id === serial.current) {
        setListing(result)
        setError('')
      }
    } catch (e) {
      if (id === serial.current) setError(String(e))
    } finally {
      if (id === serial.current) setBusy(false)
    }
  }, [filters, page, statsStart, statsEnd])
  useEffect(() => {
    void load()
  }, [load])
  useEffect(() => {
    backendApi
      .get<typeof sessions>('/api/webui/decision-replay/sessions')
      .then(setSessions)
      .catch((e) => setError(String(e)))
  }, [])
  const select = (key: keyof typeof filters, title: string, choices: [string, string][]) => (
    <label className="space-y-1 text-xs">
      {title}
      <select
        aria-label={title}
        value={filters[key]}
        onChange={(e) => update(key, e.target.value)}
        className="bg-background text-foreground block h-9 w-full rounded border px-2"
      >
        {choices.map(([value, label]) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
    </label>
  )
  const summary = listing?.summary
  return (
    <div className="space-y-5 p-4 md:p-6">
      {observationTurn && <Card><CardHeader><CardTitle>观察机会关联回放 · {observationTurn.logical_turn_id}</CardTitle></CardHeader><CardContent><Timeline turn={observationTurn} /></CardContent></Card>}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">决策回放 / Decision Replay</h1>
        <Button disabled={busy} onClick={() => void load()}>
          刷新
        </Button>
      </div>
      <ReplyStatistics sessionId={filters.session_id} start={statsStart} end={statsEnd} />
      <p className="text-muted-foreground text-sm">
        只读取历史轨迹，不重新调用模型、执行工具或发送消息。旧版没有关联 ID
        的记录不补造；本页从启用记录后开始可用。
      </p>
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {listing && (
        <p className="text-muted-foreground text-xs">
          记录：{listing.enabled ? '启用' : '关闭，仍可查询历史'} · 保留 {listing.retention_days} 天
          · 丢弃事件 {listing.health.dropped_events} · 写入错误 {listing.health.write_errors} · 排队{' '}
          {listing.health.queue_size}
        </p>
      )}
      <Card>
        <CardContent className="grid gap-3 pt-5 sm:grid-cols-3 lg:grid-cols-5">
          {select('session_id', '会话', [
            ['', '全部会话'],
            ...sessions.map((s) => [s.session_id, s.name] as [string, string]),
          ])}
          {select('hours', '最近范围', [
            ['1', '1 小时'],
            ['6', '6 小时'],
            ['24', '24 小时'],
            ['168', '7 天'],
          ])}
          <label className="text-xs" htmlFor="replay-start">
            开始时间
            <Input
              id="replay-start"
              aria-label="开始时间"
              type="datetime-local"
              value={filters.start}
              onChange={(e) => update('start', e.target.value)}
            />
          </label>
          <label className="text-xs" htmlFor="replay-end">
            结束时间
            <Input
              id="replay-end"
              aria-label="结束时间"
              type="datetime-local"
              value={filters.end}
              onChange={(e) => update('end', e.target.value)}
            />
          </label>
          {select('spoken', '最终发言', [
            ['', '全部'],
            ['true', '成功文字发言'],
            ['false', '未成功文字发言'],
          ])}
          {select('gate', '门控', [
            ['', '全部'],
            ['pass', '通过'],
            ['reject', '拒绝'],
            ['deferred', '延后'],
          ])}
          {select('planner', 'Planner 结果', [
            ['', '全部'],
            ['reply', '调用 reply'],
            ['no_reply', '无回复动作'],
            ['wait', 'wait'],
            ['failed', '工具失败'],
          ])}
          {select('failed', '执行失败', [
            ['', '全部'],
            ['true', '包含失败'],
            ['false', '未记录失败'],
          ])}
          {select('emoji', '表情结果', [
            ['', '全部'],
            ['sent', '已发送'],
            ['not_sent', '未发送（含未执行）'],
            ['probability_miss', '概率未命中'],
            ['no_tag', '无 Tag'],
            ['no_candidates', '无候选'],
            ['score_below_threshold', '分数不足'],
            ['cooldown_blocked', '冷却拦截'],
            ['send_failed', '发送失败'],
          ])}
          {select('source', '触发来源', [
            ['', '全部'],
            ['message', '普通消息'],
            ['timeout', '等待超时'],
            ['proactive', '主动任务'],
            ['priority_at', '独立 @'],
            ['silence_emoji', '暖群表情'],
            ['independent_send', '独立发送'],
            ['auto_repeat', '自动复读'],
            ['independent_model', '独立模型任务'],
            ['tag_extraction', '标签提取'],
          ])}
          {select('ascending', '时间排序', [
            ['false', '最新优先'],
            ['true', '最早优先'],
          ])}
        </CardContent>
      </Card>
      {summary && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          {[
            ['外部调度触发', summary.external_triggers],
            ['门控通过', summary.gate_passed],
            ['Planner 请求', summary.planner_calls],
            ['成功文字发言回合', summary.spoken],
            ['Planner 无回复动作', summary.planner_no_reply],
            ['实际模型请求', summary.model_calls],
            ['输入 Token（已知部分）', summary.input_tokens],
            ['输出 Token（已知部分）', summary.output_tokens],
            ['费用估算（配置币种）', summary.cost],
            ['平均已结束回合耗时 ms', summary.avg_duration_ms],
          ].map(([label, value]) => (
            <Card key={String(label)}>
              <CardHeader className="pb-1">
                <CardTitle className="text-xs font-normal">{label}</CardTitle>
              </CardHeader>
              <CardContent className="text-xl">{number(value as number | null, 6)}</CardContent>
            </Card>
          ))}
        </div>
      )}
      {summary && (
        <p className="text-muted-foreground text-xs">
          门控通过率：
          {summary.gate_pass_rate == null
            ? '未知'
            : (summary.gate_pass_rate * 100).toFixed(1) + '%'}{' '}
          · 已知用量请求 {number(summary.measured_calls)}/{number(summary.model_calls)}
          。触发按调度批次计，模型请求按供应商尝试计，成功发言按逻辑回合计；不完整回合耗时不计入平均值。
        </p>
      )}
      {listing?.items.map((turn) => (
        <Card key={turn.logical_turn_id}>
          <CardHeader>
            <button
              className="space-y-2 text-left"
              onClick={() =>
                setExpanded((old) => (old === turn.logical_turn_id ? null : turn.logical_turn_id))
              }
              aria-expanded={expanded === turn.logical_turn_id}
            >
              <CardTitle className="text-base">
                {time(turn.started_at)} ·{' '}
                {turn.session_name ||
                  sessions.find((s) => s.session_id === turn.session_id)?.name ||
                  '未知会话'}{' '}
                · {turn.source}
              </CardTitle>
              <p className="text-muted-foreground text-sm">
                消息 {turn.message_ids.length} · P {number(turn.gate_detail?.probability, 3)} / 阈值{' '}
                {number(turn.gate_detail?.threshold, 3)} ·{' '}
                {turn.spoken
                  ? '已发言'
                  : (labels[turn.planner_result ?? ''] ??
                    labels[turn.gate_result ?? ''] ??
                    turn.outcome ??
                    '未完成')}{' '}
                · {number(turn.duration_ms)} ms · Token {number(turn.usage.input_tokens)}/
                {number(turn.usage.output_tokens)}
              </p>
            </button>
          </CardHeader>
          {expanded === turn.logical_turn_id && (
            <CardContent>
              <Timeline turn={turn} />
            </CardContent>
          )}
        </Card>
      ))}
      {!busy && listing?.total === 0 && (
        <p className="text-muted-foreground py-8 text-center">当前筛选范围没有决策记录。</p>
      )}
      <div className="flex items-center justify-between">
        <Button disabled={page <= 1 || busy} onClick={() => setPage((p) => p - 1)}>
          上一页
        </Button>
        <span className="text-sm">
          第 {page} 页 · 共 {listing?.total ?? 0} 条
        </span>
        <Button
          disabled={!listing || page * 25 >= listing.total || busy}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  )
}
