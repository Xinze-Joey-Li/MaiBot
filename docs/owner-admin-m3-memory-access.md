# QQ-Agent M3：本人记忆与群聊权限

## 基线与范围

- 分支：`codex/owner-admin-m3`，基于 M2 `a6f9c6398`；其父提交为 M1 `83c073012`、M0 `8d22c5bfa`。
- 在已附加的独立工作树开发，未修改、合并或部署到 `G:\QQbot\MaiBot` 的运行目录，原目录现有 123 项未提交状态保留。
- 已阅读 `src/A_memorix/MODIFICATION_POLICY.md`，未改动 A_Memorix 实现层、学习算法、人格配置或 M4 系统控制。
- 本文的验证是本地代码、临时 SQLite、故障注入和模拟入站验证；没有发送真实 QQ 消息、发起模型调用或伪造生产监控证据。

## 四项权限状态

| 权限 | 状态 | 实际支持范围 |
| --- | --- | --- |
| D02 添加明确记忆 | 实现，有范围限制 | 当前账号已知主人私聊内的本人稳定事实；最多300字，受保护的 `owner_explicit` 来源；不允许选择他人的 person_id |
| D03 更正与删除 | 部分实现 | 本人 `owner_explicit` 标签的事实更正和逻辑撤回，先预览、后确认；永久删除、来源不足的历史学习/A_Memorix 全局事实暂不支持 |
| E03 接入黑白名单 | 实现，有路由限制 | 复用适配器策略，对当前 QQ 账号、唯一已匹配接收适配器逐群 allow/deny/reset；后续增量支持主人私聊添加group/private目标放行，允许目标尚无历史会话；多路由歧义拒绝，交由本地管理 |
| E04 用户屏蔽 | 实现 | 按真实群会话或显式当前 Bot 全局范围，基于 QQ user_id 限制回复触发，时长1分钟至7天，支持替换、取消和重启恢复 |

## 命令与确认

全部 M3 命令仅允许经过 M0 验证的主人私聊调用，群内和普通用户均拒绝并审计。

```text
/dfy memory add 我喜欢红茶
/dfy memory me [关键词]
/dfy memory edit <32位记忆ID>
/dfy memory edit <32位记忆ID> 我喜欢绿茶
/dfy memory delete <32位记忆ID>
/dfy confirm <32位预览令牌>

/dfy access list [--page N]
/dfy access deny <群ID>
/dfy access allow <群ID>
/dfy access reset <群ID>
/dfy access allow group <群ID>
/dfy access allow private <QQ号>

/dfy ignore <用户ID> 24h --group <群ID>
/dfy unignore <用户ID> --group <群ID>
/dfy ignore <用户ID> 24h --global
/dfy unignore <用户ID> --global
/dfy ignore list [--page N]
```

主人私聊无法可靠推断目标群，因此原始无范围的 `/dfy ignore <用户ID> 24h` 与 unignore 会提示追加范围，不会默认为全局。

更正、撤回、群接入变更返回具体目标及影响预览。令牌五分钟失效，绑定 QQ 平台、Bot 账号、主人 ID 和发起私聊会话，只能执行一次；同时最多100个待确认预览，重启后必须重新预览。新内容在预览和执行时均校验；记忆修订号、适配器身份或基础策略摘要变化时拒绝旧预览。

`memory edit ID` 只展示目标及更正语法，不会让模型生成新事实。`memory delete ID` 的预览和结果都明确写出“逻辑撤回，原始消息/证据/审计仍保留，不是永久删除”。没有永久删除确认选项。

## 记忆读写调用链与归属

```text
可信 QQ 入站元数据
  → M0 OwnerAdminCommandRouter / ActionRegistry
  → OwnerPermissionGuard（真实主人、私聊、已验证Bot及目标范围）
  → 审计 reserve / 请求幂等
  → OwnerMemoryAccessService（内容、归属、预览/确认、排队后再次授权）
  → TagMemoryService 已初始化存储
  → TagMemoryStore.record_batch / edit_tag / delete_tag
  → 单个 SQLite 事务、原有修订审计、commit/rollback

memory me → 同一 TagMemoryStore.list_current_tags
会话画像 → MemoryService.profile_admin → TagMemoryService.get_person_profile
Planner/Replyer 参考 → 共用 tag_memory_recall → TagMemoryStore.search_tags
```

当前会话画像与自动参考使用会话标签，并不读取 A_Memorix 的全局事实账本。本次检查了 A_Memorix 的 `memory_fact_admin` create/update/retract 等接口；直接写入该账本会形成当前画像查不到的另一条数据链。故在 MaiBot 接入层复用实际生效的标签维护接口，没有绕过 A_Memorix 维护逻辑直接修改其数据库，也没有另建平行记忆库。

显式记忆沿用现有数据结构：

- `tags`：正文、创建时间、有效状态、修订号，`requires_long_term=True`。
- `categories.path`：严格为 `人物 / 可信主人QQ ID / owner_explicit`。
- `batches.external_id`：`owner_explicit:<Bot账号>:<主人QQ ID>:<M0 request_id>`。
- `tag_sources` / `batch_sources`：真实命令的来源消息 ID；M0 审计包含同一来源 ID、身份、动作、时间及请求关联。
- `tag_revisions`：复用原有更正与撤回记录；不在管理审计参数中复制私人记忆正文或确认令牌。

普通学习任务不能指定保留的 `owner_explicit` 分类或批次前缀。来源查询结果显式区分 `owner_explicit` 和 `unclassified`；旧数据缺少可靠来源时，不冒充自主学习、模型推断或已确认的信息。原有 disputed/superseded 状态过滤继续保留。

写入仅接受第一人称偏好、习惯及少量稳定事实句式，中英文有限白名单；先做 NFKC 规范化，拒绝控制字符、权限/指令内容和任意提示词。检索仍沿用“历史数据而非指令”的既有模板；没有模型授权或记忆授权路径。

修改目标必须同时满足：真实聊天流属于当前 Bot、分类属于当前主人、批次来源属于当前账号和主人、标签当前有效。没有 person_id 参数。来源不明、他人标签、其它账号标签一律拒绝。

更正复用修订号检查与事务，旧正文只留在修订审计；显式批次摘要是固定说明，不保存旧事实副本，没有额外向量或全局事实副本。撤回沿用软删除，停止有效标签、关键词检索、会话画像和批次上下文中的标签展示。原始聊天记录与修订审计不是有效记忆，但仍可能作为历史数据存在；本阶段没有清除它们。

## 接入策略结构、生效与优先级

持久化继续使用 `config/adapter_policy.toml` 的现有 adapters、group/private、allow_ids、deny_ids 结构，不增加另一套名单文件。

原规则按匹配身份字段的 specificity 从具体适配器到较宽条目、defaults、implicit_default 求值，组内显式名单、旧名单和默认动作继续由既有 `_evaluate_policy` 解释。QQ 管理通过同一引擎验证，不在命令内复写匹配算法。

M3 写入使用精确 adapter_id/plugin_id/gateway/platform/account/scope 身份，不直接修改所继承的宽范围条目。否则旧 `_find_or_create_adapter_policy` 可能选中仅按插件定义的共享条目，使其它机器人账号受影响。现有 WebUI 写入默认语义保留；所有公开文件写入接口新增同一进程内 RLock，避免并发读改写丢失。

确认时重新解析当前接收路由、核对账号与适配器，并检查整个策略文件 SHA-256 是否与预览一致。保存前在候选文档上用既有引擎验证实际 allow/block；保存沿用临时文件原子替换；保存后重新读取实际策略并返回 allowed/source/reason。已禁用的适配器不能由群 allow 重新启用。

原群规则命令保留；新增明确语法access allow group 群ID / access allow private QQ号可添加没有历史会话的目标，使用主人当前真实私聊的唯一接收适配器。所有接入修改及确认必须来自已通过认证的主人私聊；不提供关闭私聊或更改私聊默认策略的入口。因此不会通过禁用最后一个群而切断当前主人私聊管理通道。无法证明账号/路由归属或多路由歧义时拒绝，要求本地管理。

接入规则在适配器下一条入站消息求值时立即使用，没有旧值缓存。它不是 M2 静音，不回收已运行的 Planner 回合，也不撤回已提交平台的消息。

## 用户屏蔽结构、链路与优先级

复用 M2 的 overrides / override_events：

- `property=ignore:<真实QQ user_id>`，`value=True`。
- 群范围 `target_scope=chat,target_id=<真实session_id>`；显式全局为 `target_scope=bot,target_id=<当前Bot账号>`。
- `created_by/request_id/created_at/expires_at/status/priority=100` 继续沿用 M2 字段。
- 平台固定限定为可信 QQ；其它平台即使账号字符串相同也不会命中。

同一群同一用户重复设置替换旧规则；群规则与显式全局规则为“任一有效即屏蔽”，取消某层不取消另一层。到期由同一个 M2 协调器处理，运行时有效快照也直接检查截止时间；重启加载时重新计算。没有第二个过期任务。

执行顺序：既有适配器接入/安全限制 → 正常平台解析和 M0 管理认证 → 用户级回复触发过滤 → 现有静音、Focus、频率/动态权重与 Planner 自主判断。M3 不修改门控算法、权重阈值或学习算法。

屏蔽覆盖独立 @ 候选与排队执行、普通待处理批次、动态/频率门控输入、Focus 注意力、自动复读及有可信回复来源的实际投递边界。已缓存的 @ 不再让强制触发状态替被屏蔽用户绕过门控。发送检查还验证来源会话与投递会话一致，防止派生任务上下文误伤其它群。

消息继续正常入库、进入多人历史上下文；他人引用被屏蔽消息时按实际发送者判断，不把被引用人的屏蔽传给引用者。昵称变化不影响规则。不能设置屏蔽当前主人；即使规则后来对应新的主人 ID，运行时仍按当前可信主人配置豁免，M0 管理命令始终先处理。

## 验证

环境：`uv`、Python 3.12.9、已有 frozen lock 环境，无新增依赖。

- 相关回归：**493 passed，19 subtests passed**；测试报告含33条警告，通过结论不代表依赖警告已消除。
- M3 专项：59项，均使用临时数据库/配置，未向生产监控补造记录。
- Ruff 指定改动文件检查及 `git diff --check` 通过。
- 扩展检查发现旧 `test_pending_observation` 夹具缺少 session_id 及当前评分类型字段；核对父提交确认同样存在，补齐真实运行时字段并使用真实评分 dataclass，没有在生产代码新增 fallback。

覆盖用户要求的14项：写后重新检索、来源标记、他人/跨账号拒绝、权限注入拒绝、更正与撤回一致性、真实删除语义、事务异常原子回滚、名单跨群/跨账号隔离、屏蔽不误伤其它人、昵称变化、到期恢复、主人入口保护、失败审计。另覆盖预览过期/重放/修订变化/配置变化/授权撤销、实际接收路由、禁用适配器、历史 @、普通群友 @、自动复读及跨群发送上下文。

SQL authorizer 故障注入分别在 tag_sources 和 tag_revisions 写入阶段拒绝：新增标签与批次一起回滚；更正/撤回保留原正文、valid 状态和原修订号，M0 失败审计可追踪。策略写入故障保留原文件；旧预览不能覆盖 WebUI 已改的策略。

事件循环验证：注入150ms同步记忆写入延迟，10ms节拍仍推进至少8次；整条事务在 to_thread 执行。运行时屏蔽不访问 SQLite，100条待处理消息只构建一次规则快照/哈希集合，成本为规则数与消息数之和，不增加模型调用。

主要验证命令包括 owner_admin 全部测试、tag_memory_store/service、adapter_policy、dynamic_reply_gate、priority_at 的回复/准入/并发、reply_statistics、message_turn_scheduler、auto_repeat、planner_lifecycle、pending_observation、wait_turn_lifecycle、context_history、planner_messages、reasoning_engine、reply_internal_labels 和 WebUI tag_memory_routes。

## 涉及文件

- `src/services/owner_admin/memory_access_commands.py`：固定语法、范围、分页、时长。
- `src/services/owner_admin/memory_access_service.py`：四项权限业务、预览令牌、归属校验及异步事务入口。
- `src/services/owner_admin/access_policy_service.py`：真实路由解析、精确策略变更及前后验证。
- `src/services/owner_admin/trigger_policy.py`：可信用户过滤与单次批次快照。
- `src/services/owner_admin/command_router.py`：沿用 M0 注册、Guard、幂等、审计；确认审计记录实际动作及目标。
- `src/services/owner_admin/read_service.py`：本人记忆查询显示 ID、来源和时间。
- `src/services/owner_admin/overrides.py`：统一规则生命周期增加受控 ignore 属性。
- `src/services/owner_admin/override_runtime.py`：普通/独立回复来源与发送边界。
- `src/services/tag_memory_store.py`：受保护来源、归属查询、事务结果，复用既有维护逻辑。
- `src/services/tag_memory_service.py`、`src/services/tag_memory_recall.py`：画像和共用参考显示显式来源。
- `src/platform_io/adapter_policy.py`：精确身份写入、同进程写锁、候选策略验证。
- `src/maisaka/runtime.py`、`src/maisaka/focus/runtime_mixin.py`、`src/maisaka/turn_trigger/scheduler.py`：在既有门控前过滤用户触发贡献，保留历史。
- `src/services/priority_at_reply_service.py`、`src/services/auto_repeat_service.py`：阻止旁路触发。
- `pytests/services/test_owner_admin_m3.py`：M3 专项回归。
- `pytests/services/test_owner_admin_overrides.py`、`pytests/maisaka/test_pending_observation.py`：对齐真实运行时测试夹具。
- `changelogs/changelog.md`、本文：功能、验证和限制。

## 回滚与剩余限制

1. 规则回滚：`access reset 群ID` 先预览确认后继承当前基础规则，不恢复旧配置副本；`unignore 用户ID --group 群ID/--global` 取消对应层；自然过期也不回写基础配置。更正可再次 edit 回正确事实，但撤回不伪装为可由本阶段永久清除或自动恢复。
2. 记忆仅写入已知主人私聊，在真实所属会话检索，不自动把私聊事实广播到各群。目标发现沿用最近501个真实聊天流上限，M1 memory me 仍是最近50流/每流20条；超出查询范围不代表数据不存在。
3. 仅能修改有可靠 `owner_explicit` 归属的标签。来源不足的历史学习、模型推断与 A_Memorix 全局事实需要后续独立的来源与归属方案，不能借 M3 扩大对其他人的记忆权限。
4. 逻辑撤回不会删除原始 QQ 聊天、证据、修订审计，也不会追溯清除已发往模型的请求或已冻结的参考。后续新检索不再返回撤回的有效标签，但不能保证其它独立历史/未来学习任务不会记录相同语义；没有将隐藏伪称为永久删除。
5. 群接入只处理当前账号下唯一明确的接收适配器。QQ 之外的平台、多路由及禁用适配器的重新启用交由本地入口；不实现 QQ 群禁言、踢人或插件控制。
6. 只保证核心回复触发链；任意第三方插件独立产生的消息不在 M3 控制范围。已交给平台的在途发送不能召回，群接入变更也不取消旧 Planner 回合。
7. 配置写锁为同一进程服务 WebUI 与 QQ，不是跨进程锁；外部编辑器并发改写不提供分布式事务保证。基础文件变化会使预览摘要失效。
8. 线程事务取消时等待提交/回滚；M0 pending 幂等记录阻止未确认请求盲目重放。进程被强杀或审计结果回写失败时，可能需要本地依据请求与存储状态核查，不自动重试写操作。
9. 未做真实 QQ 接入、真实模型、OS 进程重启和长期高负载验收；重启恢复由重新构造 manager 并加载真实临时数据库验证。本次不自动部署、不进入 M4。

后续白名单增量的命令、权限与验证见 [主人私聊白名单](owner-private-whitelist.md)。
