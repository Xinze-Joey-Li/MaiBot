import { useEffect, useRef, useState } from 'react'
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
  results,
}: {
  target: Target
  onState: (dirty: boolean, busy: boolean) => void
  onSaved: () => void
  results: React.ReactNode
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
      <Card>
        <CardHeader>
          <CardTitle>决策点身份</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm break-all">
          <p>
            机会 {target.opportunity_id} ·{' '}
            {target.evaluation_id
              ? `Evaluation ${target.evaluation_id}`
              : '独立机会（无 Evaluation）'}
          </p>
          {current && (
            <>
              <p>
                评估序号 {current.target.evaluation_sequence ?? '不适用'} · 特征版本{' '}
                {current.target.feature_version ?? '不适用'} · 决策时间{' '}
                {date(current.target.context_boundary_at)}
              </p>
              <p>
                快照摘要 {current.target.feature_snapshot_hash ?? '不适用'} · 评估事实摘要{' '}
                {current.target.evaluation_facts_hash ?? '不适用'}
              </p>
              <p>
                当前状态：{stateNames[current.annotation_state]} · 当前版本{' '}
                {current.current_revision}
              </p>
            </>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>A · 决策前上下文</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-muted-foreground text-sm">
            A 是 Evaluation 快照成员；B
            仅为重建参考；机会关联不是评估快照。正文是当前可读记录，不是冻结
            Prompt。仅使用接口给出的决策前参考消息。
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
                <p>缺失观察消息：{context.unavailable_message_event_ids.join('、')}</p>
              ) : null}
              {context.items.map((item, index) => (
                <article
                  className="bg-muted rounded-md p-3 text-sm"
                  key={`${item.membership}:${item.message_event_id}:${index}`}
                >
                  <p className="text-muted-foreground text-xs">
                    {item.membership === 'evaluation_member'
                      ? 'A · 本次评估成员'
                      : item.membership === 'reference_only'
                        ? 'B · 重建参考上下文'
                        : '机会关联（非评估快照）'}{' '}
                    · {date(item.event_timestamp)} · {item.name ?? '姓名不可用'}
                  </p>
                  <p className="mt-2 break-words whitespace-pre-wrap">
                    {item.status === 'available'
                      ? item.text || '正文为空 / 不可恢复'
                      : '正文缺失或身份有歧义'}
                  </p>
                  {item.text_truncated && <p>正文已截断。</p>}
                </article>
              ))}
              {!context.items.length && <p>当前页无可恢复上下文。证据不足时可选择无法判断。</p>}
              {context.limitations.map((text) => (
                <p className="text-muted-foreground text-xs" key={text}>
                  {text}
                </p>
              ))}
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
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>B · 人工判断</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
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
              disabled={busy || !current}
              value={draft.reason}
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
              <p className="whitespace-pre-wrap">{latest.annotation?.reason ?? '无理由'}</p>
            </div>
          )}
          {request && (
            <p className="text-muted-foreground text-xs break-all">
              待确认请求 {request.idempotency_key} · 基于版本 {request.expected_revision}
              。不自动重试；编辑内容会开始新的提交。
            </p>
          )}
        </CardContent>
      </Card>
      <details className="rounded-lg border p-4">
        <summary className="cursor-pointer font-medium">C · 展开实际决策与执行结果</summary>
        {results}
      </details>
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
                  : item.actor_id}
              </p>
              <p>{qualityNames[item.context_quality]}</p>
              <p className="break-words whitespace-pre-wrap">{item.reason ?? '未填写理由'}</p>
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
