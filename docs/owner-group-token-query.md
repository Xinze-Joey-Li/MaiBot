# 主人群聊 A02 Token 查询增量

## 基线与范围

基于远程 feature/qq-agent 的 v0.4.0、8d85934d8；开发分支 codex/owner-group-token-query。已阅读AGENTS.md，复用原有独立工作树，未覆盖G:\QQbot\MaiBot的123项本地未提交状态，未重启或部署机器人。

实际代码中不存在预先规划的public_view/owner_private_view机制或10项群聊公开白名单：M1的11个查询动作均注册为私聊；M0兼容命令有独立群聊规则。经用户确认，本次只新增A02群聊公开权限，其他动作保持实际基线，不把规划中的11项公开查询写成已实现。

修改前M0/M1/M4联合基线：186 passed，32条既有Pydantic弃用警告。A02原先由command_router注册的allowed_chat_types={private}阻断；自然语言又要求private才继续解析。read_service._tokens输出本身带估算费用及私有模型标签，直接放开动作会泄露信息。审计原先只有授权/执行结果，没有回执发送结果。

## 最小修改与权限边界

- 在现有read_commands增加显式PUBLIC_GROUP_READ_ACTIONS，仅含owner_admin.read.tokens；原动作仍为A02，不增加第二套统计或授权服务。
- A02注册允许private/group，OwnerPermissionGuard原样保留：真实QQ user_id匹配主人、本地核验Bot账号、目标平台/账号/会话一致。普通群友、昵称/正文自称主人、additional_config伪造授权均无效。
- 自然语言仅放行A02完整模板；其他群聊管理仍不进入可执行动作。参数继续使用原严格解析器，无效窗口也拒绝并审计。
- 群聊费用意图返回“请私聊”，在统计读取前结束；私聊保留原估算费用、缓存及分模型详情。
- 源消息是引用/合并转发时沿用原M0拒绝路径；有限中文模板不读取引用、历史、日志或模型输出。正常包含Token关键词的闲聊不会执行统计。

本次不依赖改动owner_group_commands_enabled启用A02；原配置开关继续限制其原有兼容命令，不放宽其他动作。没有修改生产配置、权限识别范围、记忆/人格/发言算法、Prompt或项目版本。

## 命令与自然语言

```text
/dfy tokens          # 默认24h
/dfy tokens 1h
/dfy tokens 24h
/dfy tokens 7d
/dfy tokens today
/dfy tokens 24h --page 2
```

支持“大肥鱼，今天用了多少 Token？”、“大肥鱼，看看最近一小时的 Token 消耗。”、“今天调用了多少次模型？”、“大肥鱼，最近 24 小时的缓存命中率是多少？”及“大肥鱼，这几天 Token 用量怎么样？”。“这几天”明确映射7d并显示实际区间。“大肥鱼，今天 API 花了多少钱？”在群聊只引导私聊；在主人私聊使用原A02详细查询。

## public_view与owner_private_view

公开结果由OwnerReadService.public_view直接构造，选择由可信actor.chat_type决定，用户或模型不能传参数切换到私有视图。私聊owner_private_view是原_tokens详细输出路径，未削减原字段。

群聊仅发布：

- Asia/Shanghai统计起止时间、[start,end]边界及整体宿主记录范围。
- 已记录LLM请求数、输入Token、输出Token、总Token。
- 启用缓存的命中Token、未命中Token及命中率；无有效分母显示不可统计。
- 真实分模型用量分组，按查询结果匿名编号；不公开自定义模型分配名、真实模型/供应商标签或账户名称。匿名编号只在本次查询内有意义。
- 统计来源/缺失口径说明、分页信息。

费用金额、账单、余额、充值、账户身份、认证信息、私有接口、配置、Prompt和完整请求/响应均不属于投影字段。投影不会读取这些字段，且不把完整统计对象传给LLM；公开结果没有LLM润色步骤，因此不会经模型重新带入私有字段。错误也只显示类型，不泄露异常内的数据源地址或SQL。

沿用原有每页4行、单行230字符、最多400行限制及--page N；分页每次重新查询，不声称跨页快照一致。分模型最多查询400项，总展示仍受400行限制。

## 数据真实性与WebUI一致性

继续调用statistics_service._get_summary_statistics_sync和_get_model_statistics_sync，与WebUI复用相同业务查询，没有新增统计SQL。使用临时SQLite真实记录进行对照测试，不伪造生产监控记录。

- today是上海当天00:00至查询时刻，1h/24h/7d为滚动窗口；转换为宿主本地墙钟与现有ModelUsage存储保持一致。
- 总Token沿用摘要口径input+output；请求数为ModelUsage行数，没有落库的失败尝试不能统计，不能当作全部API尝试次数。
- 缓存只聚合prompt_cache_enabled记录。供应商缺少miss时按原WebUI逻辑由input-hit或input补全；命中率是hit/(hit+miss)，不是hit/全部Token或请求成功率。
- 没有缓存样本显示不可统计；空库显示零条已记录调用，不推断为没有发生任何失败。
- 实时查库，非Dashboard缓存；区间结束时间是统计截至时刻，非供应商账单更新时间。
- 是当前宿主整体ModelUsage，可能包含多个账号/任务；不冒充当前群消耗，不按群聊天比例估算，也未新增任意指定群Token归属查询。

## 完整路由与发送审计

可信SessionMessage → ChatBot.receive_message原生会话归属 → 原OwnerAdminCommandRouter → 固定A02动作/白名单 → 原Guard → 原审计reserve → 原统计业务线程 → 公开投影/私聊原输出 → 原text_to_stream → 原PlatformIOManager及发送驱动 → 回执审计。

审计parameter_summary新增entry_source（原已有）、output_view（public_view/owner_private_view）及private_details_requested。原授权、执行结果、真实来源消息ID、群ID/会话及目标记录保留；不保存响应正文或敏感账单。

owner_admin_deliveries是同一审计库的附加表，通过audit_id关联，仅为A02保存发送级别及时间，不改变原授权/执行状态：

- confirmed：发送服务得到至少一个平台驱动SENT确认，不代表用户已经阅读。
- unconfirmed：原发送服务返回False，包括明确失败、无路由、丢弃或平台结果未知，不能一概冒充平台拒绝。
- unknown：发送调用异常/取消，没有取得布尔确认。
- 没有该记录：尚未发送、进程终止或审计写入失败，不能视为成功。写入失败完整记本地日志，不盲重发。

发送异常/取消也在finally记录。审计同步事务在线程执行；其他动作没有新增delivery记录，但管理回执入口会进行一次受限审计检查。未增加普通聊天/Planner的查询或模型调用。

## 验证结果

- 修改前：M0/M1/M4基线186项通过。
- 联合回归：643项及19个子测试通过，覆盖M0–M4、原主人兼容、标签存储、适配器策略、动态门控、独立@、Planner上下文/等待/调度、回放统计和WebUI标签路由。
- 最终A02/M0/M1/M4专项：225项通过（含A02新增39项），32条既有Pydantic弃用警告。
- 改动Python文件ruff、git diff --check：通过。

新增测试覆盖窗口筛选/真实数据、WebUI同区间口径、总Token/缓存归一化及20%命中率、空数据/数据源故障、私聊费用保留、非主人/伪造授权/错误账号、5种自然问句、群费用限制、其他动作不变、引用/转发、无配置变更、投影字段白名单及慢查询事件循环响应。

路由专项使用可信SessionMessage、实际ChatBot.receive_message、原text_to_stream、实际Broker与路由表、隔离Driver的SENT/FAILED/UNKNOWN/取消回执，检查最终群123/456、账号、正文、执行与发送审计；没有通过模拟Action返回成功替代路由。管理查询不进入Planner，不写聊天记忆。隔离Driver不连接真实QQ，真实平台端到端仍未验证。

## 涉及文件与剩余限制

生产代码仅涉及read_commands（语法及白名单）、command_router（注册/自然语言/审计/帮助）、natural_language（A02问句）、read_service（公开投影）、audit_store（发送审计）、chat/message_receive/bot（回执确认）。测试是新A02专项及原M1/M4两处权限断言调整；同步README、M1/M4说明和changelog。

未部署、未推送、未升级版本。其他计划公开查询不属于本次范围。真实QQ发送/身份来源仍需获得授权的测试资源验收。现有统计缺失、全宿主范围及分页实时变化保持原有边界；没有账单API、精确失败调用总数或可靠公开模型名称映射时不补造。150ms统计阻塞注入下事件循环至少维持8次10ms心跳，不代表真实生产高负载性能保证。
