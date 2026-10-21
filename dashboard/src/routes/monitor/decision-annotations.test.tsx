import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DecisionAnnotations } from './decision-annotations'
import { annotations, annotationBase } from './annotation-api'
import type { Current, Revision, SaveRequest, TargetRow } from './annotation-api'

const router = vi.hoisted(() => ({ blocker: vi.fn() }))
vi.mock('@tanstack/react-router', () => ({ useBlocker: router.blocker }))
vi.mock('./decision-observation', () => ({
  OpportunityDetail: ({
    opportunityId,
    initialEvaluation,
  }: {
    opportunityId: string
    initialEvaluation: string
  }) => (
    <p>
      真实只读结果 {opportunityId} / {initialEvaluation}
    </p>
  ),
}))

const row = (id: string | null = 'e1'): TargetRow => ({
  opportunity_id: 'o1',
  evaluation_id: id,
  target_type: id ? 'evaluation' : 'opportunity',
  session_id: 'g1',
  source_type: id ? 'ordinary_pending' : 'priority_at',
  context_boundary_at: 100,
  evaluation_sequence: id === 'e2' ? 2 : id ? 1 : null,
  current_revision: 0,
  label: null,
  annotation_state: 'unannotated',
})
const metadata = (id: string | null = 'e1') => ({
  ...row(id),
  feature_version: id ? 'features-v1' : null,
  feature_snapshot_hash: id ? `snapshot-${id}` : null,
  evaluation_facts_hash: id ? `facts-${id}` : null,
  membership_available: !!id,
  message_event_ids: ['m1'],
})
const revision = (n = 1, label: Revision['label'] = 'should_reply'): Revision => ({
  revision: n,
  label,
  reason: null,
  actor_id: 'webui:shared-admin',
  revision_created_at: 100 + n,
  context_quality: 'identity_context_available',
  declared_context_quality: 'identity_context_available',
  evidence: {
    derived_context_quality: 'identity_context_available',
    target: metadata(),
    context_view_receipt: 'not_collected',
  },
})
const current = (id: string | null = 'e1', saved: Revision | null = null): Current => ({
  target: metadata(id),
  annotation: saved,
  current_revision: saved?.revision ?? 0,
  annotation_state: !saved
    ? 'unannotated'
    : saved.label === 'uncertain'
      ? 'uncertain'
      : 'annotated',
  contract_version: 'annotation-v1',
})
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
let rows: TargetRow[]
let versions: Map<string, Revision>
let requests: SaveRequest[]
let contexts: Record<string, unknown>
let saveHandler: ((body: SaveRequest) => Response | Promise<Response>) | undefined
let currentHandler: ((id: string | null) => Response | Promise<Response>) | undefined
let historyTotal: number
let fetchMock: ReturnType<typeof vi.fn>
function idKey(value: string | null) {
  return value ?? 'opportunity'
}
function defer<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

beforeEach(() => {
  rows = [row('e1'), row('e2'), row(null)]
  versions = new Map()
  requests = []
  saveHandler = undefined
  currentHandler = undefined
  historyTotal = 0
  contexts = {
    items: [
      {
        message_event_id: 'm1',
        membership: 'evaluation_member',
        status: 'available',
        text: '合成决策前消息',
        event_timestamp: 90,
      },
      {
        message_event_id: 'm2',
        membership: 'reference_only',
        status: 'available',
        text: '合成参考消息',
        event_timestamp: 80,
      },
    ],
    total: 2,
    membership_available: true,
    expected_associated_count: 1,
    unavailable_message_event_ids: [],
    context_status: 'partial_reconstruction',
    limitations: ['当前正文不是冻结 Prompt'],
  }
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.spyOn(window, 'alert').mockImplementation(() => undefined)
  fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost')
    const id = url.searchParams.get('evaluation_id')
    if (url.pathname.endsWith('/health'))
      return json({ sessions: [{ session_id: 'g1', name: '合成测试群' }] })
    if (url.pathname.endsWith('/annotation-targets'))
      return json({
        items: rows,
        total: rows.length,
        page: Number(url.searchParams.get('page')),
        page_size: 25,
      })
    if (url.pathname.endsWith('/context')) return json(contexts)
    if (url.pathname.endsWith('/history'))
      return json({
        items: [...versions.values()].reverse(),
        total: historyTotal || versions.size,
        page: Number(url.searchParams.get('page')),
        page_size: 10,
      })
    if (init?.method === 'PUT') {
      const body = JSON.parse(init.body as string) as SaveRequest
      requests.push(body)
      if (saveHandler) return saveHandler(body)
      const saved = {
        ...revision(body.expected_revision + 1, body.label),
        reason: body.reason,
        context_quality: body.context_quality,
      }
      versions.set(idKey(body.evaluation_id), saved)
      return json({
        persisted: true,
        annotation: saved,
        current_revision: saved.revision,
        idempotent_replay: false,
      })
    }
    return currentHandler ? currentHandler(id) : json(current(id, versions.get(idKey(id)) ?? null))
  })
  vi.stubGlobal('fetch', fetchMock)
})
async function open(id = 'e1') {
  render(<DecisionAnnotations onClose={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: `批改 ${id}` }))
  await screen.findByText('当前状态：未批改 · 当前版本 0')
}
function choose(label = '应该回复', reason?: string) {
  fireEvent.click(screen.getByRole('radio', { name: label }))
  if (reason !== undefined)
    fireEvent.change(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/),
      { target: { value: reason } }
    )
}
async function saved() {
  fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
  await screen.findByText(/保存已确认/)
}

describe('annotation review with the actual cookie HTTP client', () => {
  it.each(['应该回复', '不应该回复', '无法判断'])(
    'saves explicit label %s with exact v1 request',
    async (label) => {
      await open()
      choose(label)
      expect(requests).toHaveLength(0)
      await saved()
      expect(requests[0]).toEqual({
        target_type: 'evaluation',
        evaluation_id: 'e1',
        label:
          label === '应该回复'
            ? 'should_reply'
            : label === '不应该回复'
              ? 'should_not_reply'
              : 'uncertain',
        reason: null,
        context_quality: 'identity_context_available',
        expected_revision: 0,
        idempotency_key: expect.stringMatching(/^annotation:[a-z0-9-]+$/),
        context_mode: 'decision_context_v1',
      })
      expect(await screen.findByText(/Dashboard 管理员（共享主体）/)).toBeInTheDocument()
    }
  )
  it('keeps independent evaluation drafts and identities when switching', async () => {
    await open()
    choose('应该回复', '第一评估草稿')
    fireEvent.click(screen.getByRole('button', { name: '批改 e2' }))
    await screen.findByText(/Evaluation e2/)
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
    ).toHaveValue('')
    expect(screen.getByText(/snapshot-e2/)).toBeInTheDocument()
    choose('不应该回复')
    await saved()
    expect(requests[0].evaluation_id).toBe('e2')
    fireEvent.click(screen.getByRole('button', { name: '批改 e1' }))
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
    ).toHaveValue('第一评估草稿')
    await saved()
    expect(requests[1].evaluation_id).toBe('e1')
  })
  it('saves the legitimate no-evaluation priority opportunity without invented ID', async () => {
    await open('o1')
    choose()
    await saved()
    expect(requests[0].target_type).toBe('opportunity')
    expect(requests[0].evaluation_id).toBeNull()
  })
  it('shows uncertainty separately from unannotated and uses server filters', async () => {
    rows[1] = { ...rows[1], annotation_state: 'uncertain', current_revision: 1 }
    render(<DecisionAnnotations onClose={vi.fn()} />)
    await screen.findByText('r1')
    expect(screen.getAllByText('人工不确定').length).toBeGreaterThan(1)
    fireEvent.change(screen.getByLabelText('批改状态'), { target: { value: 'uncertain' } })
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('annotation_state=uncertain'),
        expect.anything()
      )
    )
  })
  it('updates through a new revision and a new submission key', async () => {
    await open()
    choose()
    await saved()
    choose('不应该回复')
    await saved()
    expect(requests.map((r) => r.expected_revision)).toEqual([0, 1])
    expect(requests[0].idempotency_key).not.toBe(requests[1].idempotency_key)
    expect(screen.getByText(/当前状态：已批改 · 当前版本 2/)).toBeInTheDocument()
  })
  it('paginates history on the server and marks the current revision', async () => {
    historyTotal = 11
    await open()
    choose()
    await saved()
    expect(await screen.findByText(/版本 1 · 当前版本/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '历史下一页' }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/history\?.*page=2/),
        expect.anything()
      )
    )
  })
  it('allows 2000 code points and rejects 2001 without a write', async () => {
    await open()
    choose('应该回复', '鱼'.repeat(2000))
    expect(screen.getByRole('button', { name: '保存判断' })).toBeEnabled()
    fireEvent.change(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/),
      { target: { value: '鱼'.repeat(2001) } }
    )
    expect(screen.getByRole('button', { name: '保存判断' })).toBeDisabled()
    expect(requests).toHaveLength(0)
  })
  it('counts unicode characters consistently with Python rather than UTF16 units', async () => {
    await open()
    choose('应该回复', '🐟'.repeat(2000))
    expect(screen.getByText('2000 / 2000')).toBeInTheDocument()
    await saved()
    expect(requests[0].reason).toBe('🐟'.repeat(2000))
  })
  it('flags missing and ambiguous context without calling it a frozen prompt', async () => {
    contexts = { ...contexts, unavailable_message_event_ids: ['missing'] }
    await open()
    expect(screen.getByText(/部分上下文缺失，不能声称完整恢复/)).toBeInTheDocument()
    expect(screen.getByText(/缺失观察消息：missing/)).toBeInTheDocument()
    expect(screen.getByText('B · 重建参考上下文', { exact: false })).toBeInTheDocument()
  })
  it('shows critical unavailability while still allowing an uncertain judgment', async () => {
    contexts = { ...contexts, items: [], total: 0, context_status: 'chat_database_unavailable' }
    await open()
    expect(screen.getByText(/关键上下文不可恢复：/)).toBeInTheDocument()
    choose('无法判断')
    await saved()
  })
  it('starts with results collapsed and does not request outcomes', async () => {
    await open()
    const summary = screen.getByText('C · 展开实际决策与执行结果')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    expect(screen.queryByText(/真实只读结果/)).not.toBeInTheDocument()
  })
  it('loads existing read-only results on explicit expansion and load', async () => {
    await open()
    fireEvent.click(screen.getByText('C · 展开实际决策与执行结果'))
    fireEvent.click(screen.getByRole('button', { name: '加载结果记录' }))
    expect(screen.getByText('真实只读结果 o1 / e1')).toBeInTheDocument()
  })
  it('mounts only the active read-only view while preserving independent drafts', async () => {
    await open()
    fireEvent.click(screen.getByText('C · 展开实际决策与执行结果'))
    fireEvent.click(screen.getByRole('button', { name: '加载结果记录' }))
    fireEvent.click(screen.getByRole('button', { name: '批改 e2' }))
    await screen.findByText(/Evaluation e2/)
    expect(screen.queryByText('真实只读结果 o1 / e1')).not.toBeInTheDocument()
    const active = within(screen.getByRole('region', { name: '人工批改详情' }))
    fireEvent.click(active.getByText('C · 展开实际决策与执行结果'))
    fireEvent.click(active.getByRole('button', { name: '加载结果记录' }))
    expect(screen.getByText('真实只读结果 o1 / e2')).toBeInTheDocument()
    expect(screen.getAllByText(/真实只读结果/)).toHaveLength(1)
  })
  it('does not claim success before persistence confirmation and prevents duplicate clicks', async () => {
    const pending = defer<Response>()
    saveHandler = () => pending.promise
    await open()
    choose()
    fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
    expect(screen.getByRole('button', { name: '保存中…' })).toBeDisabled()
    expect(screen.queryByText(/保存已确认/)).not.toBeInTheDocument()
    await waitFor(() => expect(requests).toHaveLength(1))
    await act(async () => pending.resolve(json({ persisted: false })))
    expect(await screen.findByText(/保存结果尚未确认/)).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: '应该回复' })).toBeChecked()
  })
  it('preserves draft on revision conflict and rebases only after an explicit inspection', async () => {
    saveHandler = () =>
      json({ detail: { code: 'annotation_revision_conflict', current_revision: 3 } }, 409)
    await open()
    choose('不应该回复', '本地保留')
    fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
    await screen.findByText(/保存冲突：/)
    expect(requests).toHaveLength(1)
    versions.set('e1', revision(3))
    fireEvent.click(screen.getByRole('button', { name: '查看服务器当前版本' }))
    await screen.findByText(/服务器当前版本 3/)
    fireEvent.click(screen.getByRole('button', { name: '保留草稿并重新编辑' }))
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
    ).toHaveValue('本地保留')
    saveHandler = undefined
    await saved()
    expect(requests[1].expected_revision).toBe(3)
  })
  it('distinguishes idempotency conflicts without automatically retrying', async () => {
    saveHandler = () => json({ detail: { code: 'annotation_idempotency_conflict' } }, 409)
    await open()
    choose()
    fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
    await screen.findByText(/幂等键冲突：/)
    expect(requests).toHaveLength(1)
  })
  it.each([503, 504, 401, 403, 404, 422])(
    'keeps draft and the exact request on HTTP %s',
    async (status) => {
      saveHandler = () => json({ detail: { code: 'test_error' } }, status)
      await open()
      choose('应该回复', '保留原稿')
      fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
      await screen.findByRole('alert')
      expect(
        within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
      ).toHaveValue('保留原稿')
      expect(screen.queryByText(/保存已确认/)).not.toBeInTheDocument()
      saveHandler = undefined
      fireEvent.click(screen.getByRole('button', { name: '以原请求同键重试' }))
      await screen.findByText(/保存已确认/)
      expect(requests[0]).toEqual(requests[1])
    }
  )
  it('retries a timed out transport with the same immutable payload', async () => {
    saveHandler = () => {
      throw new DOMException('timeout', 'TimeoutError')
    }
    await open()
    choose()
    fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
    await screen.findByText(/网络响应异常或超时/)
    saveHandler = undefined
    fireEvent.click(screen.getByRole('button', { name: '以原请求同键重试' }))
    await screen.findByText(/保存已确认/)
    expect(requests[0]).toEqual(requests[1])
  })
  it('handles committed but lost HTTP response through original-key replay', async () => {
    saveHandler = (body) => {
      versions.set('e1', revision(1, body.label))
      throw new Error('response lost after commit')
    }
    await open()
    choose()
    fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
    await screen.findByText(/保存结果尚未确认/)
    saveHandler = () =>
      json({
        persisted: true,
        annotation: versions.get('e1'),
        current_revision: 1,
        idempotent_replay: true,
      })
    fireEvent.click(screen.getByRole('button', { name: '以原请求同键重试' }))
    await screen.findByText(/原请求幂等重放/)
    expect(requests[0]).toEqual(requests[1])
    expect(versions.get('e1')?.revision).toBe(1)
  })
  it('isolates a saving target while switching to a different evaluation', async () => {
    const pending = defer<Response>()
    saveHandler = () => pending.promise
    await open()
    choose('应该回复', '保存第一项')
    fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
    await waitFor(() => expect(requests).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: '批改 e2' }))
    await screen.findByText(/Evaluation e2/)
    choose('不应该回复', '第二项草稿')
    versions.set('e1', revision())
    await act(async () =>
      pending.resolve(
        json({
          persisted: true,
          annotation: revision(),
          current_revision: 1,
          idempotent_replay: false,
        })
      )
    )
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
    ).toHaveValue('第二项草稿')
    expect(screen.getByRole('radio', { name: '不应该回复' })).toBeChecked()
    expect(requests).toHaveLength(1)
  })
  it('protects unsaved drafts when a target change is declined', async () => {
    await open()
    choose('应该回复', '草稿')
    vi.mocked(window.confirm).mockReturnValue(false)
    fireEvent.click(screen.getByRole('button', { name: '批改 e2' }))
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
    ).toHaveValue('草稿')
    expect(screen.queryByText(/Evaluation e2/)).not.toBeInTheDocument()
  })
  it('refreshes the target list without erasing the active draft', async () => {
    await open()
    choose('应该回复', '草稿')
    fireEvent.click(screen.getByRole('button', { name: /刷新待批改列表/ }))
    await screen.findByRole('button', { name: '批改 e1' })
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
    ).toHaveValue('草稿')
  })
  it('blocks page navigation while saving and prompts for an unsaved draft', async () => {
    await open()
    choose()
    await waitFor(() => expect(router.blocker).toHaveBeenCalled())
    vi.mocked(window.confirm).mockReturnValue(false)
    const blocker = router.blocker.mock.lastCall?.[0]
    expect(blocker.shouldBlockFn()).toBe(true)
    expect(blocker.enableBeforeUnload()).toBe(true)
  })
  it('shows no-database empty state without enabling collection', async () => {
    rows = []
    render(<DecisionAnnotations onClose={vi.fn()} />)
    await screen.findByText(/数据库尚未创建/)
    expect(screen.queryByRole('button', { name: /开启/ })).not.toBeInTheDocument()
  })
  it('does not permit raising persisted evidence quality', async () => {
    const old = revision()
    old.context_quality = 'partial_context'
    old.evidence.derived_context_quality = 'partial_context'
    versions.set('e1', old)
    render(<DecisionAnnotations onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: '批改 e1' }))
    const select = await screen.findByLabelText(/主动降低证据质量/)
    await waitFor(() => expect(select).toHaveValue('partial_context'))
    expect(within(select).getByRole('option', { name: /身份和当前正文可用/ })).toBeDisabled()
  })
  it('starts a new request identity only after a real edit', async () => {
    saveHandler = () => json({}, 503)
    await open()
    choose()
    fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
    await screen.findByRole('alert')
    choose('不应该回复')
    saveHandler = undefined
    await saved()
    expect(requests[0].idempotency_key).not.toBe(requests[1].idempotency_key)
  })
  it('does not roll current revision back to an older idempotent result', async () => {
    await open()
    choose()
    saveHandler = () => {
      versions.set('e1', revision(2, 'uncertain'))
      return json({
        persisted: true,
        annotation: revision(1),
        current_revision: 2,
        idempotent_replay: true,
      })
    }
    await saved()
    expect(screen.getByText(/当前状态：人工不确定 · 当前版本 2/)).toBeInTheDocument()
  })
  it('isolates stale current/context responses for fast target switches', async () => {
    const pending = defer<Response>()
    currentHandler = (id) => (id === 'e1' ? pending.promise : json(current(id)))
    render(<DecisionAnnotations onClose={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: '批改 e1' }))
    fireEvent.click(screen.getByRole('button', { name: '批改 e2' }))
    await screen.findByText(/Evaluation e2/)
    await act(async () => pending.resolve(json(current('e1', revision(7)))))
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByText(
        /当前状态：未批改 · 当前版本 0/
      )
    ).toBeInTheDocument()
  })
  it('uses the same authorized backend endpoints and no additional request fields', async () => {
    const target = row()
    await annotations.current(target)
    expect(fetchMock).toHaveBeenCalledWith(
      `${annotationBase}/annotations/o1?target_type=evaluation&evaluation_id=e1`,
      expect.objectContaining({ credentials: 'include', cache: 'no-store' })
    )
  })
})
