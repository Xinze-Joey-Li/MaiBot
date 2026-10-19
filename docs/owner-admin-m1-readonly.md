# QQ-Agent 主人管理 M1 交付记录

基线：M0 提交 `8d22c5bfa`，分支 `codex/owner-admin-m0`。先复验 M0：25 项通过。
M1 分支：`codex/owner-admin-m1-readonly`，独立提交，只增加只读权限，不部署、不进入 M2。
主工作区 `G:\QQbot\MaiBot` 的已有未提交工作和运行实例未改动。

## 授权与操作方式

11 项查询全部经 M0 `OwnerAdminCommandRouter → ActionRegistry → OwnerPermissionGuard → audit_store`。
要求可信 QQ 发送者匹配后台主人、Bot 账号范围经本地确认、私聊发起。群聊全部拒绝，普通群友全部拒绝。
原有 M0 help/whoami 和兼容命令策略保持不变；没有新增配置写入或 M2 权限。

分页：在原命令末尾追加 `--page N`，1～100 页，每页至多 4 行，每行至多 230 字符摘要，整体小于 1500 字符。
同一命令翻页会重新实时查询，未声称跨页一致性快照；超过 400 行明确截断。
窗口限 1h、24h、7d，显示 Asia/Shanghai 起止时间。
Token 沿用 WebUI ModelUsage 的闭区间查询，回放与发言投影使用半开区间；两种边界均在输出标注。

## 逐项状态

“部分实现”表示命令及授权已实现，但当前底层来源不足以兑现完整字段，不以相近口径补齐。

| 权限 | 命令 | 状态 | 实现、口径与剩余限制 |
| --- | --- | --- | --- |
| A01 | `/dfy status` | 部分实现：底层数据不足 | 版本、主循环、Broker、插件运行时及当前进程存活时间。没有统一 QQ 实时连接快照，显示不可用，不把适配器加载状态当作连通。 |
| A02 | `/dfy tokens 1h/24h/7d` | 完全实现已记录消耗；账单接口不支持 | 复用统计服务的请求、输入/输出、缓存、分模型和记录估算费用；真实账单及费用币种未记录。缓存是 WebUI 归一化口径，未命中可能按输入补全，不谎称全部供应商原始值。分模型最多400项，输出仍受400行限制。 |
| A03 | `/dfy stats [群ID] 24h` | 部分实现：底层覆盖不足 | 六种口径分别展示。gate.decided 为触发检查观测；model.request started 且 task=planner 为模型尝试；Replyer生成、文本气泡、表情、平台成功数复用 reply_metrics。未记录口径显示不可统计，历史旧口径不补算。 |
| A04 | `/dfy models` | 完全实现配置查询 | 列出任务、模型、供应商；明确仅配置、未探测健康。不输出 URL、API Key，不执行探测请求。 |
| A05 | `/dfy errors 24h` | 部分实现：底层分类不足 | 按小时与结构化类型聚合回放失败和监控 llm.error；两来源分开展示，不相加。无法可靠细分所有超时或完整插件异常；不读取/输出堆栈、环境变量或错误正文。 |
| A06 | `/dfy replay 消息ID` / `/dfy replay latest 群ID` | 部分实现：关联证据不足 | 精确消息ID关联已有逻辑轮、原始消息摘要、消息监控和各阶段状态/原因码；缺失身份注入证明时明确未知。最多最近10轮、每轮前100事件，超出提示 WebUI。不会把相邻时间的事件猜成因果。 |
| C04 | `/dfy persona show` | 部分实现：覆盖来源不足 | 当前有效配置对象中的 personality、behavior_style、reply_style 摘要。临时覆盖没有统一可查询来源，明确未知，不冒充每个请求最终 Prompt。 |
| D01 | `/dfy memory me [关键词]` | 部分实现：只读范围有限 | 仅已认证 QQ 本人的有效人物标签，不接受 person_id。限本 Bot 最近50个真实聊天流、每流最近20个本人标签，关键词在此范围过滤。未扩展到全局画像/其他记忆来源，不调用可能产生写入/生成的检索接口。 |
| D04 | `/dfy learning status` | 部分实现：活动证据不足 | 标签/行为学习配置、表达/黑话学习规则与保留的 memory.extracted 事件；配置不等于每流已运行，没有记录不假称最近成功；任务成功不等于人格永久改变。 |
| E01 | `/dfy chats` | 完全实现当前可归属聊天流查询 | 名称、平台、群/私聊、目标ID、真实session_id、历史活跃时间、内存加载与Runtime状态分别展示。不把历史记录当在线。限定当前 Bot 账号，归属缺失的旧流不混入。 |
| F01 | `/dfy health` | 部分实现：监控来源不足 | 主循环、Windows 当前进程 CPU 生命期单核平均/工作集、SELECT 1、插件加载状态、回放队列及聊天Runtime队列快照。不能将快照当完整健康保证；看门狗只写日志，没有可查询卡顿快照。非Windows在线时长/内存接口未实现，明确不可用。 |

## 数据与保留边界

- 监控 event_store 保留策略为72小时/10000条，已有清理策略不变；消息观察只按精确ID返回，不猜 Planner 决策。
- 决策回放按当前保留配置查询，M1最多查询7天；超期、未采集、无关联和清理造成的缺失不能区分时直接说明。
- v2 回复统计读取同一业务投影，并标明激活时间、是否采集及保留事件范围，不能当生命周期总数。
- stats、errors、replay、memory、chats按已认证Bot账号解析真实聊天流，不生成 fallback session hash；tokens 是宿主全局 ModelUsage，与 WebUI 同口径。
- 请求去重与审计仍按 M0 执行，审计可用才执行查询；M1不新增监控/决策记录。

## 涉及文件

- `src/services/owner_admin/command_router.py`：注册11权限、严格路由，沿用M0授权/审计。
- `src/services/owner_admin/read_commands.py`：命令语法、参数与分页边界。
- `src/services/owner_admin/read_service.py`：查询调度、隐私清洗、摘要及分页。
- `src/services/owner_admin/read_data.py`：真实聊天流、只读事件关联和结构化聚合。
- `src/services/statistics_service.py`：分模型查询增加可选上限，WebUI默认10项不变。
- `src/decision_replay/store.py`、`reply_metrics.py`：新增只读连接/查询选项，复用原有详情和统计投影。
- `src/maisaka/monitor/event_store.py`：精确消息ID只读观察查询。
- `src/services/tag_memory_store.py`：只读人物标签查询，避免既有查询中的活跃过期写入。
- `pytests/services/test_owner_admin_reads.py`：11项命令、权限矩阵、空数据、只读与循环响应性测试。
- 本文及 `changelogs/changelog.md`。

## 验证与性能

- 已以 `uv sync --frozen` 在隔离工作区安装锁定依赖（Python 3.13.3）。
- M1 新增测试80项通过；M0回归25项通过；回复回放/统计测试56项通过；共享标签/统计回归26项通过。
- M1、M0、回放、共享标签/统计及普通聊天/Planner/独立@联合回归：243项及19个subtests通过；之后增加的敏感文本和连续查询用例在最终M1专项中通过。
- 本机真实数据库只读一致性快照对照：Token 汇总与同一 WebUI 统计服务一致；reply_metrics只读投影与WebUI投影一致；真实回放关联成功。该快照保留2932条回复事件，Token查询约20ms，副本已删除；不代表所有数据规模下的性能上界。
- 使用临时空库验证缺失数据，不向生产库新增假数据，不为通过测试生成虚构监控或决策事件。已有回放测试采用隔离测试桩，并非真实QQ证据。
- 查询在线程执行，单服务最多2个查询；主循环只复制内存状态。注入阻塞数据源时，事件循环仍可响应；模型和QQ均未调用。
- 统计投影超过50000保留回复事件时拒绝生成半截结果；最多500个可归属聊天流，更多应指定群或使用WebUI。
- SQLite只读查询仍会消耗线程、CPU并短暂持有读取快照，极端写负载与真实QQ长期连续查询未验证。没有执行全仓测试、QQ联调或部署。
