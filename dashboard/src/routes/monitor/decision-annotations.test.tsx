import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DecisionAnnotations } from './decision-annotations'
import { annotations, annotationBase } from './annotation-api'
import type { Current, Revision, SaveRequest, TargetRow } from './annotation-api'

const router = vi.hoisted(() => ({ blocker: vi.fn() }))
vi.mock('@tanstack/react-router', () => ({ useBlocker: router.blocker }))
vi.mock('./annotation-machine-details', () => ({
  AnnotationMachineDetails: ({
    target,
  }: {
    target: { opportunity_id: string; evaluation_id: string }
  }) => (
    <p>
      真实只读结果 {target.opportunity_id} / {target.evaluation_id}
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
    privacy_replacements: {},
    target_items: [
      {
        message_event_id: 'm1',
        status: 'available',
        text: '合成决策前消息',
        name: '群友 A17',
        event_timestamp: 90,
        membership: 'evaluation_member',
      },
    ],
    machine: {
      gate_result: 'pass',
      decision_reason: 'dynamic_trigger',
      execution_result: 'unknown',
    },
    items: [
      {
        message_event_id: 'm1',
        membership: 'evaluation_member',
        status: 'available',
        name: '群友 A17',
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
    if (url.pathname.endsWith('/review-context')) return json(contexts)
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
  fireEvent.click(
    await screen.findByRole('button', {
      name:
        id === 'o1'
          ? '开始批改：回复机会级补充判断'
          : `开始批改：本轮第 ${id === 'e2' ? 2 : 1} 次回复判断`,
    })
  )
  await screen.findByText(/当前状态：.*当前版本/)
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

describe('C phase semantics without changing human labels', () => {
  it('shows Gate pass and Planner wait independently after B, then saves a contrary human judgment', async () => {
    contexts = {
      ...contexts,
      machine: {
        gate_result: 'pass',
        decision_reason: 'dynamic_trigger',
        execution_result: 'not_observed',
        planner: {
          status: 'observed',
          evidence_status: 'tool_selection',
          rounds: [{ decision: 'wait' }],
          not_started_attempts: 0,
        },
      },
    }
    await open()
    const c = screen.getByRole('heading', { name: 'C · 机器判断' }).parentElement!
    expect(c).toHaveTextContent('门控结果：通过门控，允许进入后续规划')
    expect(c).toHaveTextContent('Planner 决策：决定等待')
    expect(c).toHaveTextContent('门控原因：动态门控允许进入后续回复规划。')
    expect(c).toHaveTextContent('未记录可确认的 Planner 判断原因')
    expect(c).toHaveTextContent('未观察到发送（不代表确定未发送）')
    expect(c).not.toHaveTextContent('应该回复')
    const b = screen.getByText('B · 人工判断')
    expect(b.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getAllByRole('radio')).toHaveLength(3)
    expect(screen.getByRole('radio', { name: '应该回复' })).not.toBeChecked()
    choose('应该回复', '人工认为应当回复，不等同于机器选择')
    await saved()
    expect(requests[0].label).toBe('should_reply')
    expect(requests[0].evaluation_id).toBe('e1')
  })
  it('does not reuse the previous target Planner result when changing evaluations', async () => {
    contexts = {
      ...contexts,
      machine: {
        gate_result: 'pass',
        decision_reason: null,
        execution_result: 'unknown',
        planner: {
          status: 'observed',
          evidence_status: 'tool_selection',
          rounds: [{ decision: 'wait' }],
          not_started_attempts: 0,
        },
      },
    }
    await open()
    expect(await screen.findByText('决定等待（已记录等待工具选择）')).toBeVisible()
    contexts = {
      ...contexts,
      machine: { gate_result: 'reject', decision_reason: null, execution_result: 'unknown' },
    }
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 2 次回复判断' }))
    expect(await screen.findByText('门控拒绝')).toBeVisible()
    const c = screen.getByRole('heading', { name: 'C · 机器判断' }).parentElement!
    expect(c).toHaveTextContent('Planner 决策：无法确认')
    expect(c).not.toHaveTextContent('决定等待')
  })
})

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
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 2 次回复判断' }))
    await waitFor(() =>
      expect(screen.getByRole('region', { name: '人工批改详情' })).toHaveTextContent(
        '本轮第 2 次回复判断'
      )
    )
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
    ).toHaveValue('')
    expect(screen.queryByText(/snapshot-e2/)).not.toBeInTheDocument()
    choose('不应该回复')
    await saved()
    expect(requests[0].evaluation_id).toBe('e2')
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 1 次回复判断' }))
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
    expect(screen.getByText(/缺失关联消息：1 条/)).toBeInTheDocument()
    expect(screen.getByText('重建参考上下文', { exact: false })).toBeInTheDocument()
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
    const summary = screen.getByText('展开完整决策细节')
    expect(summary.closest('details')).not.toHaveAttribute('open')
    expect(screen.queryByText(/真实只读结果/)).not.toBeInTheDocument()
  })
  it('loads existing read-only results on explicit expansion and load', async () => {
    await open()
    fireEvent.click(screen.getByText('展开完整决策细节'))
    expect(await screen.findByText('真实只读结果 o1 / e1')).toBeInTheDocument()
  })
  it('mounts only the active read-only view while preserving independent drafts', async () => {
    await open()
    fireEvent.click(screen.getByText('展开完整决策细节'))
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 2 次回复判断' }))
    await waitFor(() =>
      expect(screen.getByRole('region', { name: '人工批改详情' })).toHaveTextContent(
        '本轮第 2 次回复判断'
      )
    )
    expect(screen.queryByText('真实只读结果 o1 / e1')).not.toBeInTheDocument()
    const active = within(screen.getByRole('region', { name: '人工批改详情' }))
    fireEvent.click(active.getByText('展开完整决策细节'))
    expect(await screen.findByText('真实只读结果 o1 / e2')).toBeInTheDocument()
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
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 2 次回复判断' }))
    await waitFor(() =>
      expect(screen.getByRole('region', { name: '人工批改详情' })).toHaveTextContent(
        '本轮第 2 次回复判断'
      )
    )
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
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 2 次回复判断' }))
    expect(
      within(screen.getByRole('region', { name: '人工批改详情' })).getByLabelText(/理由（可选/)
    ).toHaveValue('草稿')
    expect(screen.getByRole('region', { name: '人工批改详情' })).not.toHaveTextContent(
      '本轮第 2 次回复判断'
    )
  })
  it('refreshes the target list without erasing the active draft', async () => {
    await open()
    choose('应该回复', '草稿')
    fireEvent.click(screen.getByRole('button', { name: /刷新待批改列表/ }))
    await screen.findByRole('button', { name: '开始批改：本轮第 1 次回复判断' })
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
    fireEvent.click(await screen.findByRole('button', { name: '开始批改：本轮第 1 次回复判断' }))
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
    fireEvent.click(await screen.findByRole('button', { name: '开始批改：本轮第 1 次回复判断' }))
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 2 次回复判断' }))
    await waitFor(() =>
      expect(screen.getByRole('region', { name: '人工批改详情' })).toHaveTextContent(
        '本轮第 2 次回复判断'
      )
    )
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

describe('V3 judgment workflow', () => {
  it('highlights the exact snapshot candidate, not the last message, inside one container', async () => {
    await open()
    await screen.findByText(/^目标消息 ·/)
    const region = screen.getByRole('region', { name: '人工批改详情' })
    const marked = region.querySelectorAll('[data-review-target]')
    expect(marked).toHaveLength(1)
    expect(marked[0]).toHaveTextContent('合成决策前消息')
    expect(marked[0]).not.toHaveTextContent('合成参考消息')
    expect(screen.getAllByText(/群友 A17/)).toHaveLength(2)
    const card = within(region).getByText('A · 决策前上下文').closest('[data-dashboard-card]')
    expect(card).toContainElement(within(region).getByText('B · 人工判断'))
    expect(card).toContainElement(within(region).getByText('C · 机器判断'))
    expect(screen.queryByText('决策点身份')).not.toBeInTheDocument()
    expect(region).not.toHaveTextContent('snapshot-e1')
    expect(screen.queryByText(/Evaluation/)).not.toBeInTheDocument()
  })
  it('displays and highlights all candidates as a range without inventing a unique trigger', async () => {
    currentHandler = (id) =>
      json({ ...current(id), target: { ...metadata(id), message_event_ids: ['m1', 'm2'] } })
    contexts = {
      ...contexts,
      target_items: (contexts.items as { membership: string }[]).map((item) => ({
        ...item,
        membership: 'evaluation_member',
      })),
    }
    await open()
    expect(await screen.findByText(/本次判断范围包含 2 条候选消息/)).toBeInTheDocument()
    expect(
      screen.getByRole('region', { name: '人工批改详情' }).querySelectorAll('[data-review-target]')
    ).toHaveLength(2)
    expect(screen.queryByText('目标消息')).not.toBeInTheDocument()
  })
  it('does not highlight incomplete or ambiguous candidates', async () => {
    contexts = {
      ...contexts,
      target_items: [{ message_event_id: 'm1', status: 'missing_or_ambiguous' }],
    }
    await open()
    expect(await screen.findByText(/目标消息无法唯一确认/)).toBeInTheDocument()
    expect(
      screen.getByRole('region', { name: '人工批改详情' }).querySelectorAll('[data-review-target]')
    ).toHaveLength(0)
  })
  it('shows the target preview even when the current context page contains only references', async () => {
    contexts = {
      ...contexts,
      items: [
        {
          message_event_id: 'm2',
          membership: 'reference_only',
          status: 'available',
          text: '只有参考',
          event_timestamp: 80,
        },
      ],
    }
    await open()
    expect(await screen.findByText('合成决策前消息')).toBeInTheDocument()
    expect(screen.queryByText('目标消息')).not.toBeInTheDocument()
  })
  it('displays a traceable gate summary by default and keeps full records collapsed', async () => {
    await open()
    expect(await screen.findByText(/门控原因：动态门控允许进入后续回复规划。/)).toBeVisible()
    expect(screen.getByText('结果未知（未确认实际发送结果）')).toBeVisible()
    const details = screen.getByText('展开完整决策细节').closest('details')!
    expect(details.open).toBe(false)
    fireEvent.click(screen.getByText('展开完整决策细节'))
    expect(await screen.findByText('真实只读结果 o1 / e1')).toBeInTheDocument()
    fireEvent.click(screen.getByText('展开完整决策细节'))
    await waitFor(() => expect(screen.queryByText('真实只读结果 o1 / e1')).not.toBeInTheDocument())
  })
  it('degrades missing machine evidence without claiming a negative send result', async () => {
    contexts = { ...contexts, machine: undefined }
    await open()
    expect(screen.getByText('门控结果未知')).toBeVisible()
    expect(screen.getByText('门控原因：未记录具体门控原因')).toBeVisible()
    expect(screen.getByText('结果未知（未确认实际发送结果）')).toBeVisible()
  })
  it('anonymizes persisted reasons, version history, and advanced metadata without rewriting a label-only save', async () => {
    const saved = { ...revision(), reason: '小明应该得到回复', actor_id: '小明' }
    versions.set('e1', saved)
    contexts = {
      ...contexts,
      privacy_replacements: { 小明: '群友 A17' },
      machine: { gate_result: 'pass', decision_reason: '小明被提及', execution_result: 'unknown' },
    }
    await open()
    expect(screen.getByRole('textbox')).toHaveValue('群友 A17应该得到回复')
    expect(screen.getAllByText('群友 A17应该得到回复')).toHaveLength(2)
    fireEvent.click(screen.getByText('展开完整决策细节'))
    await screen.findByText('真实只读结果 o1 / e1')
    expect(screen.getByRole('region', { name: '人工批改详情' })).not.toHaveTextContent('小明')
    choose('不应该回复')
    fireEvent.click(screen.getByRole('button', { name: '保存判断' }))
    await screen.findByText(/保存已确认/)
    expect(requests[0].reason).toBe('小明应该得到回复')
  })
  it('scrolls to A after loading and when returning to a retained draft', async () => {
    const scroll = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value: scroll,
    })
    await open()
    await waitFor(() => expect(scroll).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' }))
    expect(document.activeElement).toHaveAttribute('aria-label', '决策前上下文定位')
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 2 次回复判断' }))
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: '开始批改：本轮第 1 次回复判断' }))
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(3))
  })
})
