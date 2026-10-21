import { useCallback, useEffect, useRef, useState } from 'react'
import { useBlocker } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { authApi } from '@/lib/http'
import { annotations, annotationBase, saveError, stateNames, targetKey } from './annotation-api'
import type { Page, Target, TargetRow } from './annotation-api'
import { AnnotationEditor } from './decision-annotation-editor'

type State = { dirty: boolean; busy: boolean }
const selectClass = 'bg-background text-foreground w-full rounded-md border p-2 text-sm'

function EditorSlot({
  target,
  active,
  locate,
  update,
  saved,
}: {
  target: Target
  active: boolean
  locate: number
  update: (key: string, state: State) => void
  saved: () => void
}) {
  const onState = useCallback(
    (dirty: boolean, busy: boolean) => update(targetKey(target), { dirty, busy }),
    [target, update]
  )
  return (
    <AnnotationEditor
      target={target}
      active={active}
      locate={locate}
      onState={onState}
      onSaved={saved}
    />
  )
}

export function DecisionAnnotations({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<Page<TargetRow> | null>(null)
  const [sessions, setSessions] = useState<{ session_id: string; name: string }[]>([])
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [filter, setFilter] = useState('unannotated')
  const [session, setSession] = useState('')
  const [hours, setHours] = useState('24')
  const [end, setEnd] = useState(() => Date.now() / 1000)
  const [refresh, setRefresh] = useState(0)
  const [opened, setOpened] = useState<Target[]>([])
  const [active, setActive] = useState('')
  const [locate, setLocate] = useState(0)
  const states = useRef(new Map<string, State>())
  const update = useCallback((key: string, state: State) => {
    states.current.set(key, state)
  }, [])
  const saved = useCallback(() => setRefresh((value) => value + 1), [])
  function leave() {
    if ([...states.current.values()].some((state) => state.busy)) {
      window.alert('保存仍在进行，请等待结果后再离开。')
      return false
    }
    return (
      ![...states.current.values()].some((state) => state.dirty) ||
      window.confirm(
        '有未保存草稿或未确认提交。离开批改页面将丢失内存中的草稿和原请求，是否仍要离开？'
      )
    )
  }
  useBlocker({
    shouldBlockFn: () => !leave(),
    enableBeforeUnload: () =>
      [...states.current.values()].some((state) => state.dirty || state.busy),
  })
  useEffect(() => {
    let live = true
    authApi
      .get<{ sessions: { session_id: string; name: string }[] }>(`${annotationBase}/health`)
      .then((value) => {
        if (live) setSessions(value.sessions)
      })
      .catch((e) => {
        if (live) setError(saveError(e).message)
      })
    return () => {
      live = false
    }
  }, [])
  useEffect(() => {
    let live = true
    setData(null)
    setError('')
    annotations
      .targets({
        page,
        annotation_state: filter,
        start: end - Number(hours) * 3600,
        end,
        session_id: session,
      })
      .then((value) => {
        if (live) setData(value)
      })
      .catch((e) => {
        if (live) setError(saveError(e).message)
      })
    return () => {
      live = false
    }
  }, [page, filter, session, hours, end, refresh])
  function select(target: Target) {
    const key = targetKey(target)
    if (key === active) {
      setLocate((value) => value + 1)
      return
    }
    const previous = states.current.get(active)
    if (
      previous?.dirty &&
      !window.confirm('当前目标有未保存或待确认内容。切换后草稿及原请求仍保留在本页；是否切换？')
    )
      return
    setOpened((values) =>
      values.some((value) => targetKey(value) === key) ? values : [...values, target]
    )
    setActive(key)
    setLocate((value) => value + 1)
  }
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">人工批改工作台</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            按真实决策点保存判断 · 不影响生产发言
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => {
            if (leave()) onClose()
          }}
        >
          返回只读观察
        </Button>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>待批改导航</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <label htmlFor="annotation-state" className="space-y-1 text-sm">
              批改状态
              <select
                id="annotation-state"
                className={selectClass}
                value={filter}
                onChange={(event) => {
                  setPage(1)
                  setFilter(event.target.value)
                }}
              >
                <option value="unannotated">未批改</option>
                <option value="annotated">已批改</option>
                <option value="uncertain">人工不确定</option>
                <option value="">全部</option>
              </select>
            </label>
            <label htmlFor="annotation-session" className="space-y-1 text-sm">
              所属群聊
              <select
                id="annotation-session"
                className={selectClass}
                value={session}
                onChange={(event) => {
                  setPage(1)
                  setSession(event.target.value)
                }}
              >
                <option value="">全部授权群聊</option>
                {sessions.map((item) => (
                  <option key={item.session_id} value={item.session_id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label htmlFor="annotation-time" className="space-y-1 text-sm">
              决策时间范围
              <select
                id="annotation-time"
                className={selectClass}
                value={hours}
                onChange={(event) => {
                  setPage(1)
                  setHours(event.target.value)
                }}
              >
                <option value="24">最近24小时</option>
                <option value="168">最近7天</option>
                <option value="744">最近31天</option>
              </select>
            </label>
          </div>
          <Button
            variant="outline"
            onClick={() => {
              setPage(1)
              setEnd(Date.now() / 1000)
              saved()
            }}
          >
            刷新待批改列表（保留草稿）
          </Button>
          {error && (
            <p role="alert" className="text-destructive">
              {error}
            </p>
          )}
          {!data && !error && <p role="status">读取批改目标…</p>}
          {data && !data.total && (
            <p>
              此范围暂无批改目标。数据库尚未创建、未授权、已清理或当前筛选为空时均可能如此；不会自动启用采集。
            </p>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="text-muted-foreground mb-2 text-left text-xs">
                每次回复判断独立批改；没有单次判断记录时，使用回复机会级补充判断。点击后自动定位到判断范围。
              </caption>
              <thead>
                <tr>
                  {['决策时间', '群聊', '目标', '状态', '版本', '上下文', '操作'].map((text) => (
                    <th className="p-2" key={text}>
                      {text}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data?.items.map((item) => (
                  <tr
                    className={active === targetKey(item) ? 'bg-primary/5 border-t' : 'border-t'}
                    key={targetKey(item)}
                  >
                    <td className="p-2 whitespace-nowrap">
                      {new Date(item.context_boundary_at * 1000).toLocaleString('zh-CN')}
                    </td>
                    <td className="p-2">
                      {sessions.find((value) => value.session_id === item.session_id)?.name ??
                        '群聊名称不可用'}
                    </td>
                    <td className="max-w-56 p-2 break-all">
                      {item.evaluation_id
                        ? `本轮第 ${item.evaluation_sequence ?? '?'} 次回复判断`
                        : '回复机会级补充判断'}
                    </td>
                    <td className="p-2">{stateNames[item.annotation_state]}</td>
                    <td className="p-2">r{item.current_revision}</td>
                    <td className="p-2">尚未核验，打开查看</td>
                    <td className="p-2">
                      <Button
                        variant={active === targetKey(item) ? 'secondary' : 'outline'}
                        aria-label={
                          item.evaluation_id
                            ? `开始批改：本轮第 ${item.evaluation_sequence ?? '?'} 次回复判断`
                            : '开始批改：回复机会级补充判断'
                        }
                        onClick={() => select(item)}
                      >
                        {active === targetKey(item) ? '继续批改' : '开始批改'}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="outline"
              disabled={!data || page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              目标上一页
            </Button>
            <span>
              第 {page} 页 · {data?.total ?? 0} 个目标
            </span>
            <Button
              variant="outline"
              disabled={!data || page * 25 >= data.total}
              onClick={() => setPage((p) => p + 1)}
            >
              目标下一页
            </Button>
          </div>
        </CardContent>
      </Card>
      {opened.map((target) => (
        <div hidden={targetKey(target) !== active} key={targetKey(target)}>
          <EditorSlot
            target={target}
            active={targetKey(target) === active}
            locate={locate}
            update={update}
            saved={saved}
          />
        </div>
      ))}
      {!active && (
        <p className="text-muted-foreground py-6 text-center">
          选择一个真实决策点开始批改。先看判断范围和匿名上下文，机器简版判断直接可见。
        </p>
      )}
    </div>
  )
}
