import { useEffect, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { authApi } from '@/lib/http'
import { annotationBase, saveError } from './annotation-api'
import type { Target } from './annotation-api'

type Detail = {
  opportunity: { session_id: string }
  groups: Record<
    string,
    { items: Record<string, unknown>[]; total: number | null; available: boolean }
  >
  replay: { logical_turn_id: string; status: string }[]
  limitations: string[]
  replay_total?: number
  replay_truncated?: boolean
}
const groupNames: Record<string, string> = {
  evaluations: '回复判断与冻结特征',
  execution_events: '排队、消费与规划执行',
  reply_events: '回复生成与发送尝试',
  delivery_receipts: '已关联平台回执',
  attempt_states: '发送尝试结论',
  session_unlinked_receipts: '同群未关联回执（不属于当前判断）',
  message_events: '消息关联证据',
  transfers: '路径转移关系',
}

export function AnnotationMachineDetails({
  target,
  text,
}: {
  target: Target
  text: (value: string) => string
}) {
  const [data, setData] = useState<Detail | null>(null)
  const [page, setPage] = useState(1)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    authApi
      .get<Detail>(`${annotationBase}/opportunities/${encodeURIComponent(target.opportunity_id)}`, {
        query: { page },
        cache: 'no-store',
      })
      .then((value) => {
        if (live) {
          setData(value)
          setError('')
        }
      })
      .catch((e) => {
        if (live) setError(saveError(e).message)
      })
    return () => {
      live = false
    }
  }, [target, page])
  const total = Math.max(0, ...Object.values(data?.groups ?? {}).map((g) => g.total ?? 0))
  return (
    <div className="mt-4 space-y-4 text-sm">
      <p>以下为整个回复机会的记录，可能包含其他轮次；仅明确关联当前判断的事实可归属于本轮。</p>
      <p className="text-muted-foreground">
        显示层替换本群聊天库已知的昵称、群名片和账号。正文中未知人物、未入库的别名无法可靠识别；匿名化不等于不可重新识别。
      </p>
      {error && <p role="alert">{error}</p>}
      {!data && !error && <p>读取决策细节…</p>}
      {data && (
        <>
          <pre className="bg-muted overflow-auto rounded p-3 text-xs whitespace-pre-wrap">
            {text(JSON.stringify(data.opportunity, null, 2))}
          </pre>
          {Object.entries(data.groups).map(([name, group]) => (
            <section key={name} className="space-y-2 border-t pt-3">
              <h4 className="font-medium">
                {groupNames[name] ?? '其他记录'} · {group.total ?? '未接入'}
              </h4>
              {group.items.map((fact, i) => (
                <div key={i}>
                  {fact.evaluation_id != null && (
                    <p>
                      {fact.evaluation_id === target.evaluation_id
                        ? '当前回复判断'
                        : '其他回复判断'}
                    </p>
                  )}
                  <pre className="bg-muted max-h-96 overflow-auto rounded p-3 text-xs whitespace-pre-wrap">
                    {text(JSON.stringify(fact, null, 2))}
                  </pre>
                </div>
              ))}
              {!group.items.length && (
                <p>{group.available ? '本页无记录，不能据此推断未执行。' : '此历史版本未接入。'}</p>
              )}
            </section>
          ))}
          {data.limitations.map((value) => (
            <p key={value}>{text(value)}</p>
          ))}
          {data.replay.map((trace, i) => (
            <p key={trace.logical_turn_id}>
              机会回放 {i + 1} ·{' '}
              {trace.status === 'available' ? (
                <Link
                  to="/decision-replay"
                  search={{
                    observationTurn: trace.logical_turn_id,
                    observationSession: data.opportunity.session_id,
                  }}
                >
                  打开只读回放（离开批改页）
                </Link>
              ) : (
                '不可用'
              )}
            </p>
          ))}
          {data.replay_truncated && <p>共 {data.replay_total} 条回放，仅展示前 100 条。</p>}
        </>
      )}
      <div className="flex items-center gap-3">
        <Button variant="outline" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
          细节上一页
        </Button>
        <span>第 {page} 页 · 各组独立分页</span>
        <Button
          variant="outline"
          disabled={!data || page * 50 >= total}
          onClick={() => setPage((p) => p + 1)}
        >
          细节下一页
        </Button>
      </div>
    </div>
  )
}
