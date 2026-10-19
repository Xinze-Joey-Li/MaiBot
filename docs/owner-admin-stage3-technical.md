# QQ-Agent 主人管理系统第三阶段技术文档（M0–M4）

日期：2026-10-20。M4 基于已提交 M0 8d22c5bfa、M1 83c073012、M2 a6f9c6398、M3 57a3f850d；分支 codex/owner-admin-m4。

## 交付与验收边界

在原已附加独立工作树继续开发，保留 G:\QQbot\MaiBot 的原有未提交工作及运行实例。没有部署、热更新、启动、重启、发送真实 QQ 测试消息或操作生产插件。没有推送远程。

下述“实现”表示代码及注明的隔离测试通过，**不代表25项全部生产验收通过**。本次无获授权的真实QQ测试群、撤回消息和插件Runner测试资源：真实QQ撤回、真实Runner生命周期及完整生产场景仍待验收。自然语言入口采用受控中文模板，未接入通用LLM意图识别；不将此限制称为完整自然语言理解。

## 25项权限总清单

M4之后的A02增量允许可信主人群聊查询公开Token用量，其他新增权限仍限主人私聊，复用M0；help/whoami、兼容入口和确认/覆盖查询是辅助动作，不计为额外业务权限。

| 编号 | 命令 | 状态与实际边界 |
| --- | --- | --- |
| A01 | status | 部分实现：版本、进程/主循环、Broker/插件状态；缺可靠QQ实时连通数据时显示不可用 |
| A02 | tokens [1h/24h/7d/today] | 群聊公开用量，私聊完整授权用量和记录费用；默认24h。已记录消耗实现；与WebUI共用ModelUsage服务。记录费用为估算，真实账单/币种接口不足，不混用 |
| A03 | stats [群ID] 24h | 部分实现：触发、Planner、Replyer、气泡、表情、实际发送六口径分开；历史未采集项不可统计 |
| A04 | models | 配置查询实现；不含Key/URL，不把配置值当探测健康 |
| A05 | errors 24h | 部分实现：按时间/结构化类型聚合，来源分开；未结构化错误不能完整归类 |
| A06 | replay 消息ID / replay latest 群ID | 部分实现：仅已有精确关联证据；缺失身份/原因不编造，超期或未采集如实提示 |
| B01 | mute 群ID 时长 / unmute 群ID | 实现：非@自主发言受限，正常@沿用原规则，旧回合最终发送复查 |
| B02 | frequency 群ID 值 时长/reset | 实现：现有talk_value预算路径生效，0..1；基础零频率不被重新打开 |
| B03 | threshold 群ID ±百分比 时长/reset | 实现：dynamic最终权重概率门槛独立偏移，限±50%；非dynamic明确拒绝 |
| B04 | mode 群ID quiet/normal/active/mention_only [时长] | 实现：转成既有预算/自主参与配置，不另造Planner |
| C01 | style gentle/calm 时长/reset | 实现：审核表达预设，不改基础reply_style |
| C02 | persona default/gentle/playful/calm [时长] | 实现：审核预设，同轮Planner/Replyer快照一致；保留鲸鱼娘身份、主人关系和记忆 |
| C04 | persona show | 基础与有效临时覆盖查询实现；不宣称等于每个在途请求的全部最终Prompt |
| D01 | memory me [关键词] | 部分实现：当前Bot可归属真实聊天流中的本人标签，有限扫描；不开放其他人隐私和全局A_Memorix画像 |
| D02 | memory add 内容 | 实现：本人稳定事实、owner_explicit来源、事务写入并可重新检索；拒绝权限/系统指令注入 |
| D03 | memory edit/delete ID | 部分实现：可靠归属的owner_explicit事实更正/逻辑撤回；保留原始证据/修订，不支持永久删除 |
| D04 | learning status | 部分实现：实际配置与可核实活动分开；无记录不声称成功，不将单次成功当永久人格变化 |
| E01 | chats | 实现当前账号可归属聊天流查询；历史、内存加载、运行状态分开 |
| E02 | chat config/set 群ID 字段 值 | 实现：白名单frequency/threshold/mode等支持字段，不允许任意toml键 |
| E03 | access list/deny/allow/reset [群ID]；access allow group/private ID | 主人私聊添加群聊/私聊目标放行，限定账号及唯一适配器；原群规则保留；歧义拒绝；不关闭主人私聊入口 |
| E04 | ignore/unignore 用户ID … --group 群ID/--global | 实现：可信平台用户ID，显式范围，统一期限；只限制触发，保留消息/其他人/主人恢复入口 |
| E06 | recall 消息ID | 受限实现，平台验收待完成：仅近期可靠SENT回执+本地本人消息+真实聊天映射；固定NapCat API明确成功才成功 |
| F01 | health | 部分实现：进程资源、数据库、插件/队列快照；缺看门狗可查询源等显示不可用 |
| F02 | pause 时长 / resume / pause status | 实现，隔离验收通过：有界软暂停、不关适配器/进程；管理与恢复保留 |
| F03 | plugin list/enable/disable 插件ID | 受限实现：真实Host方法及模拟Supervisor验证；真实Runner/生产资源未验收，关键插件及依赖拒绝变更 |

M1监控event_store仍为72小时/10000条；决策回放遵循自身保留配置。查询最大7天，today为Asia/Shanghai当天00:00至当前时间，其他时长为滚动窗口。Token闭区间与事件半开区间沿用原业务口径并标注。未知/空数据不补造。

## 完整调用链与安全边界

可信QQ平台入站元数据 → 原有消息解析/真实聊天流 → ChatBot前置管理路由 → 确定性/dfy解析（优先）或受控自然语言模板 → 不可变AdminActionRequest → ActionRegistry固定动作 → OwnerPermissionGuard → 审计reserve及请求幂等 → 具体业务服务 → 必要预览/确认及再次授权 → 原服务/事务/固定平台API → 真实AdminActionResult → 审计完成 → 原会话管理回执（A02群聊公开投影，其余私聊）。

认证只依赖真实QQ platform、user_id、Bot账号及真实session_id，使用本地配置owner_qq；昵称、Prompt、模型字段、记忆、日志和查询内容均不能授予权限。Bot账号通过本地服务核实；目标群必须是当前Bot已知、唯一可归属真实流，不计算fallback hash。群内仅新增A02公开用量；普通用户及其他群内管理动作仍拒绝并审计。

管理消息在普通聊天/插件钩子之前处理。只接受平台本次顶层命令；引用/转发内嵌命令拒绝。自然语言要求顶层全为文本，时间在当前前120秒/未来5秒容差内；不将历史、模型回复、查询或记忆检索结果重新送入意图入口。原有确定性命令认证和语法不被自然语言替代。

审计先持久reserve再执行，存储不可用时失败关闭。请求ID绑定原生消息及操作者/动作，重复请求不盲重试。QQ错误输出仅摘要，完整异常留本地日志；不输出凭据、环境变量和敏感堆栈。

## 自然语言映射与确认

当前是有限中文模板，无额外模型调用。固定别名覆盖25项动作，参数仍由原/dfy解析器完整校验：

| 表达 | 结构化映射/结果 |
| --- | --- |
| 大肥鱼，看看今天用了多少 Token。 | tokens today，只读 |
| 管理：查询运行状态 | status，只读 |
| 管理：查询Token消耗 1h | tokens 1h，只读 |
| 早茶群最近太吵了，先安静两个小时。 | 唯一精确群名解析→mute 群ID 2h，预览确认 |
| 管理：设置表达风格 gentle 2h | style gentle 2h，预览确认 |
| 管理：添加我的记忆 我喜欢红茶 | memory add，校验内容/本人归属，预览确认 |
| 管理：更正我的记忆 ID 新内容 | 原memory edit预览确认 |
| 管理：软暂停机器人 30m | pause 30m，预览确认 |
| 管理：恢复机器人 | resume，直接恢复逃生通道 |
| 管理：启用插件 test.safe | plugin enable test.safe，预览确认 |
| 今天说话温柔一点 / 刚才为什么没回我 | 缺时长/消息ID，要求澄清，不猜目标 |
| 别说了 / 闭嘴 / 滚去睡觉 | 普通聊天，不自动暂停 |

未来模型候选契约仅允许单字段 {"command": "/dfy …"}；validate_candidate重新调用严格解析器并检查选定注册集合。附带authorized/owner_verified等字段、未知Action、任意API、Shell、安装/配置任意写入均拒绝。当前没有实际LLM proposer、后台执行器或自由生成“已执行”反馈；返回值来自真实业务结果。这是部分自然语言支持的明确限制。

复用M3同一预览存储，不新建确认系统：最多100个，五分钟过期，重启失效；绑定操作者platform/Bot/user/session、原始request_id、固定Action、具体目标及冻结参数。确认审计记录原动作、原请求、目标、期限和参数SHA256；参数内容不作为授权依据。确认只能消费一次，其他人不能消费；执行时重新检查OwnerPermissionGuard及目标/配置摘要。暂停、插件变更、撤回必须确认；自然语言新增副作用操作也预览确认。恢复命令无需危险确认。

## 软暂停设计、生命周期与发送边界

复用OverrideManager，Bot范围soft_paused=True，priority=100，必须1分钟至7天，状态RUNNING/SOFT_PAUSED；不调用/offline、/online，不关闭Adapter。覆盖完整记录override_id/property/scope/target/value/creator/时间/期限/status/priority/request_id；同项替换、事务持久化、取消、统一到期机制不变。

优先级为系统安全/软暂停 → 黑白名单/屏蔽 → 主人临时覆盖 → 群持久配置 → 当前全局基础配置。到期只撤去覆盖，重新读取现时基础配置，WebUI修改不会被旧值回滚。

新入站普通消息在暂停时保留历史但不触发Planner；Scheduler和独立@准入拒绝新任务。Planner每轮及请求返回处检查暂停；安静期等待之后检查代际再收消息，暂停后恢复也不能重启旧轮。仅退出当前普通轮，不取消主循环。自动复读暂停；其他定时/插件普通输出在统一平台发送边界拦截，后台学习任务未停。

每次暂停使用随机override_id作为账号代际；从历史override事件重启恢复最新代际。旧轮、独立@准入队列及已派发未启动任务携带准入代际，恢复或到期后仍失效，防止旧结果补发。新消息可按既有门控新开轮，不强制回复积压。暂停消息不等于删除消息。

账号级投递屏障采用并发读/独占写：不同群正常投递仍并发；暂停变更等待已进入投递的操作结束，发布状态后才返回确认。已经交给远端的平台发送无法追溯取消，发生在暂停确认之前；确认之后的旧投递被阻止。取消等待中的暂停不泄露屏障。已有会话锁继续保证群内配置与投递边界。

管理回执豁免是ChatBot本地设置的、绑定当前已授权来源session的ContextVar（A02允许群聊）；消息/模型字段不能设置。普通@也被软暂停拦截，区别于只限制自主发言的M2群静音。健康查询、管理命令、resume仍可用。

## 撤回真实能力及限制

只读检查本机已安装SnowLuma适配器：apis/message.py公开adapter.napcat.message.delete_msg，query/action服务调用OneBot delete_msg，并检查status/retcode。平台ID来自真实发送DeliveryReceipt.external_message_id，send_service将其保存为platform_message_id；不能将本地UUID直接当平台ID。

M4要求OutboundTracker最近120秒SENT确认回执、明确QQ账号/Plugin Driver、唯一内外ID映射、当前适配器及scope匹配；同步数据库查询在线程中验证记录发送者确为当前Bot、真实聊天归属与发送时间，再检查公开固定API。其他用户、其他账号、历史/未知/歧义ID、未确认回执、缺公开API均拒绝。确认时再次检查全目标及期限。

仅调用固定invoke_api(plugin, adapter.napcat.message.delete_msg, numeric_message_id)，不得选择任意后台API。只有Envelope无错误、payload.success=True、result.status=ok且整数retcode=0才成功。超时结果UNKNOWN不自动重试；失败按真实返回。无数据库删除伪装撤回。

120秒为保守客户端限制，并非保证所有QQ平台时限相同；平台最终裁决。进程重启后内存回执丢失、旧消息、缺映射和不提供该API的适配器暂不支持。真实QQ成功/失败仍需获授权资源验证。

## 插件保护、依赖与回滚

复用PluginRuntimeManager.apply_plugin_config，与WebUI共用全局变更锁；只修改已验证Manifest对应config.toml的[plugin].enabled。配置读取/扫描/原子文件替换在线程，生命周期异步执行；list分开显示configured_enabled与actual加载状态，每页4项。禁用为持久enabled=false并同步卸载；启用必须实际加载成功才返回成功，不能将临时卸载当持久禁用。

保护所有内置插件、非extension插件、LLM provider、当前已注册Plugin Driver及其传递依赖，因此QQ通道、认证/管理组件和基础依赖不允许关闭。管理/身份服务本身在核心，不提供可卸载动作。依赖图合并Manifest与实际运行时图；若目标存在其他插件反向依赖，保守拒绝；启用所需依赖须已加载，避免隐式启动控制组件。该保守规则可能拒绝安全但复杂的变更，交由本地管理。

预览冻结文件摘要，执行锁内再查授权、目标保护、摘要。只改enabled，保留其他配置；失败经同一生命周期入口恢复原始文件/原加载状态并验证。若文件已被其他写入改变则不覆盖，回滚失败明确报告状态不确定，不能假称恢复。取消时等待已开始的生命周期任务收敛。不给全局插件管理员、安装、Shell、任意配置修改权限。

隔离测试使用真实ManifestValidator及真实Host.apply_plugin_config，模拟Supervisor/平台，不代表真实Runner子进程或生产插件可靠性已验证。

## 关键模块与改动职责

| 文件 | 职责 |
| --- | --- |
| services/owner_admin/command_router.py | 注册三系统权限、受控NL入口、再授权、统一审计/确认分发 |
| system_commands.py / system_service.py | 严格命令语法与暂停/插件/撤回业务编排 |
| system_runtime.py | 内存暂停判断、代际租约、本地管理回执范围 |
| plugin_control.py | 已验证插件目录、保护图、enabled变更及生命周期验证/回滚 |
| recall_service.py | 双ID/发送账号/聊天归属证明及固定平台API结果校验 |
| natural_language.py | 有限模板、别名、候选契约、参数再解析 |
| overrides.py / override_runtime.py | 统一持久期限、重启代际、投递屏障、最终发送拦截 |
| memory_access_service.py | 复用M3冻结预览/确认，审计目标/参数摘要 |
| platform_io/outbound_tracker.py / manager.py | 只读查询近期明确SENT回执 |
| maisaka/runtime.py / reasoning_engine.py / turn_trigger/scheduler.py | 保留暂停消息、拒绝触发、终止旧Planner轮 |
| services/priority_at_reply_service.py / auto_repeat_service.py | @准入代际与队列失效、复读抑制 |
| chat/message_receive/bot.py | 仅真实管理回执设置本地豁免 |
| read_commands.py / read_service.py | today上海日界窗口，复用既有统计业务 |
| pytests/services/test_owner_admin_m4.py | 安全矩阵、故障、并发、隔离场景 |
| 既有M3/overrides/priority/adaptor测试 | 修正真实类型字段、测试导入顺序及旧构造夹具 |

没有修改A_Memorix、权限识别范围、基础人格、学习算法、实际bot_config/model_config、依赖或Prompt模板。

## 自动化验证报告

环境：Windows、Python3.12.9、uv --frozen锁定依赖。独立临时SQLite及测试文件，不改生产库，不新增虚构生产监控记录。

- M0–M3、标签服务、适配器策略、动态门控、独立@、Planner等待/上下文/调度、回放统计/WebUI标签路由联合：493 passed，19 subtests passed，54.90s。
- M4专项94项 + Outbound取消7项：101 passed，27.51s。32条既有Pydantic弃用警告，不作为测试失败。
- 插件状态同步及Adapter生命周期：6 passed，1.25s。旧Adapter测试object.__new__绕过构造缺少_status_listeners；父提交代码同样如此，仅修正夹具，不改生产运行时。
- 插件状态同步、Adapter生命周期和Runner激活联合：34 passed，69.24s（包括上述6项，不重复计数）。激活测试使用真实进程内PluginRunner及临时插件，Host传输被隔离，仍非M4真实外部端到端。
- 分组累计628项及19个子测试通过；最后help文字调整后M0专项另复验12项通过，不重复计入628。
- 改动Python文件ruff及git diff --check：通过。

安全覆盖：普通用户/群内拒绝、冒充授权字段、未注册Action、提示词/记忆/日志注入载体、合并转发/引用来源、历史NL、模糊口语、错误群/平台消息ID、撤回他人/旧消息/错账号、未确认发送、平台拒绝及UNKNOWN、暂停超期/重启/重复/取消、旧Planner和@队列、跨群并发、控制插件/依赖保护、停用运行时、过期文件摘要、插件失败回滚及回滚失败、审计不可用、他人确认、重复确认/执行、原/dfy兼容。

## 15步端到端隔离场景

| 步骤 | 验证结果与证据范围 |
| --- | --- |
| 1 状态 | 原M0路由/真实查询代码；临时数据，非生产健康证明 |
| 2 最近一小时Token | 空SQLModel库真实查询，零记录/费用来源明确 |
| 3 指定群发言统计 | 临时空ReplayStore查询，不用假事件填结果 |
| 4 真实决策回放 | **未完成真实记录验收**；仅验证缺失消息诚实返回，没有编造原因 |
| 5 群安静 | M2真实覆盖写入及查询 |
| 6 临时风格 | M2真实覆盖及统一快照 |
| 7 查询/添加本人记忆 | 临时TagMemoryStore真实事务/来源/重新检索 |
| 8 更正新记忆 | 原M3预览/确认/修订路径 |
| 9 屏蔽与解除 | 显式群范围，统一Override |
| 10 暂停 | M0审计/确认→持久软暂停 |
| 11 暂停中健康 | 原管理查询继续返回 |
| 12 恢复 | 取消覆盖，旧代际不复活 |
| 13 插件禁用/恢复 | 临时测试Manifest/config，真实Host方法、模拟Supervisor |
| 14 撤回 | 受控回执和消息映射、模拟公开API；**非真实QQ撤回** |
| 15 自然语言重复 | today查询、style预览/确认等有限模板，与/dfy同注册执行链 |

全过程检查对应审计、预览/成功状态、参数绑定与至少24条允许记录；空ReplayStore事件数保持0。未触碰生产群/生产插件。步骤4及13/14的真实外部验收缺失，完整生产端到端验收**未通过/未宣称完成**。

## 性能、回滚与剩余工作

自然语言固定匹配长度≤600，不发模型请求，新增管理解析Token消耗0；回复为业务结果模板，无额外LLM反馈调用。每次插件扫描有文件成本，在线程执行；150ms阻塞扫描注入期间10ms事件循环心跳仍至少8次。普通聊天暂停读取为内存快照，不加数据库/模型请求。账号屏障允许不同群并发，暂停等待现有平台请求完成；不存在实际生产高负载基准，不能给吞吐保证。

暂停取消/到期均恢复当前基础行为，旧轮失效；重启从持久覆盖/事件恢复。插件事务不能保证远端任意插件副作用可逆，文件及实际加载恢复分别验证。撤回为远端不可逆，不提供伪回滚或失败盲重试。程序退出后确认令牌失效，须重新预览。

回滚代码需先在本地/已授权管理入口恢复暂停、恢复被改变插件的enabled状态，再回退独立M4提交；代码回退不自动撤销已执行的平台撤回、插件副作用或先前M1–M3数据。未自动回退生产。

后续需获授权测试资源完成真实QQ撤回及失败、真实Runner插件生命周期、带真实消息ID的决策回放及完整串联验收。通用LLM意图识别、多语自然语言、永久删除记忆、不支持适配器撤回、任意安装/后台执行均未实现。仅模板支持不应标为自由自然语言生产可用。未进入M5或扩展未选定权限。

## A02后续增量

群聊仅公开Token用量，私聊保留完整授权用量及记录费用。基于v0.4.0实际代码，本次仅新增A02一个群聊公开动作；并非把25项或计划中的11项查询整体放行。范围、字段、安全测试及发送审计详见 [A02群聊查询说明](owner-group-token-query.md)。
