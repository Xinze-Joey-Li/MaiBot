# QQ-Agent 决策回放 / Decision Replay

本功能只观察、持久化和展示真实执行路径，不重新执行模型、工具或消息发送。现有麦麦观察保留。没有新增 LLM 请求，也不把完整 Prompt、聊天正文、API Key 或供应商请求体复制进回放库。

## 使用入口与配置

Dashboard 左侧「概览 → 决策回放」，地址 `/decision-replay`，生产地址通常为 `http://127.0.0.1:8001/decision-replay`。需使用现有 WebUI 登录；API 继续使用 `require_auth`。本次没有启动机器人或真实 QQ。

默认模板新增 `[debug]`：

```toml
decision_replay_enabled = true
decision_replay_retention_days = 7
```

配置结构版本 8.14.77；不新增 ConfigUpgradeHook，不修改 legacy_migration，没有手动修改用户实际 bot_config/model_config 的取值。通过已有配置升级/重载机制使用新字段。关闭 `decision_replay_enabled` 后停止新增记录，原聊天调度和模型调用保持原行为，历史仍可查询。保留时间限定 1–365 天。

前端已构建至本地 dashboard/dist。其他机器拉取源码后仍需在 dashboard 执行 `bun install --frozen-lockfile`、`bun run build`。

## 事件模型与关联

schema_version=1。事件包括 event_id、timestamp（Unix 秒）、真实 session_id、logical_turn_id、cycle_id、request_id、message_ids、stage、event_type、decision、reason_code、detail、duration_ms、schema_version。

- 每个调度批次创建独立回合；相同消息集合与相同调度结论的重复检查不重复记录。通过门控的 ID 交给 Planner，关联后续内部轮次、Replyer、工具、发送和附件。
- Planner 使用同一个真实 logical_turn_id；wait 后的同一工具链继续复用该 ID。内部轮次使用当前 cycle_id，每轮仍独立显示。续轮不会仅因已经成功回复而被回放归类为沉默。
- optimized 模式缓冲新消息、结束后重新门控的现有行为不变，记录 deferred/regate/新门控批次。legacy/observe 内部合并消息也保留真实关联 ID。
- 通过 ContextVar 隔离任务上下文，asyncio.to_thread 会携带关联上下文。目标会话不同的独立发送另建回合，禁止借用其他群的回合；存储层再次校验 session_id。
- 独立 @ 的入队、过载、运行和取消有自己的回合；暖群、自动复读、其他独立发送和标签提取任务分别标记。独立模型请求使用其真实 RequestTraceContext.session_id 和请求 ID，避免错误借用调用方会话。
- 模型 request_id 使用 `真实请求 UUID:供应商尝试序号`，开始与完成更新同一请求记录。重试是另一真实调用；Planner 快照、工具监控等不会再次累加 Token，也不重复写原 ModelUsage 账本。
- 未正常结束的记录显示 `incomplete_or_running`，不能仅凭它区分仍在运行与异常退出；事件时间可辅助判断，但不伪造结束原因。恢复 wait 会重新标记运行状态。

## 已接入字段与边界

| 模块 | 实际记录 |
|---|---|
| 调度 | 待处理消息 ID/数量、频率、强制触发、Focus 拒绝、wait、退避、运行中缓冲、重新门控、延迟检查 |
| 动态门控 | 实际输入特征、逐项贡献、Logit、概率、阈值及来源、窗口预算、观察机会；贡献单位明确为 Logit |
| frequency | 消息阈值及现有触发/等待/延迟结论，不伪造动态概率 |
| Planner | 开始、内部轮次、工具及成功状态、中断、纠错/续轮、当地生命周期模式判定、最终 CycleEnd 原因、待处理消息 |
| Replyer | 开始/生成成功或失败、耗时、模型、目标消息 ID、有效文本状态、生成片段数；reply 工具补记后处理分段数量与附件 Tag 状态 |
| 发送 | 真实发送返回值、成功可见消息 ID、文本/表情类型、失败；冷却拒绝独立记录 |
| 附件表情 | Tag 有效性、双层概率结果、候选数、所有候选分数组成（每 100 个分一条事件）、前 100 名摘要、惩罚、最高分/门槛、选中 Hash、冷却与发送 |
| 独立表情 | @ 固定候选随机选择、50% 分支与文字降级；暖群随机选择及豁免冷却，均不套用附件评分公式 |
| 标签 | 召回命中/未命中/关闭/异常/超时、返回的标签 ID、最终 Provider 请求中确实存在的参考 ID 与目标（插件移除时标记未注入）；提取任务开始/结束、候选数、冲突裁决数/预算延后、提交成功/失败 |
| 用量 | 每个宿主可观察 Provider 尝试的模型、任务、ID、Token、缓存、耗时、尝试编号、状态；未上报时为 null |

费用复用原有价格时段选择和计算公式，只计算展示值、不重复记账，属于本地配置价格估算，不能当作供应商实付账单。原统计与回放合计不得再次相加。缓存字段仅在 UsageRecord 标记供应商报告时展示；供应商适配器可能由已报告输入/命中量推导未命中量，回放不额外推测。部分请求无用量时，Token 合计是已知部分；费用有缺失时总费用显示未知。

以下内容不编造：旧事件没有关联 ID 时不进行日志文本反推；旧监控记录仍在麦麦观察。本页从启用后开始累积。标签兴趣权重尚未接入，明确标记 not_integrated。标签新增/更新/合并/拒绝的事务细分数量、Replyer 自然语言“风格”、供应商账单币种/实付金额没有可靠统一接口，当前不虚构这些字段。插件自行绕过宿主 LLMOrchestrator 或发送服务的调用无法完整追踪。候选缩略图暂不额外接入，Hash/评分可查。

现有单候选表情会直接选中、不经过 70 分计算，回放标记 singleton_bypasses_scoring；没有修改这一设计。暖群、独立 @ 的冷却豁免、附件概率、权重系数、人格和 Replyer Prompt 均保留。

## API

所有端点为同步 def，由 WebUI 线程池处理 SQLite，避免占住主循环或 WebUI 事件循环：

- `GET /api/webui/decision-replay`：session_id、start/end（Unix 秒）、source、gate、planner、emoji、spoken、failed、page/page_size（最多 100）、ascending。emoji=not_sent 包含未发生表情路径的回合。返回分页回合、当前筛选范围统计、记录开关/保留天数和队列健康。
- `GET /api/webui/decision-replay/sessions`：真实会话名称，历史快照名称兜底，不在界面优先展示哈希。
- `GET /api/webui/decision-replay/{turn_id}?session_id=...&after=...&limit=...`：校验会话归属，事件游标分页（最多 200），返回逐请求前 100 条及明确的总数/截断标记。
- `GET /api/webui/decision-replay/{turn_id}/requests?session_id=...&page=...&limit=...`：逐请求分页（最多 200）。

外部调度触发、逻辑回合、Planner 请求、Provider 请求、成功文字发言回合分别计数。消息数仅合并触发/输入消息 ID，不把 bot 分段或附件消息 ID 加进触发数量。一次回合多个分段只算一次成功发言，所有真实发送仍在事件中保留。

## 持久化、清理和性能

独立 SQLite：`data/decision_replay.db`，不受麦麦观察保留条数限制。使用 WAL；turns、events、requests 三张表，按会话/时间/来源/门控/Planner/表情/请求 ID 建索引。查询有分页并在一致的读事务中生成页面和汇总。

热路径仅生成少量结构化字段并放入有界 Queue（4096 条），后台线程每批最多 100 条事务写入；两个事件循环均不执行同步 SQLite 写入。每小时及首次列表读取按当前配置清理过期回合及其关联事件/请求（重启和热重载后也读取当前保留天数），不做热路径 VACUUM。正常退出尝试刷新队列，最长等待 3 秒；异常断电或进程被强杀可能损失队列尾部，留下不完整回合。

磁盘锁定、磁盘错误、队列满时丢弃监控记录并记录健康计数/限频错误日志，不让聊天等待或失败。此机制是可观测性记录，不是不可丢失的消息账本；健康计数按进程累计，重启后清零。

本机临时库合成 5000 回合的批量写入约 167 ms，第 100 页（25 条，含汇总）查询约 13 ms。只是单次本地合成基线，不代表真实 QQ 多群并发或长期磁盘负载表现。模型请求增量为 0，Token 增量为 0；新增开销为本地字段整理、队列、SQLite 与查询。

## 验证与示例

测试不会使用真实 QQ 或收费模型。pytests/conftest.py 默认关闭记录并把存储隔离到每项测试临时目录；专项测试显式启用临时记录。首次验证产生的少量合成痕迹已保留至 temp/decision-replay-test-artifacts，不留在正式回放库。

- 新增专项：33 项通过，覆盖持久化、关联、去重、分页、过滤、鉴权、跨群、失败、取消、无开关副作用、实际门控/评分/冷却分支、召回超时、示例精确合计。
- 受影响组合回归：297 项通过、1 项跳过、19 个 subtests 通过（其中包含当时的 31 项专项；之后增加保留配置与最终标签注入两项专项并单独通过）。
- Dashboard：回放、菜单、路由共 36 项通过；TypeScript 与 Vite 生产构建通过。仅有现存的大包体积提示和 Python 依赖弃用告警。
- Ruff 与 git diff --check 通过。没有提交或推送。

`qq-agent-decision-replay-example.json` 是测试数据经真实 ReplayStore 重建的可复现示例：19 条事件，2 次去重模型调用，输入 1200、输出 80，文字及表情均发送成功，optimized 本地结束。所有消息/标签/群/费用均为合成值，不能作为真实账单或上线证据。

## 文件清单

新增：
- src/decision_replay/__init__.py、recorder.py、store.py：事件关联、探针、写入队列、存储、保留和查询。
- src/webui/routers/decision_replay.py：鉴权查询 API。
- dashboard/src/routes/monitor/decision-replay.tsx、decision-replay.test.tsx：真实 API 页面与测试。
- pytests/decision_replay/test_replay.py、test_integration.py、pytests/conftest.py：专项测试和测试数据隔离。
- 本文与 qq-agent-decision-replay-example.json：接口/模型/风险/示例。

修改：
- src/config/config.py、official_configs.py：模板开关、保留天数及配置结构版本。
- src/maisaka/runtime.py、reasoning_engine.py、turn_trigger/gates.py、scheduler.py：真实触发关联、门控数据、轮次与生命周期探针。
- src/maisaka/monitor/events.py：复用真实 Planner 快照；探针异常不会阻断原广播。
- src/maisaka/builtin_tool/reply.py、src/chat/replyer/maisaka_generator_base.py：生成、实际注入、后处理分段。
- src/llm_models/utils_model.py：Provider 尝试开始/成功/失败，用原请求 ID 关联。
- src/services/send_service.py、priority_at_reply_service.py、group_silence_emoji_service.py：平台回执与独立路径。
- src/services/tag_memory_recall.py、tag_extraction_service.py：标签参考、注入、任务与冲突状态。
- src/emoji_system/reply_attachment.py、local_selection.py：概率、候选评分与选择。
- src/webui/routers/__init__.py：注册独立端点。
- dashboard/src/router.tsx、components/layout/constants.ts、i18n/locales/{zh,en,ja}.json：导航及标签。
- pytests/maisaka/test_planner_lifecycle.py、test_priority_at_reply.py：补齐已有测试桩接口，不删除原断言。
- dashboard/src/__tests__/router.test.tsx、components/layout/constants.test.ts：保留严格菜单/路由检查，补上既有标签记忆及新回放入口。
- changelogs/changelog.md：功能说明。

## 回退与未验证风险

关闭 debug.decision_replay_enabled 即可停止新增记录，页面仍只读。没有更改 legacy/observe/optimized 的行为、发言权重、Prompt、概率或发送间隔；无需撤销聊天数据。回放库独立，可以停机后另行归档，不能为清理回放删除主 MaiBot.db。

尚未进行真实 QQ、多群线上压力测试、异常断电测试或真实供应商账单比对。长时间进程/大量群聊可能触发队列丢弃或产生较大 SQLite 文件；清理会释放可复用页而不缩减文件本身体积。升级后启动再有新流量才会形成真实历史；已有旧日志不会自动变成完整回放。
