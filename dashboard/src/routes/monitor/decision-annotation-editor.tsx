import { useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'
import {
  annotations,
  labelNames,
  qualities,
  qualityNames,
  saveError,
  stateNames,
} from './annotation-api'
import type {
  Current,
  Label,
  Page,
  Quality,
  ReviewContext,
  Revision,
  SaveRequest,
  Target,
} from './annotation-api'
import { machineView, privateText, reviewTargets } from './annotation-presentation'
import { AnnotationMachineDetails } from './annotation-machine-details'

const selectClass = 'bg-background text-foreground w-full rounded-md border p-2 text-sm'
const date = (value: number) => new Date(value * 1000).toLocaleString('zh-CN')
type Draft = { label: Label | ''; reason: string; quality: Quality }
const empty: Draft = { label: '', reason: '', quality: 'identity_context_available' }
const draftFor = (current: Current): Draft =>
  current.annotation
    ? {
        label: current.annotation.label,
        reason: current.annotation.reason ?? '',
        quality: current.annotation.context_quality,
      }
    : empty

export function AnnotationEditor({
  target,
  onState,
  onSaved,
  active,
  locate,
}: {
  target: Target
  onState: (dirty: boolean, busy: boolean) => void
  onSaved: () => void
  active: boolean
  locate: number
}) {
  const [current, setCurrent] = useState<Current | null>(null)
  const [draft, setDraft] = useState<Draft>(empty)
  const [baseline, setBaseline] = useState<Draft>(empty)
  const [request, setRequest] = useState<Readonly<SaveRequest> | null>(null)
  const [busy, setBusy] = useState(false)
  const saving = useRef(false)
  const mounted = useRef(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [conflict, setConflict] = useState(false)
  const [latest, setLatest] = useState<Current | null>(null)
  const [history, setHistory] = useState<Page<Revision> | null>(null)
  const [historyPage, setHistoryPage] = useState(1)
  const [historyRefresh, setHistoryRefresh] = useState(0)
  const [historyError, setHistoryError] = useState('')
  const [context, setContext] = useState<ReviewContext | null>(null)
  const [contextPage, setContextPage] = useState(1)
  const [contextError, setContextError] = useState('')
  const [expanded, setExpanded] = useState(false)
  const contextAnchor = useRef<HTMLDivElement>(null)
  const text = useMemo(
    () => privateText(context?.privacy_replacements),
    [context?.privacy_replacements]
  )
  const targets = reviewTargets(current?.target, context)
  const machine = machineView(context?.machine)
  const ready = !!context && !!current
  useEffect(() => {
    if (!active || !ready) return
    // scrollIntoView handles nested scroll containers; scroll margin clears the fixed header.
    const frame = requestAnimationFrame(() => {
      contextAnchor.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
      contextAnchor.current?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [active, ready, locate])
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline) || request !== null
  useEffect(() => {
    onState(dirty, busy)
  }, [dirty, busy, onState])
  useEffect(() => {
    mounted.current = true
    let active = true
    annotations
      .current(target)
      .then((value) => {
        if (!active) return
        setCurrent(value)
        setDraft(draftFor(value))
        setBaseline(draftFor(value))
      })
      .catch((e) => {
        if (active) setError(saveError(e).message)
      })
    return () => {
      active = false
      mounted.current = false
    }
  }, [target])
  useEffect(() => {
    let active = true
    setContext(null)
    setContextError('')
    annotations
      .context(target, contextPage)
      .then((value) => {
        if (active) setContext(value)
      })
      .catch((e) => {
        if (active) setContextError(saveError(e).message)
      })
    return () => {
      active = false
    }
  }, [target, contextPage])
  useEffect(() => {
    let active = true
    setHistory(null)
    setHistoryError('')
    annotations
      .history(target, historyPage)
      .then((value) => {
        if (active) setHistory(value)
      })
      .catch((e) => {
        if (active) setHistoryError(saveError(e).message)
      })
    return () => {
      active = false
    }
  }, [target, historyPage, historyRefresh])

  function edit(change: Partial<Draft>) {
    // A real edit starts a different submission. Retrying without edits must use
    // the immutable previous payload, including its original expected_revision.
    setDraft((previous) => ({ ...previous, ...change }))
    setRequest(null)
    setNotice('')
  }
  async function save() {
    if (!current || !draft.label || saving.current || conflict) return
    const body =
      request ??
      Object.freeze({
        target_type: target.target_type,
        evaluation_id: target.evaluation_id,
        label: draft.label,
        reason: draft.reason || null,
        context_quality: draft.quality,
        expected_revision: current.current_revision,
        idempotency_key: `annotation:${crypto.randomUUID()}`,
        context_mode: 'decision_context_v1' as const,
      })
    setRequest(body)
    saving.current = true
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await annotations.save(target, body)
      if (!mounted.current) return
      if (result.persisted !== true) throw new Error('Missing persistence confirmation')
      // The server may return an older idempotently replayed revision. Always
      // refetch the real current version; never promote that old revision.
      setNotice(`服务端已确认持久化提交版本 ${result.annotation.revision}；正在核对当前版本…`)
      const value = await annotations.current(target)
      if (!mounted.current) return
      setCurrent(value)
      setDraft(draftFor(value))
      setBaseline(draftFor(value))
      setRequest(null)
      setNotice(
        `保存已确认 · 当前版本 ${value.current_revision}${result.idempotent_replay ? '（原请求幂等重放）' : ''}`
      )
      setHistoryPage(1)
      setHistoryRefresh((v) => v + 1)
      onSaved()
    } catch (e) {
      if (mounted.current) {
        const failure = saveError(e)
        setError(failure.message)
        setConflict(failure.conflict)
      }
    } finally {
      saving.current = false
      if (mounted.current) setBusy(false)
    }
  }
  async function inspectLatest() {
    try {
      const value = await annotations.current(target)
      if (mounted.current) {
        setLatest(value)
        setError('')
      }
    } catch (e) {
      if (mounted.current) setError(saveError(e).message)
    }
  }
  function rebase() {
    if (
      !latest ||
      !window.confirm('将保留本地草稿，并以查看到的服务器版本开始新一轮编辑。确认后仍需手动保存。')
    )
      return
    setCurrent(latest)
    setBaseline(draftFor(latest))
    setRequest(null)
    setConflict(false)
    setLatest(null)
    setError('')
    setNotice('已基于当前版本重新编辑；尚未保存。')
  }
  const count = Array.from(draft.reason).length
  // Only persisted evidence is a server quality judgment. Paginated context is
  // never enough to claim a complete snapshot; the server rechecks at save time.
  const floor = current?.annotation
    ? Math.max(
        qualities.indexOf(current.annotation.evidence.derived_context_quality),
        qualities.indexOf(current.annotation.context_quality)
      )
    : 0
  return (
    <section className="space-y-4" aria-label="人工批改详情">
      <Card className="gap-0 overflow-hidden py-0">
        <CardHeader className="border-b py-5">
          <CardTitle>人工批改</CardTitle>
          {current && (
            <p className="text-muted-foreground text-sm">
              {target.evaluation_id
                ? `本轮第 ${current.target.evaluation_sequence ?? '?'} 次回复判断`
                : '回复机会级补充判断'}{' '}
              · {date(current.target.context_boundary_at)}
            </p>
          )}
          {current && (
            <p className="text-muted-foreground text-xs">
              当前状态：{stateNames[current.annotation_state]} · 当前版本 {current.current_revision}
            </p>
          )}
        </CardHeader>
        <div
          ref={contextAnchor}
          tabIndex={-1}
          className="scroll-mt-24 space-y-3 px-6 pt-5 outline-none"
          aria-label="决策前上下文定位"
        >
          <h3 className="mb-3 font-semibold">A · 决策前上下文</h3>
          <div className="mb-4 space-y-2 rounded-lg border border-amber-400 bg-amber-50 p-4 text-amber-950 dark:bg-amber-950/30 dark:text-amber-100">
            <p className="font-semibold">本次判断目标：机器人是否应该回复下面这句话？</p>
            {targets.items.length === 0 ? (
              <p>
                目标消息无法唯一确认
                {targets.count > 0 ? `（记录关联 ${targets.count} 条候选消息，证据尚不完整）` : ''}
                。
              </p>
            ) : (
              <>
                {targets.items.length > 1 && (
                  <p>
                    本次判断范围包含 {targets.items.length}{' '}
                    条候选消息，未记录唯一触发句；请结合整组消息判断。
                  </p>
                )}
                {target.target_type === 'opportunity' && (
                  <p>以下为回复机会关联范围，不代表单次回复判断快照。</p>
                )}
                {targets.items.map((item) => (
                  <div key={item.message_event_id} className="space-y-1">
                    <p className="font-medium break-words whitespace-pre-wrap">
                      {item.text || '正文为空 / 不可恢复'}
                    </p>
                    <p className="text-xs">
                      发送者：
                      {item.is_bot && (
                        <span className="mr-1 rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-800 dark:bg-blue-900 dark:text-blue-100">
                          机器人
                        </span>
                      )}
                      {item.name ?? '未知发送者'} · 时间：{date(item.event_timestamp)}
                    </p>
                    {item.text_truncated && <p className="text-xs">正文已截断。</p>}
                  </div>
                ))}
              </>
            )}
          </div>
          <p className="text-muted-foreground mb-3 text-xs">
            黄色消息属于本次判断范围；其他消息仅作参考。正文来自当前可读记录，不代表当时完整输入。匿名代号仅在本回复机会内使用。
          </p>
          {contextError && <p role="alert">{contextError}</p>}
          {!context && !contextError && <p>加载上下文…</p>}
          {context && (
            <>
              <p role="status" className="text-sm">
                {context.context_status !== 'partial_reconstruction'
                  ? '关键上下文不可恢复：聊天数据库或表不可用。'
                  : !context.membership_available ||
                      (context.unavailable_message_event_ids?.length ?? 0) > 0 ||
                      context.items.some((item) => item.status !== 'available')
                    ? '部分上下文缺失，不能声称完整恢复。'
                    : '当前页身份和正文可读；尚未核验全部页面，保存时由服务端保守复核。'}
              </p>
              {context.unavailable_message_event_ids?.length ? (
                <p>缺失关联消息：{context.unavailable_message_event_ids.length} 条</p>
              ) : null}
              {context.bot_history_status && context.bot_history_status !== 'available' && (
                <p role="status" className="text-muted-foreground text-xs">
                  {context.bot_history_status === 'read_limit'
                    ? '机器人历史发言超过安全读取上限，本次未补入；未显示不代表没有历史发言。'
                    : '机器人身份或发送时间证据不可用，本次未补入机器人历史；未显示不代表没有历史发言。'}
                </p>
              )}
              {context.items.some((item) => item.membership === 'bot_history_reference') && (
                <p className="text-muted-foreground text-xs">
                  机器人历史消息用于辅助理解群聊，不代表已确认属于当时的完整模型输入；时间显示为平台确认时刻。
                </p>
              )}
              {context.items.map((item, index) => (
                <article
                  className={
                    targets.ids.has(item.message_event_id)
                      ? 'rounded-md border border-amber-400 bg-amber-50 p-3 text-sm text-amber-950 dark:bg-amber-950/30 dark:text-amber-100'
                      : item.is_bot
                        ? 'rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-950 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-100'
                        : 'bg-muted rounded-md p-3 text-sm'
                  }
                  data-review-target={targets.ids.has(item.message_event_id) || undefined}
                  data-review-bot={item.is_bot || undefined}
                  key={`${item.membership}:${item.message_event_id}:${index}`}
                >
                  <p className="text-muted-foreground text-xs">
                    {item.is_bot && (
                      <>
                        <span className="mr-1 rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-800 dark:bg-blue-900 dark:text-blue-100">
                          机器人
                        </span>
                        大肥鱼 ·{' '}
                      </>
                    )}
                    {targets.ids.has(item.message_event_id)
                      ? targets.items.length === 1
                        ? '目标消息'
                        : '判断范围 · 候选消息'
                      : item.membership === 'evaluation_member'
                        ? '决策前消息'
                        : item.membership === 'reference_only'
                          ? '重建参考上下文'
                          : item.membership === 'bot_history_reference'
                            ? '机器人历史发言 · 决策前参考'
                            : '回复机会关联消息'}{' '}
                    · {item.time_basis === 'platform_confirmation' ? '确认于 ' : ''}
                    {date(item.event_timestamp)}
                    {!item.is_bot && <> · {item.name ?? '未知发送者'}</>}
                  </p>
                  <p className="mt-2 break-words whitespace-pre-wrap">
                    {item.status === 'available'
                      ? item.text || (item.is_bot ? '历史发言正文不可恢复' : '正文为空 / 不可恢复')
                      : item.is_bot
                        ? '历史发言正文不可恢复'
                        : '正文缺失或身份有歧义'}
                  </p>
                  {item.text_truncated && <p>正文已截断。</p>}
                </article>
              ))}
              {!context.items.length && <p>当前页无可恢复上下文。证据不足时可选择无法判断。</p>}
              <div className="flex flex-wrap gap-3">
                <Button
                  variant="outline"
                  disabled={contextPage <= 1}
                  onClick={() => setContextPage((p) => p - 1)}
                >
                  上下文上一页
                </Button>
                <span>
                  第 {contextPage} 页 · {context.total} 条
                </span>
                <Button
                  variant="outline"
                  disabled={contextPage * 25 >= context.total}
                  onClick={() => setContextPage((p) => p + 1)}
                >
                  上下文下一页
                </Button>
              </div>
            </>
          )}
        </div>
        <div className="mt-5 space-y-4 border-t px-6 py-5">
          <h3 className="font-semibold">B · 人工判断</h3>
          <p className="text-sm">
            仅判断：基于这个决策点可获得的上下文，大肥鱼是否应该发言？不评价回复文采、发送成败或群友反馈。
          </p>
          <fieldset disabled={busy || !current}>
            <legend className="mb-2 text-sm">选择一个判断</legend>
            <div className="flex flex-wrap gap-4">
              {(Object.keys(labelNames) as Label[]).map((value) => (
                <label key={value} className="flex items-center gap-2">
                  <input
                    type="radio"
                    name={`label-${target.evaluation_id ?? target.opportunity_id}`}
                    checked={draft.label === value}
                    onChange={() => edit({ label: value })}
                  />
                  {labelNames[value]}
                </label>
              ))}
            </div>
          </fieldset>
          <label
            className="block space-y-2"
            htmlFor={`reason-${target.evaluation_id ?? target.opportunity_id}`}
          >
            <span>理由（可选，最多2000字符）</span>
            <Textarea
              id={`reason-${target.evaluation_id ?? target.opportunity_id}`}
              autoResize={false}
              rows={4}
              disabled={busy || !current || !context?.privacy_replacements}
              value={context?.privacy_replacements ? text(draft.reason) : ''}
              onChange={(event) => edit({ reason: event.target.value })}
            />
          </label>
          <p className={count > 2000 ? 'text-destructive' : 'text-muted-foreground'}>
            {count} / 2000
          </p>
          <p className="text-sm">
            服务端保存质量：
            {current?.annotation
              ? qualityNames[current.annotation.context_quality]
              : '尚未保存；提交时由服务端核验'}
            。质量描述与人工标签分别保存。
          </p>
          <label
            className="block space-y-2"
            htmlFor={`quality-${target.evaluation_id ?? target.opportunity_id}`}
          >
            <span>主动降低证据质量（不能提高服务端判定）</span>
            <select
              id={`quality-${target.evaluation_id ?? target.opportunity_id}`}
              className={selectClass}
              disabled={busy || !current}
              value={draft.quality}
              onChange={(event) => edit({ quality: event.target.value as Quality })}
            >
              {qualities.map((value, index) => (
                <option key={value} value={value} disabled={index < floor}>
                  {qualityNames[value]}
                </option>
              ))}
            </select>
          </label>
          <p className="text-muted-foreground text-xs">
            服务端保存时取更保守的质量；当前正文不是当时完整
            Prompt。现有契约不记录阅读回执，也不能证明严格盲评。实际结果展开与否不伪造证据。
          </p>
          {error && (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          )}
          {notice && <p role="status">{notice}</p>}
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={
                busy ||
                !current ||
                !draft.label ||
                count > 2000 ||
                draft.reason.includes('\0') ||
                conflict ||
                !dirty
              }
              onClick={() => void save()}
            >
              {busy ? '保存中…' : request ? '以原请求同键重试' : '保存判断'}
            </Button>
            {conflict && (
              <Button variant="outline" onClick={() => void inspectLatest()}>
                查看服务器当前版本
              </Button>
            )}
            {latest && (
              <Button variant="outline" onClick={rebase}>
                保留草稿并重新编辑
              </Button>
            )}
          </div>
          {latest && (
            <div className="bg-muted rounded p-3 text-sm">
              <p>
                服务器当前版本 {latest.current_revision} ·{' '}
                {latest.annotation ? labelNames[latest.annotation.label] : '未批改'}
              </p>
              <p className="whitespace-pre-wrap">
                {context?.privacy_replacements
                  ? text(latest.annotation?.reason ?? '无理由')
                  : '匿名化信息尚不可用'}
              </p>
            </div>
          )}
          {request && (
            <p className="text-muted-foreground text-xs break-all">
              待确认提交 · 基于批改版本 {request.expected_revision}
              。不自动重试；编辑内容会开始新的提交。
            </p>
          )}
        </div>
        <div className="space-y-4 border-t px-6 py-5">
          <h3 className="font-semibold">C · 机器判断</h3>
          <div className="bg-muted/40 space-y-2 rounded-lg border p-4 text-sm">
            <p>
              <strong>门控结果：</strong>
              {machine.gate}
            </p>
            <p>
              <strong>Planner 决策：</strong>
              {machine.planner}
            </p>
            <div className="space-y-1">
              <strong>主要判断原因：</strong>
              <p>门控原因：{machine.gateReason}</p>
              <p>{machine.plannerReason}</p>
            </div>
            <p>
              <strong>最终执行结果：</strong>
              {machine.execution}
            </p>
            <p className="text-muted-foreground text-xs">
              门控、规划工具选择与发送结果分别取证，不是人工判断的标准答案。规划仅汇总可独立关联本次判断的已观察轮次；未执行仅指明确记录的调度尝试，不能证明所有历史尝试均未执行。
            </p>
          </div>
          <details onToggle={(event) => setExpanded(event.currentTarget.open)}>
            <summary className="cursor-pointer rounded-md border p-3 text-sm font-medium">
              展开完整决策细节
            </summary>
            {expanded && active && context?.privacy_replacements && (
              <>
                <pre className="bg-muted mt-3 max-h-96 overflow-auto rounded p-3 text-xs whitespace-pre-wrap">
                  {text(
                    JSON.stringify(
                      {
                        target: current?.target,
                        machine: context.machine,
                        context: { ...context, privacy_replacements: undefined },
                      },
                      null,
                      2
                    )
                  )}
                </pre>
                <AnnotationMachineDetails target={target} text={text} />
              </>
            )}
            {!context?.privacy_replacements && <p>匿名化信息尚不可用，暂不能安全显示原始详情。</p>}
          </details>
        </div>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>版本历史（只读）</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {historyError && <p role="alert">{historyError}</p>}
          {history?.items.map((item) => (
            <article className="bg-muted space-y-1 rounded p-3 text-sm" key={item.revision}>
              <p>
                版本 {item.revision}{' '}
                {item.revision === current?.current_revision ? '· 当前版本' : ''} ·{' '}
                {labelNames[item.label]} · {date(item.revision_created_at)}
              </p>
              <p>
                {item.actor_id === 'webui:shared-admin'
                  ? 'Dashboard 管理员（共享主体）'
                  : '批改管理员'}
              </p>
              <p>{qualityNames[item.context_quality]}</p>
              <p className="break-words whitespace-pre-wrap">
                {context?.privacy_replacements
                  ? text(item.reason ?? '未填写理由')
                  : '匿名化信息尚不可用'}
              </p>
            </article>
          ))}
          {history && !history.total && <p>暂无保存历史。</p>}
          <div className="flex flex-wrap gap-3">
            <Button
              variant="outline"
              disabled={!history || historyPage <= 1}
              onClick={() => setHistoryPage((p) => p - 1)}
            >
              历史上一页
            </Button>
            <span>
              第 {historyPage} 页 · {history?.total ?? 0} 个版本
            </span>
            <Button
              variant="outline"
              disabled={!history || historyPage * 10 >= history.total}
              onClick={() => setHistoryPage((p) => p + 1)}
            >
              历史下一页
            </Button>
          </div>
        </CardContent>
      </Card>
    </section>
  )
}
