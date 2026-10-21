# Decision Observation · Phase 2A

日期：2026-10-20。开发基线：`feature/qq-agent` / `v0.4.0.1` /
`15be307438a964d47bdfc1b0b2801f66ed44f78a`。

本次只开发基础静默采集与普通门控观察。没有部署、启用生产采集、修改实际配置、重启服务、发送 QQ 消息、调用模型 API、提交或推送。

## 配置与启停

配置定义位于 `src/config/official_configs.py::DecisionObservationConfig`，注册在
`src/config/config.py::Config.decision_observation`。配置定义版本为 `8.14.81`。
本仓库由 ConfigBase 定义生成配置模板，本次没有修改实际 `config/bot_config.toml`，没有新增 ConfigUpgradeHook。

```toml
[decision_observation]
enabled = false
authorized_session_ids = []
retention_days = 30
queue_capacity = 2048
```

- 默认关闭、白名单为空；关闭时既不采集，也不创建观察数据库。
- 白名单填写 **chat_manager 已注册的真实 QQ 群 ChatSession.session_id**。群号、群名、自行计算的 hash 均不是替代品。
- 即使把私聊 session_id 放入白名单，也不能采集私聊；非 QQ 平台同样排除。
- `recorder.start()` 在插件启动前安装策略及配置重载回调。启停和白名单变更可通过现有配置重载独立生效，与 Decision Replay 开关无关。
- 策略切换先使旧 generation 失效；控制操作返回前等待在途事务退出。旧事件即使在重新启用后才出队，也不能恢复写入。
- `ObservationStore.close()` 是进程关停时的终止操作；运行中独立启停使用策略总开关，不反复关闭同一 writer 实例。
- 队列逻辑上限可以热调整；已有物理队列不在线更换。增大后的有效容量受旧物理容量限制，可从 health 查看两者。之后正常启动会按新容量构建队列。

## 数据契约与存储

正常启用后的数据库位置为 `data/decision_observation.db`，已由现有 `data/` Git 忽略规则覆盖。
本次只在临时目录生成模拟数据库，未创建生产位置的数据库。

数据库使用 SQLite/WAL、独立单写线程、显式外键与级联删除。`PRAGMA user_version=1`；不认识的版本拒绝写入并记录健康错误，不擅自升级已有数据。
初始化 DDL 在事务内完成。不修改 `MaiBot.db` 或 `decision_replay.db` 的结构。

| 表 | 身份及用途 |
|---|---|
| `message_events` | 稳定 message_event_id；session + platform_message_id 唯一；发送者、平台事件时间、接收时间、run_id、source_type、schema_version |
| `opportunities` | UUID opportunity_id；真实 session、普通 pending 来源、创建/结束时间、状态、run_id、policy_version |
| `opportunity_messages` | 机会与消息的多对多关联；复合主键含 role，2A 写入 `pending` |
| `evaluations` | 稳定 evaluation_id；机会 + sequence 唯一；实际评估时间、特征 JSON、概率、阈值、结果、原因和权重 hash |
| `outcomes` | 门控摘要；Planner、Replyer、发送均为 `not_observed`；replay_turn_id=null，关联状态为 `phase_2b_not_connected` |

消息身份使用 session 和平台 ID，避免同内容消息被合并。平台身份沿用当前会话的账号/作用域隔离，不自行重新计算 session_id。
消息重投保持同一身份；评估重放相同 ID/sequence 不新增行；旧评估重放不会覆盖较新门控摘要。

快照含：

- `feature_version=reply-likelihood-v1` 与严格字段/标量检查。
- 当前实际用于评分的九项特征，实际批次 message_event_ids。
- 实际概率、最终概率阈值、静态/窗口阈值来源、窗口预算与特征贡献。
- 临界等待原始 budget 判定、延迟信息和最终放行结果，避免把临界放行误报为原门槛通过。
- 本地权重参数 SHA-256；不修改、拟合或重载权重。
- 未计算字段为 null，另有 `not_calculated` 状态。frequency 分支的消息数阈值单列记录，不冒充概率阈值。

不保存消息正文、完整 Prompt、API Key、模型请求或任意 additional_config。
如果消息最初未在接收边界登记，但后来出现在 pending 中，记录为 `pending_observed`，received_timestamp=null，不伪造接收时间。

索引覆盖：session + 消息/机会时间、发送者 + 时间、机会状态 + 时间、评估结果 + 时间、消息反向关联，以及唯一约束对应索引。

## 采集入口与机会生命周期

1. `HeartFCMessageReceiver.process_message()` 进入时捕获接收时间；解析到真实 Runtime 后、独立 @ claim 前登记消息身份。
2. `MaisakaHeartFlowChatting.register_message()` 把普通消息加入 message_cache 后调用 `registered()`。
3. Runtime 自有 `PendingWorkset` 管理机会。pending 开始建立一个机会；新增消息扩展关联，多条消息不机械生成多个机会。
4. Scheduler 各已有判定分支记录事实。dynamic 分支只读取生产 evaluate 返回的值，不增加一次 evaluate。
5. 同一 pending 工作集的后续检查递增 evaluation_sequence，保留每次评估，不使用 Replay 的判定签名去重。
6. `_collect_pending_messages()` 消费 pending 时标记 `consumed`；它不表示 Planner 执行或平台发送。
7. Runtime 停止、策略切换和观察正常关闭标记未完成机会 interrupted；进程异常退出的旧 run pending 在下次初始化时标记 interrupted。

快照在同步门控返回后立即序列化，中间不 await、不查询稍后的聊天历史。修改后来的消息对象不会修改已入队快照。
消息登记与机会创建彼此独立；独立 @ 消息可以有接收台账，但不宣称有完整机会/执行关联。

## 写入、故障、删除与查询

主链仅复制允许字段、构造不可变批次并 `put_nowait()`。工作线程每批最多取 64 个批次，SQLite 等待上限 0.1 秒。
每个评估批次携带必要的父机会和消息记录：若单独创建批次丢失，后续批次仍可完整落库。丢失评估不会被补造，sequence 可以出现缺口。

- 写入异常：当前批次丢弃并计数，不重试生产行为、不调用模型。
- 队列满：直接丢弃并计数，不等待空位。异常日志按模块最多每 60 秒一次，不包含异常原文或聊天载荷。
- 单次工作集最多 512 条消息，超出时明确降级计数，不截断后冒充完整数据。
- `health()` 提供启用/关闭、run_id、队列长度/容量、提交和提交成功批次数、丢弃/撤销批次数、总错误/写错误、最近错误码、worker 和初始化状态。
- 上述计数是 **批次和当前进程计数**，不是消息/机会覆盖率；硬崩溃尾部损失未知，未实现持久健康台账。
- 正常采集没有逐条 Observation 日志。原 Scheduler/Replay 日志未清理。
- 关闭默认最多排空约 3 秒；返回布尔值明确是否排空/线程退出，超时不伪报成功。未完成事件失效，writer 不再提交迟到采集数据。

基础控制接口如下，均没有 Dashboard/API 暴露：

```python
# 在已有进程的控制面调用；阻塞控制操作放入线程。
await asyncio.to_thread(recorder.store.delete_session, real_session_id)
health = recorder.store.health()
page = await asyncio.to_thread(recorder.store.query, real_session_id, 100)
```

`query(session_id, limit)` 使用只读连接，不创建数据库、不迁移、不清理，limit 限制为 1–200。
每个机会的 evaluation 列表也有上限，2A 不提供完整分页/导出接口。

`delete_session()` 先撤销该 session 的排队 generation，再事务删除全部关联观察数据。
它同时把该会话加入本 writer 生命周期的禁止采集集合，避免活跃 Runtime 中的旧 pending 在下一次检查重新写回。
要恢复该群采集需新的 store/run；不会自动重启机器人。
不要另建一个离线 store 冒充正在运行的 writer 执行删除；它无法撤销另一实例中的队列。离线维护应先停止采集并确认 writer 已关闭。

保留清理由后台约每小时执行：按机会创建时间删除过期机会及关联评估/结果，再删除过期且无引用的消息。
关闭采集后不继续后台清理/写入；显式删除控制操作仍可使用。
本阶段没有按用户/时间范围删除、备份擦除、训练导出或复杂授权 UI。

## 验证与证据

测试使用本地 `.venv` 的现有依赖，未安装或同步依赖。源码、测试、配置副本放在
`temp/decision-observation-2a-66312958/`，通过二进制复制和字节比对保真。
配置自动升级、主库初始化、回放写入及子进程测试均发生在该副本/pytest 临时目录。

实际命令（工作目录为上述隔离副本）：

```powershell
$env:PYTHONUTF8='1'
uv run --no-project --no-sync --python 'G:\QQbot\MaiBot\.venv\Scripts\python.exe' python -m pytest `
  pytests/decision_observation `
  pytests/maisaka/test_message_turn_scheduler.py `
  pytests/maisaka/test_dynamic_reply_gate.py `
  pytests/maisaka/test_reply_likelihood_weights.py `
  pytests/maisaka/test_pending_observation.py `
  pytests/maisaka/test_planner_lifecycle.py `
  pytests/maisaka/test_priority_at_admission.py `
  pytests/maisaka/test_priority_at_reply.py `
  pytests/maisaka/test_priority_at_concurrency.py `
  pytests/maisaka/test_wait_turn_lifecycle.py `
  pytests/test_runtime_shutdown.py pytests/decision_replay -q -s --tb=short
```

新增专项包含关闭零写入、授权/私聊排除、真实门控通过与拒绝、多消息工作集、多次评估、真实临界延迟、跨群隔离、幂等、消费/中断、策略重载、撤销及在途事务回滚、删除后旧 pending 屏障、锁/写异常/满队列、有限关停、快照不被未来消息污染、日志静默和严格字段检查。

行为对照使用相同模拟输入，执行真实 Scheduler、动态门控、pending 消费及 Planner 响应动作处理器；模型与工具执行边界为 AsyncMock。检查：

- 门控 evaluate 恰好一次、实际特征输入一致。
- 窗口评分、回复窗口、已计消息集合及 Runtime 游标/状态一致。
- Planner、Replyer、发送调用及其参数一致；通过样本各一次，拒绝样本均零次。
- writer 抛错、观察适配器抛错时仍一致。
- 无 QQ 连接和收费模型调用。测试不能证明在线模型或真实平台行为。

最终测试结果、测量数据和样例见本文末尾的交付记录；模拟数据库查询结果另存
`decision-observation-phase2a-example.json`，无需依赖实际群聊数据。

## 修改清单与 Phase 2B 边界

新增：

- `src/decision_observation/{__init__,models,store,recorder}.py`
- `pytests/decision_observation/test_observation.py`
- 本文与模拟观察记录 JSON。

修改：

- `src/chat/heart_flow/heartflow_message_processor.py`：接收时间与消息登记。
- `src/maisaka/runtime.py`：pending 机会、消费、focus 拒绝与中断。
- `src/maisaka/turn_trigger/scheduler.py`：已有普通分支的结果采集。
- `src/maisaka/turn_trigger/gates.py`：暴露实际评分时间和权重 hash；不更改公式、权重、阈值或 evaluate 次数。
- `src/config/{official_configs,config}.py`：默认关闭的配置定义与版本。
- `src/main.py`、`src/common/runtime_shutdown.py`：启动/重载/关闭接入。
- `changelogs/changelog.md`：本阶段功能说明。

已覆盖：授权 QQ 群接收台账、普通 pending、动态通过/拒绝、临界延迟复查、Scheduler 的等待/退避/静默/focus 等分支、frequency 分支基本结果、机会消费和观察中断。

未完整覆盖：

- 主链之前的适配器过滤、主人管理/插件命令早期消费；真实 Runtime 创建失败时也无法登记。
- 独立 @ 的排队、执行、Replyer 与发送关联；forced 分支仅标记 `forced_trigger_out_of_scope`。
- 插件主动任务、wait 超时的完整续轮、focus 切换、复读和静默表情机会。
- Planner/Replyer/平台执行关联、人工标签、上下文正文、完整覆盖率、Dashboard、训练和导出。

Phase 2B 建议：显式携带 opportunity/evaluation ID 进入最终消费批次、逻辑 turn/cycle/reply/outbound 链，再接独立 @ 的准入及执行。禁止按时间接近猜关联。特殊触发另立 source_type，不混入普通门控样本。

已知限制：SQLite 同步工作仅在 writer/控制线程；快照复制和序列化仍占少量主循环时间。健康计数进程内保存，队列内存数据遇硬退出会丢失。采集关闭后已存在数据不会自动删除；按群删除会持续停止该 writer 对该群采集。没有部署，也没有进入 Phase 2B。

## 最终交付记录

- 最终扩展回归：**196 passed，19 subtests passed，22 warnings，17.08 秒**。
- 随后对样例测试统一模拟时钟并增加事件/接收/决策时间顺序断言；单独执行 `pytests/decision_observation/test_observation.py::test_payload_example_and_admission_benchmark -q -s --tb=short`，**1 passed，2.23 秒**。交付 JSON 来自这次 SQLite 读回；生产代码未因此变化。
- 其中新增观察专项 32 项。警告来自既有 Pydantic、SWIG 和 FastAPI/Starlette 依赖弃用提示。
- 本次改动的 Python 文件 Ruff check 通过；新增模块和测试 Ruff format 通过；git diff --check 通过。
- 最终测试副本与当前 13 个相关 Python 文件再次逐字节比对一致。
- 实际 bot/model 配置 SHA-256 与开发前记录一致；生产 `data/decision_observation.db` 不存在。
- 现有用户未提交修改及适配器独立仓库保持原状。未 commit/push。

性能初测为本机合成数据、单消息工作集，不代表真实多群压测，也不是零开销承诺：

| 测量 | 次数 | p50 | p95 | p99 | 最大 |
|---|---:|---:|---:|---:|---:|
| 仅有界队列入队 | 2000 | 0.9 μs | 1.0 μs | 1.1 μs | 13.1 μs |
| 观察适配器关闭 | 1000 | 0.2 μs | 0.2 μs | 0.4 μs | 21.7 μs |
| 观察适配器开启，含快照序列化与入队 | 1000 | 23.6 μs | 52.6 μs | 115.9 μs | 780.2 μs |

另有事件循环测试：阻塞 writer 后，主循环继续完成 10 次定时让出和观察提交；数据库锁定时，生产侧不等待数据库超时。
这些测试不覆盖在线 QQ、真实模型、生产吞吐、长期磁盘增长或硬断电损失。

模拟记录摘录（完整数据来自实际临时 SQLite 查询，不是手填的执行结果）：

```json
{
  "source_type": "ordinary_pending",
  "opportunity_status": "consumed",
  "evaluation_sequence": 1,
  "calculated_probability": 0.8017852886012584,
  "effective_threshold": 0.336,
  "gate_result": "pass",
  "decision_reason": "dynamic_trigger",
  "planner_status": "not_observed",
  "replyer_status": "not_observed",
  "send_status": "not_observed",
  "replay_turn_id": null
}
```

完整样例：[decision-observation-phase2a-example.json](decision-observation-phase2a-example.json)。
测试原始输出位于隔离副本的 `phase2a-regression-final.txt`，包含 Mock 故障注入日志；不应把这些故障视为生产故障。
