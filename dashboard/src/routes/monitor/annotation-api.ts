import { ApiError, authApi } from '@/lib/http'

export const annotationBase = '/api/webui/decision-observation'
export type Label = 'should_reply' | 'should_not_reply' | 'uncertain'
export type Quality = 'identity_context_available' | 'partial_context' | 'critical_context_missing'
export type Target = {
  opportunity_id: string
  target_type: 'evaluation' | 'opportunity'
  evaluation_id: string | null
}
export type TargetRow = Target & {
  session_id: string
  source_type: string
  context_boundary_at: number
  evaluation_sequence: number | null
  current_revision: number
  label: Label | null
  annotation_state: 'unannotated' | 'annotated' | 'uncertain'
}
export type TargetMetadata = Target & {
  session_id: string
  evaluation_sequence: number | null
  feature_version: string | null
  feature_snapshot_hash: string | null
  evaluation_facts_hash: string | null
  message_event_ids: string[]
  membership_available: boolean
  context_boundary_at: number
}
export type Revision = {
  revision: number
  label: Label
  reason: string | null
  actor_id: string
  revision_created_at: number
  context_quality: Quality
  declared_context_quality: Quality
  evidence: {
    derived_context_quality: Quality
    target: TargetMetadata
    context_view_receipt: string
  }
}
export type Current = {
  target: TargetMetadata
  annotation: Revision | null
  current_revision: number
  annotation_state: TargetRow['annotation_state']
  contract_version: 'annotation-v1'
}
export type SaveRequest = {
  target_type: Target['target_type']
  evaluation_id: string | null
  label: Label
  reason: string | null
  context_quality: Quality
  expected_revision: number
  idempotency_key: string
  context_mode: 'decision_context_v1'
}
export type SaveResult = {
  persisted: boolean
  annotation: Revision
  current_revision: number
  idempotent_replay: boolean
}
export type Page<T> = { items: T[]; total: number; page: number; page_size: number }
export type ReviewContext = {
  items: {
    message_event_id: string
    membership: string
    status: string
    name?: string
    text?: string
    text_truncated?: boolean
    event_timestamp: number
  }[]
  total: number
  membership_available: boolean
  expected_associated_count?: number
  unavailable_message_event_ids?: string[]
  context_status: string
  limitations: string[]
}
export const labelNames: Record<Label, string> = {
  should_reply: '应该回复',
  should_not_reply: '不应该回复',
  uncertain: '无法判断',
}
export const stateNames = { unannotated: '未批改', annotated: '已批改', uncertain: '人工不确定' }
export const qualityNames: Record<Quality, string> = {
  identity_context_available: '身份和当前正文可用（不代表完整 Prompt）',
  partial_context: '部分上下文缺失 / 机会关联',
  critical_context_missing: '关键上下文不可恢复',
}
export const qualities = Object.keys(qualityNames) as Quality[]
export const targetKey = (target: Target) =>
  JSON.stringify([target.opportunity_id, target.target_type, target.evaluation_id])
const path = (target: Target) =>
  `${annotationBase}/annotations/${encodeURIComponent(target.opportunity_id)}`
const query = (target: Target) => ({
  target_type: target.target_type,
  evaluation_id: target.evaluation_id,
})

// The existing cookie client without automatic 401 navigation keeps an uncertain
// submission in memory so login expiry cannot silently discard its idempotency key.
export const annotations = {
  targets: (params: {
    page: number
    annotation_state: string
    start: number
    end: number
    session_id: string
  }) =>
    authApi.get<Page<TargetRow>>(`${annotationBase}/annotation-targets`, {
      query: { ...params, page_size: 25 },
    }),
  current: (target: Target) =>
    authApi.get<Current>(path(target), { query: query(target), cache: 'no-store' }),
  history: (target: Target, page: number) =>
    authApi.get<Page<Revision>>(`${path(target)}/history`, {
      query: { ...query(target), page, page_size: 10 },
      cache: 'no-store',
    }),
  context: (target: Target, page: number) =>
    authApi.get<ReviewContext>(
      `${annotationBase}/opportunities/${encodeURIComponent(target.opportunity_id)}/context`,
      { query: { evaluation_id: target.evaluation_id, page, page_size: 25 }, cache: 'no-store' }
    ),
  save: (target: Target, body: SaveRequest) =>
    authApi.put<SaveResult>(path(target), { body, signal: AbortSignal.timeout(30000) }),
}

export function saveError(error: unknown): {
  message: string
  conflict: boolean
  unknown: boolean
} {
  const status = error instanceof ApiError ? error.status : undefined
  const detail =
    error instanceof ApiError
      ? (error.detail as { detail?: { code?: string } } | undefined)
      : undefined
  const code = detail?.detail?.code
  if (status === 409)
    return {
      message:
        code === 'annotation_idempotency_conflict'
          ? '幂等键冲突：该请求身份已被其他内容使用。请查看当前版本，再明确开始新一轮编辑。'
          : '保存冲突：服务器版本、目标证据或授权已变化。草稿保留；请查看当前版本，再明确重新编辑。',
      conflict: true,
      unknown: false,
    }
  const messages: Record<number, string> = {
    401: '登录已过期。草稿和原请求已保留，请在另一标签页重新登录后重试。',
    403: '没有操作权限。草稿已保留，请确认管理员权限。',
    404: '目标已删除、撤销授权或数据库不存在。草稿已保留，不能继续假定该目标可用。',
    422: '输入或目标身份不合法，请检查理由长度及目标。草稿已保留。',
    503: '数据库忙或不可写；保存结果尚未确认。请使用原请求同键重试。',
  }
  return {
    message:
      messages[status ?? 0] ??
      '网络响应异常或超时；保存结果尚未确认。原请求和幂等键已保留，请同键重试。',
    conflict: false,
    unknown: status === undefined || status === 503 || status >= 500 || status < 300,
  }
}
