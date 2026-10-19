# QQ-Agent 主人管理 M2：行为与人格控制

基线：M0 `8d22c5bfa` → M1 `83c073012`。开发分支 `codex/owner-admin-m2`。
沿用当前聊天所附隔离工作区，不改动 `G:\QQbot\MaiBot` 中的现有未提交工作和运行实例。
本阶段没有部署、推送、创建发布标签或开发 M3/M4。

## 七项权限

所有命令仅限经过 M0 可信 QQ ID、Bot 账号范围及权限检查的主人私聊。
无效语法、未知/跨 Bot/不唯一群聊天流均拒绝；不计算替代 session hash。
修改操作在排队后、数据库事务开始前重新核验主人授权。

| 权限 | 命令 | 实现与作用 |
| --- | --- | --- |
| B01 | `/dfy mute 群ID 1h` / `unmute 群ID` | 临时静音只限制该真实聊天流的非 @ 自主发言。普通入站走现有静默接收，真实 @ 仍遵守原来规则；静音前已运行的自主回合也受最终投递边界约束。unmute只取消静音，不取消mention_only等其他配置。 |
| B02 | `/dfy frequency 群ID 0.6 2h` / `frequency 群ID reset` | 临时替换该聊天流的主动发言频率，合法范围0～1；运行时使用统一有效快照。reset回到当前持久群配置/基础配置，不恢复以前保存的旧值。 |
| B03 | `/dfy threshold 群ID +10% 2h` / `threshold 群ID reset` | `+10%`定义为最终触发概率阈值加0.10，即10个百分点；偏移限制正负50个百分点，最终门槛裁剪到[0,1]。只支持dynamic；其他触发模式明确返回unsupported且不写入。 |
| B04 | `/dfy mode 群ID quiet/active/mention_only 2h` / `mode 群ID normal` | quiet：继承频率×0.5、概率阈值+0.05；active：频率×1.25并封顶1、阈值-0.05；mention_only：只允许本轮真实 @ 直接回复；normal取消临时mode、继承当前基础/群配置。概率阈值分量仅dynamic有效。 |
| C01 | `/dfy style gentle/calm 2h` / `style reset` | 当前Bot账号范围的固定表达预设，不修改基础reply_style。优先于persona中的表达预设，也抑制本次随机替代风格和Planner工具提出的冲突风格。 |
| C02 | `/dfy persona gentle/playful/calm 2h` / `persona default` | 仅切换审核过的表现预设，不能替换核心身份/安全规则/关系/记忆。default只取消persona，显式style是独立属性，需要style reset单独取消。 |
| E02 | `/dfy chat config 群ID` / `chat set 群ID 字段 值` | 持久群字段仅frequency、threshold、mode。例：frequency 0.6、threshold +10%、mode quiet；字段值reset取消该持久层，mode normal也取消持久mode。不允许任意TOML键。 |

辅助命令：`/dfy overrides`、`/dfy overrides 群ID`。
查看当前Bot的有效覆盖、来源层级、状态、override_id及到期时间；追加 `--page N` 翻页。
期限支持整数m/h/d，范围1分钟至7天；每页至多4行、每行摘要230字符、上限100页/400行。
用户回执时间显示Asia/Shanghai偏移，存储使用Unix时间。
M1的 `persona show` 已增加当前有效persona/style覆盖摘要。

## OverrideManager

持久化文件：`data/owner_admin/overrides.sqlite3`，不进入共享历史。

每条记录包含override_id、property、target_scope、target_id、value、created_by、created_at、
expires_at、status、priority、request_id。群目标使用真实session_id，Bot表达预设以已验证Bot账号为目标。

- 临时层priority=100；群持久层priority=10、expires_at为空。模式的频率/阈值组合按固定规则计算，不创建另一个Planner。
- 同一目标、属性、层级的旧记录变为replaced，新记录获得新override_id。SQLite唯一索引禁止多条同层active记录。
- 取消记录为cancelled，到期记录为expired。统一override_events保存变更、系统到期及M0 request_id，能关联原始管理审计；到期操作者标记为system:expiry。
- 一次事务内完成旧记录状态变更、插入、事件记录和完整快照读取，成功提交后一次发布内存快照。
- 协程取消不能中断已进入线程的事务；等待事务结束并发布相同快照后才传播取消。事务失败则回滚，旧内存快照不变。
- 有效快照读取不查库、不建库、不更新配置，按expires_at即时排除到期条目。
- 只有一个到期协调器负责持久化过期状态及重排已有待处理消息，没有每功能一套计时器，也不会因到期强制发言。
- 启动在插件/消息接入前加载覆盖并处理已过期条目；九份预设文件在线程中预热。关停接入既有统一协调器。

## 优先级和冲突规则

1. 既有系统安全/准入和全局零频率、局部零频率调整，不能被M2重新打开。
2. 原来的明确黑白名单、用户屏蔽、Focus准入及冷却路径保持原位；M2不新增白名单或绕过路径。
3. 主人临时覆盖。显式临时frequency/threshold优先于mode派生的同类数值。
4. 群持久配置。mode按继承数值施加固定系数/偏移；临时直接数值仍可独立覆盖其数值分量。
5. `ChatConfigUtils`读取的当前talk_value/talk_value_rules及全局默认。

静音、mention_only是限制条件，即使active或frequency=1也不能解除它们。
M2没有实现新的全局暂停管理权限；这里只保留既有安全/准入约束。
WebUI通过稳定ConfigProxy替换基础配置后，下一次基础读取即取得新值。
临时覆盖到期不写回旧副本；若存在显式持久群配置，按优先级继承它，需要单独reset才回到WebUI基础值。

## 真实作用路径和立即生效边界

- 频率：`ChatConfigUtils.get_talk_value → MaisakaHeartFlowChatting._get_effective_reply_frequency`
  应用有效覆盖 → 现有MessageTurnScheduler及DynamicReplyGate回复预算；旧frequency模式仍用原消息数量折算。
- 阈值：`DynamicReplyGate._resolve_threshold`原有窗口/静态门槛 → 独立threshold_delta → 最终比较。
  分数、模型权重、预计需求计数和已回复额度不因threshold覆盖被替换。
- 触发回放记录最终门槛、覆盖ID及偏移。静默接收回放区分owner_autonomous_suppressed与原零频率。
- mute/mention_only：复用现有静默消费和历史维护；实际PlatformIOManager投递前再次读取当前覆盖。
- 每个群的投递与覆盖变更共用一把锁。在平台投递已经开始时，mute等待它结束再确认；
  确认后旧回合后续气泡不能再提交。不能撤回已在平台处理或已送达的消息。
- 可信直接 @ 豁免由本轮实际入站缓存冻结，安静期结束后再刷新最终批次；独立@队列由真实At准入。
  模型选择历史@消息、回复引用、正文标记或伪造priority_at_response字段均不授予豁免。
- frequency/threshold/quiet/active影响接下来的原有门控，不撤销已经通过门控的回合；
  mute/mention_only则立即约束正在运行回合的后续实际发送。
- 变更后重排已有Runtime中的待处理消息，不凭空创建聊天回合或强制回复。

## 人格连续性

原始personality、behavior_style、reply_style、主人安全关系规则、记忆和当前情绪来源均保留。
新预设只有gentle/calm/playful固定值，中英日各三份模板；没有任意文本或Prompt参数。

Planner和Replyer仅通过override_runtime的统一表达来源取值。
同一逻辑轮冻结同一表达快照，避免一方已切换、另一方仍使用旧风格；新的逻辑轮使用新的预设。
预设到期会使已冻结的表达快照在后续Prompt构造时失效。已经发送给模型的请求无法修改，
已在途请求/已生成正文可能保留旧表达，这与行为发送边界的立即复查不同。

## 验证

- M2专项：覆盖所有命令变体、群隔离、多属性及并发替换、取消、重启、到期协调器、
  真实门控阈值、真实频率路径、真实平台入口、在途投递锁边界、@豁免、防伪造、
  人格连续性、多语言、事务失败回滚、协程取消一致性、撤权、幂等及审计。
- WebUI配置替换验证直接替换config_manager.global_config，与现有热重载路径一致；
  稳定ConfigProxy下，临时0.6覆盖失效后读取新基础0.8，没有回到旧0.4。
- 观察到并修复M0审计首次并发初始化竞争：一次性受锁初始化WAL/schema；
  8个并发首次查询全部成功且schema初始化仅一次。审计失败仍拒绝执行。
- 已完成Python3.13锁定依赖下的相关联合回归378项、19个subtests。
- 最终在与生产相同的Python3.12.9下，使用同一uv.lock验收：联合487项、19个subtests通过，
  包含M0/M1、M2、Planner、复古Replyer、上下文、发送、回放及启动关停。
- M2专项包含126个参数化用例；故障注入也覆盖到期协调器失败的立即完整日志，而非静默丢失任务。
- 所有验证使用隔离数据库、测试桩与故障注入，不向真实QQ发消息、不调用付费模型、不新增生产假监控记录。

## 性能与剩余限制

- SQLite事务、初始化和预设预热均在线程中执行；频率/阈值/发送路径读取内存有效快照。
  每次快照成本随当前active覆盖数量线性增长；覆盖数量极大时尚未做长压测。
- 变更按目标投递锁和全局持久化锁串行，避免覆盖互相丢失；mute的确认延迟可能包含已有平台投递的剩余时间。
- 到期和重启恢复已经本地验证，真实QQ、模型长耗时、高频群和OS重启未做现场验收。
- M2独立概率阈值只适用于dynamic；其他触发模式明确不写入。quiet/active的频率分量仍在原frequency模式生效。
- 既有近阈值PendingObservation仍基于调整后的gap运行；阈值不是硬性发送禁令，硬性自主限制用mute或mention_only。
- 人格切换影响新逻辑轮；不能追溯改写在途模型请求或已送达消息。
- 只支持单一Host进程统一管理器写入；不提供多Host同时编辑同一个覆盖库的缓存同步协议。
- 数据库或审计不可用时操作失败并保留原快照，不声称生效；没有替代配置或自由Prompt兜底。
- 到期协调器存储异常会立即记录完整错误，不自动吞掉；到期值仍由内存快照过滤，但持久化/主动重排须恢复服务后继续。

## 回滚与文件范围

操作回滚：unmute、frequency reset、threshold reset、mode normal、style reset、persona default；
持久群配置使用 `chat set 群ID frequency/threshold/mode reset` 分别取消。
这些取消只移除对应层，其他属性和更新后的基础值保留。
代码回滚应先按需取消有效覆盖，再回到M1提交并优雅重启；M1不读取覆盖库，
未取消的记录在重新启用M2时仍可能在有效期内恢复。不自动删库或写基础配置。

新增：owner_admin下的overrides.py、override_runtime.py、control_commands.py、control_service.py，
两个M2测试文件、九份中英日预设和本文。
修改：owner_admin注册/帮助、M1人格查询、审计初始化；Maisaka runtime/门控/调度/轮上下文/reply工具；
Planner与Replyer表达入口；PlatformIOManager最终发送入口；独立@服务；主程序初始化与统一关停；changelog。
既有测试只补齐真实Runtime新增依赖字段和关停组件测试桩，没有使用getattr兜底掩盖字段缺失。
未修改实际bot_config.toml/model_config.toml、配置迁移、A_memorix实现、前端或插件权限。

### 文件清单

- `changelogs/changelog.md`
- `docs/owner-admin-m2-controls.md`
- `prompts/en-US/owner_presentation_calm.prompt`
- `prompts/en-US/owner_presentation_gentle.prompt`
- `prompts/en-US/owner_presentation_playful.prompt`
- `prompts/ja-JP/owner_presentation_calm.prompt`
- `prompts/ja-JP/owner_presentation_gentle.prompt`
- `prompts/ja-JP/owner_presentation_playful.prompt`
- `prompts/zh-CN/owner_presentation_calm.prompt`
- `prompts/zh-CN/owner_presentation_gentle.prompt`
- `prompts/zh-CN/owner_presentation_playful.prompt`
- `pytests/decision_replay/test_reply_statistics.py`
- `pytests/maisaka/test_dynamic_reply_gate.py`
- `pytests/services/test_owner_admin_control.py`
- `pytests/services/test_owner_admin_overrides.py`
- `pytests/test_runtime_shutdown.py`
- `src/chat/replyer/maisaka_generator_base.py`
- `src/common/runtime_shutdown.py`
- `src/main.py`
- `src/maisaka/builtin_tool/reply.py`
- `src/maisaka/chat_loop_service.py`
- `src/maisaka/reasoning_engine.py`
- `src/maisaka/runtime.py`
- `src/maisaka/turn_trigger/dynamic_gate.py`
- `src/maisaka/turn_trigger/gates.py`
- `src/maisaka/turn_trigger/scheduler.py`
- `src/platform_io/manager.py`
- `src/services/owner_admin/audit_store.py`
- `src/services/owner_admin/command_router.py`
- `src/services/owner_admin/control_commands.py`
- `src/services/owner_admin/control_service.py`
- `src/services/owner_admin/override_runtime.py`
- `src/services/owner_admin/overrides.py`
- `src/services/owner_admin/read_service.py`
- `src/services/priority_at_reply_service.py`
