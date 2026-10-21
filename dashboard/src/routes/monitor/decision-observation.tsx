import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { backendApi } from '@/lib/http'
import { DecisionAnnotations } from './decision-annotations'

type Fact = Record<string, unknown>
type Opportunity = {
  opportunity_id: string
  session_id: string
  session_name?: string
  created_at: number
  source_type: string
  message_count: number
  gate: string
  planner: string
  generation: string
  send: string
  confirmation: string
}
type Listing = {
  items: Opportunity[]
  total: number
  summary: Record<string, number | null>
  units: string
}
type Health = {
  database_exists: boolean
  schema_version: number | null
  process: Fact
  stored_counts: Fact
  sessions: { session_id: string; name: string }[]
  authorization_model: string
  authorized_group_count: number
}
type Detail = {
  opportunity: Opportunity
  groups: Record<string, { items: Fact[]; total: number | null; available: boolean }>
  replay: { logical_turn_id: string; status: string }[]
  limitations: string[]
  replay_total?: number
  replay_truncated?: boolean
}
type Context = {
  items: {
    message_event_id: string
    membership: string
    status: string
    name?: string
    text?: string
    received_at: number | null
    event_timestamp: number
    is_at?: boolean
    is_mentioned?: boolean
    reply_to?: string
    text_truncated?: boolean
  }[]
  total: number
  membership_available: boolean
  context_status: string
  expected_associated_count?: number
  unavailable_message_event_ids?: string[]
  limitations: string[]
}
const api = '/api/webui/decision-observation'
const date = (value: number) => new Date(value * 1000).toLocaleString('zh-CN')
const labels: Record<string, string> = {
  message_events: '关联消息（去重）',
  opportunities: '机会',
  evaluations: '门控评估',
  gate_pass: '评估通过',
  gate_reject: '评估拒绝',
  gate_defer: '评估延后',
  planner_turns: '实际Planner逻辑回合',
  planner_starts: 'Planner执行启动（去重）',
  generation_calls: 'Replyer生成调用',
  driver_attempts: 'Driver发送尝试',
  platform_unique_messages: '已确认平台唯一消息',
  platform_confirmed_opportunities: '有可信确认的机会',
  definitive_failure_opportunities: '有明确失败证据的机会',
  unknown_opportunities: '回执结论未知的机会',
  not_observed_opportunities: '未观察回执的机会',
  confirmed_attempts: '可信确认尝试',
  definitive_failure_attempts: '明确失败尝试',
  unknown_attempts: '未知结果尝试',
  unobserved_attempts: '未观察回执尝试',
  local_attempts: '本地渲染尝试（不适用）',
  conflicting_attempts: '存在冲突证据的尝试',
  not_observed: '未观察',
  not_applicable: '不适用',
  started: '已启动',
  pass: '通过',
  reject: '拒绝',
  defer: '延后',
  generation_started: '开始生成',
  generation_completed: '生成完成',
  generation_failed: '生成失败',
  interrupted: '中断',
  send_attempted: '已发起尝试',
  platform_confirmed: '存在可信确认',
  definitive_failure: '存在明确失败',
  unknown: '未知',
  ordinary_pending: '普通发言',
  priority_at: '独立 @',
}
const label = (value: string) => labels[value] ?? value
const selectClass = 'bg-background text-foreground h-9 w-full rounded-md border px-2 text-sm'
function Facts({ fact }: { fact: Fact }) {
  return (
    <pre className="bg-muted max-h-96 overflow-auto rounded-lg p-3 text-xs break-all whitespace-pre-wrap">
      {JSON.stringify(fact, null, 2)}
    </pre>
  )
}
function Pager({
  page,
  total,
  size,
  change,
}: {
  page: number
  total: number
  size: number
  change: (page: number) => void
}) {
  return (
    <div className="flex items-center gap-3 text-sm">
      <Button variant="outline" disabled={page <= 1} onClick={() => change(page - 1)}>
        上一页
      </Button>
      <span>
        第 {page} 页 · 共 {total} 条
      </span>
      <Button variant="outline" disabled={page * size >= total} onClick={() => change(page + 1)}>
        下一页
      </Button>
    </div>
  )
}

export function DecisionObservationPage() {
  const [review, setReview] = useState(false)
  const [health, setHealth] = useState<Health | null>(null)
  const [data, setData] = useState<Listing | null>(null)
  const [error, setError] = useState('')
  const [healthError, setHealthError] = useState('')
  const [busy, setBusy] = useState(false)
  const [page, setPage] = useState(1)
  const [refresh, setRefresh] = useState(() => Date.now() / 1000)
  const [customStart, setCustomStart] = useState('')
  const [customEnd, setCustomEnd] = useState('')
  const [filters, setFilters] = useState({
    session_id: '',
    hours: '24',
    source_type: '',
    gate: '',
    planner: '',
    generation: '',
    send: '',
    confirmation: '',
  })
  const serial = useRef(0)
  const bounds = useMemo(() => {
    const end = customEnd ? new Date(customEnd).getTime() / 1000 : refresh
    return {
      end,
      start: customStart
        ? new Date(customStart).getTime() / 1000
        : end - Number(filters.hours) * 3600,
    }
    // Refresh deliberately fixes a new time window; paging retains the same bounds.
  }, [customStart, customEnd, filters.hours, refresh])
  useEffect(() => {
    let active = true
    backendApi
      .get<Health>(`${api}/health`)
      .then((result) => {
        if (active) {
          setHealth(result)
          setHealthError('')
        }
      })
      .catch((e) => {
        if (active) setHealthError(String(e))
      })
    return () => {
      active = false
    }
  }, [refresh])
  useEffect(() => {
    const id = ++serial.current
    setBusy(true)
    const query = new URLSearchParams({
      ...filters,
      page: String(page),
      page_size: '25',
      start: String(bounds.start),
      end: String(bounds.end),
    })
    query.delete('hours')
    backendApi
      .get<Listing>(`${api}/opportunities?${query}`)
      .then((result) => {
        if (id === serial.current) {
          setData(result)
          setError('')
        }
      })
      .catch((e) => {
        if (id === serial.current) {
          setError(String(e))
          setData(null)
        }
      })
      .finally(() => {
        if (id === serial.current) setBusy(false)
      })
    return () => {
      serial.current++
    }
  }, [filters, page, refresh, bounds])
  const choices: Record<string, [string, string][]> = {
    session_id: [
      ['', '所有授权群'],
      ...(health?.sessions ?? []).map((s): [string, string] => [s.session_id, s.name]),
    ],
    hours: [
      ['1', '最近1小时'],
      ['24', '最近24小时'],
      ['168', '最近7天'],
      ['744', '最近31天'],
    ],
    source_type: [
      ['', '全部来源'],
      ['ordinary_pending', '普通发言'],
      ['priority_at', '独立 @'],
    ],
    gate: [
      ['', '全部门控'],
      ...['pass', 'reject', 'defer', 'not_observed'].map((s): [string, string] => [s, label(s)]),
    ],
    planner: [
      ['', '全部Planner状态'],
      ...['started', 'not_applicable', 'not_observed'].map((s): [string, string] => [s, label(s)]),
    ],
    generation: [
      ['', '全部生成状态'],
      ...[
        'generation_started',
        'generation_completed',
        'generation_failed',
        'interrupted',
        'not_applicable',
        'not_observed',
      ].map((s): [string, string] => [s, label(s)]),
    ],
    send: [
      ['', '全部发送状态'],
      ['send_attempted', '已发起尝试'],
      ['not_observed', '未观察'],
    ],
    confirmation: [
      ['', '全部确认状态'],
      ...['platform_confirmed', 'definitive_failure', 'unknown', 'not_observed'].map(
        (s): [string, string] => [s, label(s)]
      ),
    ],
  }
  if (review) return <main className="space-y-5 p-4 md:p-6"><DecisionAnnotations onClose={() => setReview(false)}/></main>
  return (
    <main className="space-y-5 p-4 md:p-6">
      <Button variant="outline" onClick={() => setReview(true)}>进入人工批改</Button>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">决策观察中心</h1>
          <p className="text-muted-foreground text-sm">只读事实 · 从发言机会追溯真实执行与回执</p>
        </div>
        <Button variant="outline" onClick={() => setRefresh(Date.now() / 1000)}>
          刷新
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {healthError && (
        <p role="alert" className="text-destructive">
          {healthError}
        </p>
      )}
      <Card>
        <CardHeader>
          <CardTitle>采集健康</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {!health ? (
            <p>{healthError ? '健康信息暂不可用，请刷新重试。' : '正在读取健康状态…'}</p>
          ) : (
            <>
              <p>
                采集{health.process.enabled ? '开启' : '关闭'} ·{' '}
                {health.database_exists
                  ? `数据库 schema ${health.schema_version}`
                  : '尚无观察数据库'}{' '}
                · 授权群 {health.authorized_group_count} · 队列 {String(health.process.queue_size)}{' '}
                / {String(health.process.queue_capacity)}
              </p>
              <p className="text-muted-foreground">
                {health.authorization_model}。打开此页不会开启采集或创建数据库。
              </p>
              <details>
                <summary className="cursor-pointer">进程计数与存储计数（不同口径）</summary>
                <p>submitted / dropped / write_errors 等仅属于本次进程；历史缺失指标显示未知。</p>
                <Facts
                  fact={{
                    process_lifetime_only: health.process,
                    stored_rows_authorized_only: health.stored_counts,
                  }}
                />
              </details>
            </>
          )}
        </CardContent>
      </Card>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {Object.entries(choices).map(([key, values]) => (
          <label key={key} className="space-y-1 text-xs">
            <span>
              {
                (
                  {
                    session_id: '群聊',
                    hours: '时间范围',
                    source_type: '来源',
                    gate: '门控',
                    planner: 'Planner',
                    generation: '生成',
                    send: '发送',
                    confirmation: '平台确认',
                  } as Record<string, string>
                )[key]
              }
            </span>
            <select
              aria-label={key}
              className={selectClass}
              value={filters[key as keyof typeof filters]}
              onChange={(e) => {
                setFilters((old) => ({ ...old, [key]: e.target.value }))
                setPage(1)
              }}
            >
              {values.map(([value, title]) => (
                <option key={value} value={value}>
                  {title}
                </option>
              ))}
            </select>
          </label>
        ))}
      </div>
      <p className="text-muted-foreground text-xs">
        {data?.units}
        。列表状态是存在性摘要，详情保留多次尝试；未知不等于失败。本页展示只读事实，人工判断请进入批改工作台。
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <label htmlFor="observation-start" className="space-y-1 text-xs">
          自定义开始（浏览器本地时间，最多31天）
          <Input
            id="observation-start"
            type="datetime-local"
            value={customStart}
            onChange={(e) => {
              setCustomStart(e.target.value)
              setPage(1)
            }}
          />
        </label>
        <label htmlFor="observation-end" className="space-y-1 text-xs">
          自定义结束（留空为本次刷新时刻）
          <Input
            id="observation-end"
            type="datetime-local"
            value={customEnd}
            onChange={(e) => {
              setCustomEnd(e.target.value)
              setPage(1)
            }}
          />
        </label>
      </div>
      <p className="text-muted-foreground text-xs">
        实际范围：{date(bounds.start)} 至 {date(bounds.end)}
        。指标以所选机会创建时刻为队列口径；展示其当前保留的关联事实，后到回执可能更新结果。
      </p>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {Object.entries(data?.summary ?? {}).map(([key, value]) => (
          <Card key={key}>
            <CardContent className="pt-4">
              <p className="text-muted-foreground text-xs">{label(key)}</p>
              <p className="mt-1 text-xl font-semibold">
                {value == null ? '未知' : value.toLocaleString()}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Card>
        <CardContent className="pt-4">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr>
                  {[
                    '机会 / 群聊',
                    '创建时间',
                    '来源 / 消息数',
                    '门控',
                    'Planner',
                    '生成',
                    '发送',
                    '确认（存在性）',
                  ].map((title) => (
                    <th key={title} className="p-2 font-medium">
                      {title}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data?.items.map((item) => (
                  <tr key={item.opportunity_id} className="border-t">
                    <td className="p-2">
                      <Link
                        className="text-primary underline"
                        to="/decision-observation/$opportunityId"
                        params={{ opportunityId: item.opportunity_id }}
                      >
                        {item.opportunity_id.slice(0, 18)}
                      </Link>
                      <p className="text-muted-foreground text-xs">{item.session_name}</p>
                    </td>
                    <td className="p-2">{date(item.created_at)}</td>
                    <td className="p-2">
                      {label(item.source_type)} / {item.message_count}
                    </td>
                    {[item.gate, item.planner, item.generation, item.send, item.confirmation].map(
                      (state, index) => (
                        <td key={index} className="p-2">
                          {label(state)}
                        </td>
                      )
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {busy && <p role="status">加载中…</p>}
          {!busy && !error && !data?.items.length && (
            <p className="text-muted-foreground py-8 text-center">
              此范围暂无观察记录。采集关闭、未授权或历史已清理均可能为空。
            </p>
          )}
          <Pager page={page} total={data?.total ?? 0} size={25} change={setPage} />
        </CardContent>
      </Card>
    </main>
  )
}

export function DecisionObservationDetailPage() {
  const { opportunityId = '' } = useParams({ strict: false })
  return <OpportunityDetail key={opportunityId} opportunityId={opportunityId} />
}

export function OpportunityDetail({ opportunityId, initialEvaluation = '', embedded = false }: { opportunityId: string; initialEvaluation?: string; embedded?: boolean }) {
  const [data, setData] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [evaluation, setEvaluation] = useState(initialEvaluation)
  const [contextPage, setContextPage] = useState(1)
  const [context, setContext] = useState<Context | null>(null)
  useEffect(() => {
    let active = true
    setData(null)
    backendApi
      .get<Detail>(`${api}/opportunities/${encodeURIComponent(opportunityId)}?page=${page}`)
      .then((value) => {
        if (active) {
          setData(value)
          setError('')
        }
      })
      .catch((e) => {
        if (active) setError(String(e))
      })
    return () => {
      active = false
    }
  }, [opportunityId, page])
  useEffect(() => {
    let active = true
    setContext(null)
    backendApi
      .get<Context>(
        `${api}/opportunities/${encodeURIComponent(opportunityId)}/context?evaluation_id=${encodeURIComponent(evaluation)}&page=${contextPage}`
      )
      .then((value) => {
        if (active) {
          setContext(value)
          setError('')
        }
      })
      .catch((e) => {
        if (active) setError(String(e))
      })
    return () => {
      active = false
    }
  }, [opportunityId, evaluation, contextPage])
  const Container = embedded ? 'section' : 'main'
  return (
    <Container className="space-y-5 p-4 md:p-6">
      {!embedded && <Link to="/decision-observation" className="text-primary underline">
        返回观察中心
      </Link>}
      <h1 className="text-2xl font-semibold">机会执行详情</h1>
      <p className="text-muted-foreground text-sm break-all">{opportunityId}</p>
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      {!data && !error && <p>加载中…</p>}
      {data && (
        <>
          <Card>
            <CardContent className="space-y-3 pt-4">
              <p className="font-medium">
                {data.opportunity.session_name ?? '群聊名称不可用'} ·{' '}
                {label(data.opportunity.source_type)} · {date(data.opportunity.created_at)}
              </p>
              <p className="text-sm">
                门控 {label(data.opportunity.gate)} · Planner {label(data.opportunity.planner)} ·{' '}
                {label(data.opportunity.generation)} · {label(data.opportunity.confirmation)}
              </p>
              <details>
                <summary className="cursor-pointer text-sm">机会身份与原始记录</summary>
                <Facts fact={data.opportunity} />
              </details>
              {data.limitations.map((text) => (
                <p className="text-muted-foreground text-xs" key={text}>
                  {text}
                </p>
              ))}
              {data.replay.map((trace) => (
                <p key={trace.logical_turn_id} className="text-sm break-all">
                  Replay {trace.logical_turn_id} ·{' '}
                  {trace.status === 'available' ? (
                    <Link
                      to="/decision-replay"
                      search={{
                        observationTurn: trace.logical_turn_id,
                        observationSession: data.opportunity.session_id,
                      }}
                    >
                      打开现有回放
                    </Link>
                  ) : (
                    `不可用：${trace.status}`
                  )}
                </p>
              ))}
              {data.replay_truncated && (
                <p className="text-muted-foreground text-xs">
                  Replay关联共 {data.replay_total} 条，仅显示前100条；未显示不代表不存在。
                </p>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>决策时聊天上下文</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-muted-foreground text-sm">
                选择当次评估（留空只看机会关联，非评估快照）。A 是快照明确关联的消息；B
                仅是接收时间可证明的参考上下文，均不声称完整还原 Planner Prompt。
              </p>
              <label htmlFor="observation-evaluation" className="block space-y-1 text-sm">
                评估 ID
                <Input
                  id="observation-evaluation"
                  aria-label="评估 ID"
                  list="observation-evaluations"
                  value={evaluation}
                  onChange={(e) => {
                    setEvaluation(e.target.value)
                    setContextPage(1)
                  }}
                />
              </label>
              <datalist id="observation-evaluations">
                {data.groups.evaluations?.items.map((e) => (
                  <option key={String(e.evaluation_id)} value={String(e.evaluation_id)}>
                    {String(e.gate_result)} · {date(Number(e.evaluated_at))}
                  </option>
                ))}
              </datalist>
              {context && (
                <>
                  <p className="text-xs">
                    {context.context_status} · 快照成员
                    {context.membership_available ? '可用' : '缺失'}
                  </p>
                  {context.expected_associated_count != null && (
                    <p className="text-xs">
                      明确关联 {context.expected_associated_count} 条；观察消息身份缺失{' '}
                      {context.unavailable_message_event_ids?.length ?? 0}{' '}
                      条。主聊天正文缺失或身份有歧义时按各条记录标注。
                    </p>
                  )}
                  {context.limitations.map((text) => (
                    <p className="text-muted-foreground text-xs" key={text}>
                      {text}
                    </p>
                  ))}
                  {context.items.map((message) => (
                    <article
                      key={message.message_event_id}
                      className={`rounded-lg border p-3 ${message.membership === 'evaluation_member' ? 'border-primary/50 bg-primary/5' : 'bg-muted/40'}`}
                    >
                      <div className="flex flex-wrap gap-2 text-xs">
                        <strong>
                          {message.membership === 'evaluation_member'
                            ? 'A · 本次评估成员'
                            : message.membership === 'opportunity_associated'
                              ? '机会关联 · 非评估快照'
                              : 'B · 参考上下文'}
                        </strong>
                        <span>{message.name ?? '成员不可用'}</span>
                        <time>{date(message.event_timestamp)}</time>
                      </div>
                      <p className="mt-2 text-sm break-words whitespace-pre-wrap">
                        {message.status === 'available'
                          ? message.text || '（无可读文本）'
                          : '消息已缺失或身份有歧义，无法重建'}
                      </p>
                      <p className="text-muted-foreground mt-1 text-xs">
                        {message.is_at && '实际消息含 @ 标记 · '}
                        {message.reply_to && `引用 ${message.reply_to} · `}
                        {message.text_truncated && '文本已截断 · '}触发消息未单独断言
                      </p>
                    </article>
                  ))}
                  <Pager
                    page={contextPage}
                    total={context.total}
                    size={25}
                    change={setContextPage}
                  />
                </>
              )}
            </CardContent>
          </Card>
          {Object.entries(data.groups).map(([name, group]) => (
            <Card key={name}>
              <CardHeader>
                <CardTitle>
                  {(
                    {
                      evaluations: '门控评估与冻结特征',
                      execution_events: '排队、消费与 Planner 执行',
                      reply_events: 'Replyer、分段与发送尝试',
                      delivery_receipts: '已关联平台回执与证据',
                      attempt_states: '发送尝试当前结论（独立于原始回执）',
                      session_unlinked_receipts: '同群未关联回执（不代表本机会确认）',
                      message_events: '关联消息身份与接收事实',
                      transfers: '路径转移关系',
                    } as Record<string, string>
                  )[name] ?? name}{' '}
                  · {group.total ?? '未接入'}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {group.items.map((fact, index) => (
                  <div
                    key={String(fact.event_id ?? fact.evaluation_id ?? fact.receipt_id ?? index)}
                  >
                    <p className="mb-1 text-xs">
                      {fact.occurred_at || fact.evaluated_at || fact.observed_at
                        ? date(Number(fact.occurred_at ?? fact.evaluated_at ?? fact.observed_at))
                        : ''}{' '}
                      · {label(String(fact.stage ?? fact.gate_result ?? fact.conclusion ?? ''))} ·{' '}
                      {label(String(fact.status ?? ''))}
                    </p>
                    {name === 'evaluations' && (
                      <p className="mb-2 text-sm">
                        实际概率 {String(fact.calculated_probability ?? '未计算')} · 生效阈值{' '}
                        {String(fact.effective_threshold ?? '未计算')} · 原因{' '}
                        {String(fact.decision_reason ?? '未观察')}
                      </p>
                    )}
                    {name === 'execution_events' &&
                      Array.isArray(fact.tool_names) &&
                      fact.tool_names.length > 0 && (
                        <p className="mb-2 text-sm">实际工具：{fact.tool_names.join('、')}</p>
                      )}
                    <details>
                      <summary className="cursor-pointer text-sm">查看记录、身份与缺失原因</summary>
                      <Facts fact={fact} />
                    </details>
                  </div>
                ))}
                {!group.items.length && (
                  <p className="text-muted-foreground text-sm">
                    {group.available ? '本页无记录；不得据此推断未执行。' : '此历史schema未接入。'}
                  </p>
                )}
              </CardContent>
            </Card>
          ))}
          <Pager
            page={page}
            total={Math.max(...Object.values(data.groups).map((g) => g.total ?? 0), 0)}
            size={50}
            change={setPage}
          />
        </>
      )}
    </Container>
  )
}
