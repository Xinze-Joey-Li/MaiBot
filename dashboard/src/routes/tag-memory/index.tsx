import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BookOpen,
  GitBranch,
  MessageCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  Tags,
} from 'lucide-react'

import { backendApi } from '@/lib/http'
import './tag-memory.css'

type Tag = {
  id: string
  batch_id: string
  name: string
  summary: string
  category_path: string[]
  status: 'valid' | 'superseded' | 'disputed'
  is_active: boolean
  auto_active: number
  requires_long_term: boolean
  revision: number
  manual_active_until: number | null
  created_at: number
  source_message_ids: string[]
}
type Tree = { id: string; name: string; tags: Tag[]; children: Tree[] }
type Topic = {
  id: string
  title: string
  summary: string
  is_active: number
  tag_ids: string[]
  last_active_at: number
}
type Conflict = {
  id: string
  old_tag_id: string | null
  new_tag_id: string | null
  decision: string
  reason: string
  evidence: string
  created_at: number
  manual_decision: string | null
  manual_reason: string | null
  resolved_at: number | null
}
type Overview = {
  tree: Tree
  topics: Topic[]
  conflicts: Conflict[]
  rules: {
    batch_size: number
    max_wait_seconds: number
    retention_days: number
    cleanup_interval_days: number
  }
}
type Detail = Tag & {
  evidence: { message_id: string; quote: string }[]
  revisions: {
    id: string
    reason: string
    created_at: number
    before_json: string
    after_json: string
  }[]
}
type Batch = {
  id: string
  summary: string
  created_at: number
  tags: { name: string; summary: string }[]
}
type Form = {
  name: string
  summary: string
  is_active: boolean
  requires_long_term: boolean
  reason: string
}
const statusLabels = { valid: '有效', superseded: '已替代', disputed: '存疑' }
const decisions: Record<string, string> = {
  replace_old: '采用新信息',
  keep_old: '保留旧信息',
  uncertain: '暂时存疑',
}
const date = (value: number) => new Date(value * 1000).toLocaleString('zh-CN')
function flatten(tree?: Tree): Tag[] {
  return tree ? [...tree.tags, ...tree.children.flatMap(flatten)] : []
}

export function TagMemoryPage() {
  const client = useQueryClient()
  const [session, setSession] = useState('')
  const [tab, setTab] = useState<'tree' | 'topics' | 'conflicts'>('tree')
  const [category, setCategory] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('all')
  const [selected, setSelected] = useState<string | null>(null)
  const [form, setForm] = useState<Form | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [deleteReason, setDeleteReason] = useState('')
  const [resolution, setResolution] = useState<Conflict | null>(null)
  const [resolutionChoice, setResolutionChoice] = useState('replace_old')
  const [resolutionReason, setResolutionReason] = useState('')
  const [showBatches, setShowBatches] = useState(false)
  const sessions = useQuery({
    queryKey: ['tag-memory-sessions'],
    queryFn: () =>
      backendApi.get<{
        sessions: { session_id: string; name: string }[]
        enabled: boolean
      }>('/api/webui/tag-memory/sessions'),
  })
  useEffect(() => {
    if (!session && sessions.data?.sessions.length) setSession(sessions.data.sessions[0].session_id)
  }, [sessions.data, session])
  const base = `/api/webui/tag-memory/${encodeURIComponent(session)}`
  const overview = useQuery({
    queryKey: ['tag-memory', session, 'overview'],
    enabled: !!session && sessions.data?.enabled === true,
    queryFn: () => backendApi.get<Overview>(base),
  })
  const detail = useQuery({
    queryKey: ['tag-memory', session, 'detail', selected],
    enabled: !!session && !!selected,
    queryFn: () => backendApi.get<Detail>(`${base}/tags/${encodeURIComponent(selected!)}`),
  })
  const batches = useQuery({
    queryKey: ['tag-memory', session, 'batches', detail.data?.batch_id],
    enabled: !!session && !!detail.data?.batch_id && showBatches,
    queryFn: () => backendApi.get<{ batches: Batch[] }>(`${base}/batches/${detail.data!.batch_id}`),
  })
  useEffect(() => {
    if (detail.data)
      setForm({
        name: detail.data.name,
        summary: detail.data.summary,
        is_active: Boolean(detail.data.is_active),
        requires_long_term: Boolean(detail.data.requires_long_term),
        reason: '',
      })
  }, [detail.data])
  const tags = flatten(overview.data?.tree)
  const visible = tags.filter(
    (tag) =>
      category.every((part, index) => tag.category_path[index] === part) &&
      `${tag.name} ${tag.summary} ${tag.category_path.join(' ')}`
        .toLowerCase()
        .includes(search.toLowerCase()) &&
      (filter === 'all' ||
        (filter === 'long'
          ? tag.requires_long_term
          : filter === 'active'
            ? tag.is_active
            : tag.status === filter))
  )

  async function act(operation: () => Promise<unknown>, message: string) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await operation()
      await client.invalidateQueries({ queryKey: ['tag-memory', session] })
      setNotice(message)
      return true
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '操作失败')
      return false
    } finally {
      setBusy(false)
    }
  }
  function selectTag(id: string) {
    setSelected(id)
    setForm(null)
    setDeleting(false)
    setShowBatches(false)
    setError('')
    setNotice('')
  }
  function treeNode(node: Tree, path: string[] = []) {
    const count = flatten(node).length
    return (
      <div key={node.id} className="tm-tree-node">
        <button
          className={`tm-tree-button ${JSON.stringify(category) === JSON.stringify(path) ? 'selected' : ''}`}
          onClick={() => {
            setCategory(path)
            setSelected(null)
          }}
        >
          <GitBranch size={15} />
          <span>{node.name}</span>
          <small>{count}</small>
        </button>
        {!!node.children.length && (
          <div className="tm-tree-children">
            {node.children.map((child) => treeNode(child, [...path, child.name]))}
          </div>
        )}
      </div>
    )
  }
  const queryError = sessions.error || overview.error
  return (
    <div className="tm-page">
      <header className="tm-header">
        <div>
          <div className="tm-eyebrow">
            <Tags size={16} /> QQ AGENT · MEMORY
          </div>
          <h1>标签记忆</h1>
          <p>查看对话留下的记忆，管理持续话题，纠正冲突信息。</p>
        </div>
        <div className="tm-header-actions">
          <select
            aria-label="选择群会话"
            value={session}
            disabled={busy}
            onChange={(event) => {
              setSession(event.target.value)
              setSelected(null)
              setCategory([])
              setForm(null)
              setResolution(null)
              setError('')
              setNotice('')
            }}
          >
            <option value="" disabled>
              选择群会话
            </option>
            {sessions.data?.sessions.map((item) => (
              <option key={item.session_id} value={item.session_id}>
                {item.name}
              </option>
            ))}
          </select>
          <button
            disabled={busy || !session}
            onClick={() => void client.invalidateQueries({ queryKey: ['tag-memory', session] })}
          >
            <RefreshCw size={16} />
            刷新
          </button>
        </div>
      </header>
      <div className="tm-stats">
        <div>
          <Tags />
          <strong>{tags.length}</strong>
          <span>标签总数</span>
        </div>
        <div>
          <MessageCircle />
          <strong>{overview.data?.topics.filter((topic) => topic.is_active).length ?? 0}</strong>
          <span>活跃话题</span>
        </div>
        <div>
          <BookOpen />
          <strong>{tags.filter((tag) => tag.requires_long_term).length}</strong>
          <span>长期保留</span>
        </div>
        <div>
          <ShieldCheck />
          <strong>
            {overview.data?.conflicts.filter(
              (item) => (item.manual_decision ?? item.decision) === 'uncertain'
            ).length ?? 0}
          </strong>
          <span>存疑冲突</span>
        </div>
      </div>
      {overview.data && (
        <div className="tm-rule">
          每 {overview.data.rules.batch_size} 条消息 / {overview.data.rules.max_wait_seconds / 60}{' '}
          分钟提取 · 每 {overview.data.rules.cleanup_interval_days} 天清理 · 保留活跃和长期标签
        </div>
      )}
      {(error || queryError) && (
        <div role="alert" className="tm-alert">
          {error || (queryError as Error).message}
        </div>
      )}
      {notice && (
        <div role="status" className="tm-notice">
          {notice}
        </div>
      )}
      {sessions.data?.enabled === false && (
        <div className="tm-alert">标签记忆尚未启用，请先在机器人配置中开启。</div>
      )}
      {sessions.data && !sessions.data.sessions.length && (
        <div className="tm-empty">
          暂无群会话。机器人接收到群消息并创建聊天流后，将在这里显示群名称。
        </div>
      )}
      <nav className="tm-tabs" aria-label="记忆管理页签">
        {(
          [
            ['tree', '标签树', GitBranch],
            ['topics', '活跃话题', MessageCircle],
            ['conflicts', '冲突记录', ShieldCheck],
          ] as const
        ).map(([key, label, Icon]) => (
          <button
            key={key}
            aria-pressed={tab === key}
            className={tab === key ? 'selected' : ''}
            onClick={() => setTab(key)}
          >
            <Icon size={17} />
            {label}
          </button>
        ))}
      </nav>
      {overview.isLoading && <div className="tm-empty">正在读取标签记忆…</div>}
      {overview.data && tab === 'tree' && (
        <div className="tm-workspace">
          <aside className="tm-panel tm-tree">
            <h2>分类目录</h2>
            <p>记忆按当前群独立保存</p>
            {treeNode(overview.data.tree)}
          </aside>
          <section className="tm-panel tm-list">
            <div className="tm-search">
              <Search size={17} />
              <input
                aria-label="搜索标签"
                placeholder="搜索名称、摘要或人物 ID"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              <select
                aria-label="筛选标签"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
              >
                <option value="all">全部标签</option>
                <option value="valid">有效</option>
                <option value="active">活跃</option>
                <option value="long">长期保留</option>
                <option value="disputed">存疑</option>
                <option value="superseded">已替代</option>
              </select>
            </div>
            <div className="tm-list-title">
              <h2>{category.length ? category.join(' / ') : '全部标签'}</h2>
              <span>{visible.length} 个标签</span>
            </div>
            {!visible.length && (
              <div className="tm-empty">
                没有匹配的标签。新的有效群消息完成批量提取后，会自动出现在这里。
              </div>
            )}
            {visible.map((tag) => (
              <button
                className={`tm-tag-card ${selected === tag.id ? 'selected' : ''}`}
                key={tag.id}
                onClick={() => selectTag(tag.id)}
              >
                <div>
                  <strong>{tag.name}</strong>
                  <div className="tm-badges">
                    <span className={`tm-badge ${tag.status}`}>{statusLabels[tag.status]}</span>
                    {tag.is_active && <span className="tm-badge">活跃</span>}
                    {!!tag.requires_long_term && <span className="tm-badge">长期保留</span>}
                  </div>
                </div>
                <p>{tag.summary}</p>
                <small>
                  {tag.category_path.join(' / ')} · {date(tag.created_at)} ·{' '}
                  {tag.source_message_ids.length} 条证据
                </small>
              </button>
            ))}
          </section>
        </div>
      )}
      {overview.data && tab === 'topics' && (
        <section className="tm-panel">
          <h2>持续讨论的话题</h2>
          <p className="tm-muted">
            连续两次提取延续同一具体话题时自动激活。手动激活保护 24
            小时，到期后交回自动判断；非长期标签按普通规则统一清理。
          </p>
          {!overview.data.topics.length && <div className="tm-empty">暂无持续话题。</div>}
          {overview.data.topics.map((topic) => (
            <article className="tm-topic" key={topic.id}>
              <div>
                <h3>
                  {topic.title}{' '}
                  <span className="tm-badge">{topic.is_active ? '活跃' : '已结束'}</span>
                </h3>
                <p>{topic.summary}</p>
                <small>最近活动：{date(topic.last_active_at)}</small>
                <div className="tm-linked-tags">
                  {topic.tag_ids
                    .map((id) => tags.find((tag) => tag.id === id))
                    .filter((tag): tag is Tag => !!tag)
                    .map((tag) => (
                      <button
                        key={tag.id}
                        onClick={() => {
                          selectTag(tag.id)
                          setTab('tree')
                        }}
                      >
                        {tag.name}
                      </button>
                    ))}
                </div>
              </div>
              <button
                disabled={busy}
                onClick={() =>
                  void act(
                    () =>
                      backendApi.patch(`${base}/topics/${topic.id}`, {
                        body: { active: !topic.is_active },
                      }),
                    topic.is_active ? '话题已结束' : '话题已激活'
                  )
                }
              >
                {topic.is_active ? '结束话题' : '激活话题'}
              </button>
            </article>
          ))}
        </section>
      )}
      {overview.data && tab === 'conflicts' && (
        <section className="tm-panel">
          <h2>冲突裁决与人工纠正</h2>
          <p className="tm-muted">
            每条冲突链只保留最新裁决。旧结果被替代后不能重新提交，人工纠正会更新当前裁决及理由。
          </p>
          {!overview.data.conflicts.length && <div className="tm-empty">暂无冲突记录。</div>}
          {[...overview.data.conflicts].reverse().map((conflict) => {
            const evidence = JSON.parse(conflict.evidence) as {
              old: {
                name: string
                summary: string
                sources: { message_id: string; quote: string }[]
              }
              new: { name: string; summary: string; evidence_quotes: Record<string, string> }
            }
            return (
              <article className="tm-conflict" key={conflict.id}>
                <div className="tm-list-title">
                  <h3>
                    {decisions[conflict.manual_decision ?? conflict.decision]}{' '}
                    <span className="tm-badge">
                      {conflict.manual_decision ? '人工纠正' : '模型裁决'}
                    </span>
                  </h3>
                  <small>{date(conflict.resolved_at ?? conflict.created_at)}</small>
                </div>
                <div className="tm-comparison">
                  <div>
                    <small>旧信息 · {evidence.old.name}</small>
                    <p>{evidence.old.summary}</p>
                    {evidence.old.sources.map((source) => (
                      <blockquote key={source.message_id}>
                        {source.quote}
                        <small>{source.message_id}</small>
                      </blockquote>
                    ))}
                  </div>
                  <div>
                    <small>新信息 · {evidence.new.name}</small>
                    <p>{evidence.new.summary}</p>
                    {Object.entries(evidence.new.evidence_quotes).map(([id, quote]) => (
                      <blockquote key={id}>
                        {quote}
                        <small>{id}</small>
                      </blockquote>
                    ))}
                  </div>
                </div>
                <p className="tm-muted">最新裁决理由：{conflict.reason}</p>
                <button
                  disabled={
                    busy ||
                    !tags.some((tag) => tag.id === conflict.old_tag_id) ||
                    !tags.some((tag) => tag.id === conflict.new_tag_id)
                  }
                  onClick={() => {
                    setResolution(conflict)
                    setResolutionChoice(conflict.manual_decision ?? conflict.decision)
                    setResolutionReason('')
                  }}
                >
                  人工纠正
                </button>
              </article>
            )
          })}
        </section>
      )}
      {selected && tab === 'tree' && (
        <div className="tm-overlay">
          <section
            className="tm-dialog tm-detail"
            role="dialog"
            aria-modal="true"
            aria-label="标签详情"
          >
            <div className="tm-list-title">
              <h2>标签详情与编辑</h2>
              <button disabled={busy} onClick={() => setSelected(null)}>
                关闭
              </button>
            </div>
            {detail.isLoading && <p>正在读取证据…</p>}
            {detail.error && <p role="alert">{detail.error.message}</p>}
            {error && (
              <p role="alert" className="tm-alert">
                {error}
              </p>
            )}
            {detail.data && form && (
              <>
                <label>
                  标签名称
                  <input
                    maxLength={120}
                    value={form.name}
                    onChange={(event) => setForm({ ...form, name: event.target.value })}
                  />
                </label>
                <label>
                  精简摘要
                  <textarea
                    rows={3}
                    maxLength={600}
                    value={form.summary}
                    onChange={(event) => setForm({ ...form, summary: event.target.value })}
                  />
                </label>
                <div className="tm-checks">
                  <label>
                    <input
                      type="checkbox"
                      checked={form.is_active}
                      disabled={detail.data.status !== 'valid'}
                      onChange={(event) => setForm({ ...form, is_active: event.target.checked })}
                    />
                    手动保护 24 小时
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={form.requires_long_term}
                      onChange={(event) =>
                        setForm({ ...form, requires_long_term: event.target.checked })
                      }
                    />
                    长期保留
                  </label>
                </div>
                {detail.data.manual_active_until != null && (
                  <p className="tm-muted">
                    手动保护至 {date(detail.data.manual_active_until)}，到期后交回自动活跃判断。
                  </p>
                )}
                <label>
                  修改理由
                  <input
                    maxLength={1000}
                    placeholder="说明这次修订的依据"
                    value={form.reason}
                    onChange={(event) => setForm({ ...form, reason: event.target.value })}
                  />
                </label>
                <div className="tm-dialog-actions">
                  <button
                    disabled={
                      busy || !form.reason.trim() || !form.name.trim() || !form.summary.trim()
                    }
                    onClick={() =>
                      void act(
                        () =>
                          backendApi.patch(`${base}/tags/${selected}`, {
                            body: { ...form, revision: detail.data!.revision },
                          }),
                        '标签已保存，原证据和修订记录保留'
                      )
                    }
                  >
                    保存修改
                  </button>
                  <button
                    disabled={busy}
                    className="outline"
                    onClick={() => {
                      setDeleting(true)
                      setDeleteReason('')
                    }}
                  >
                    删除标签
                  </button>
                </div>
                <h3>原始消息证据</h3>
                {detail.data.evidence.map((source) => (
                  <blockquote key={source.message_id}>
                    {source.quote || '该早期标签没有保存引文'}
                    <small>消息 ID：{source.message_id}</small>
                  </blockquote>
                ))}
                <button className="outline" onClick={() => setShowBatches(!showBatches)}>
                  {showBatches ? '收起相邻批次' : '查看前后批次摘要'}
                </button>
                {showBatches && (
                  <div>
                    {batches.isLoading && <p>正在读取批次…</p>}
                    {batches.error && <p role="alert">{batches.error.message}</p>}
                    {batches.data?.batches.map((batch) => (
                      <article className="tm-batch" key={batch.id}>
                        <small>
                          {date(batch.created_at)}
                          {batch.id === detail.data!.batch_id ? ' · 当前批次' : ''}
                        </small>
                        <p>{batch.summary}</p>
                      </article>
                    ))}
                  </div>
                )}
                <h3>人工修订记录</h3>
                {!detail.data.revisions.length && <p className="tm-muted">暂无人工修订。</p>}
                {detail.data.revisions.map((revision) => (
                  <div className="tm-batch" key={revision.id}>
                    <small>{date(revision.created_at)}</small>
                    <p>{revision.reason}</p>
                    <details>
                      <summary>查看修订前后内容</summary>
                      <p>修改前：{JSON.parse(revision.before_json).summary}</p>
                      <p>修改后：{JSON.parse(revision.after_json).summary}</p>
                    </details>
                  </div>
                ))}
              </>
            )}
          </section>
        </div>
      )}
      {deleting && detail.data && (
        <div className="tm-overlay tm-top">
          <section className="tm-dialog" role="dialog" aria-modal="true" aria-label="确认删除标签">
            <h2>删除标签</h2>
            <p>“{detail.data.name}”将停止展示和检索。原证据和修订记录按既有清理周期保留。</p>
            <label>
              删除理由
              <input
                value={deleteReason}
                maxLength={1000}
                onChange={(event) => setDeleteReason(event.target.value)}
              />
            </label>
            <div className="tm-dialog-actions">
              <button
                disabled={busy || !deleteReason.trim()}
                onClick={async () => {
                  if (
                    await act(
                      () =>
                        backendApi.delete(`${base}/tags/${selected}`, {
                          body: { revision: detail.data!.revision, reason: deleteReason },
                        }),
                      '标签已删除'
                    )
                  ) {
                    setDeleting(false)
                    setSelected(null)
                  }
                }}
              >
                确认删除
              </button>
              <button className="outline" disabled={busy} onClick={() => setDeleting(false)}>
                取消
              </button>
            </div>
            {error && <p role="alert">{error}</p>}
          </section>
        </div>
      )}
      {resolution && (
        <div className="tm-overlay">
          <section className="tm-dialog" role="dialog" aria-modal="true" aria-label="人工纠正冲突">
            <h2>人工纠正冲突</h2>
            <label>
              处理结果
              <select
                value={resolutionChoice}
                onChange={(event) => setResolutionChoice(event.target.value)}
              >
                {Object.entries(decisions).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              纠正理由
              <textarea
                rows={3}
                maxLength={1000}
                value={resolutionReason}
                onChange={(event) => setResolutionReason(event.target.value)}
                placeholder="说明采用哪一方信息，以及依据"
              />
            </label>
            <div className="tm-dialog-actions">
              <button
                disabled={busy || !resolutionReason.trim()}
                onClick={async () => {
                  if (
                    await act(
                      () =>
                        backendApi.post(`${base}/conflicts/${resolution.id}/resolve`, {
                          body: {
                            decision: resolutionChoice,
                            reason: resolutionReason,
                            expected_resolved_at: resolution.resolved_at,
                          },
                        }),
                      '人工裁决已保存'
                    )
                  )
                    setResolution(null)
                }}
              >
                保存裁决
              </button>
              <button disabled={busy} className="outline" onClick={() => setResolution(null)}>
                取消
              </button>
            </div>
            {error && <p role="alert">{error}</p>}
          </section>
        </div>
      )}
    </div>
  )
}
