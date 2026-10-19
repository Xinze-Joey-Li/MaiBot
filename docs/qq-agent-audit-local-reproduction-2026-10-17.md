# QQ-Agent 审查问题本地复核与复现记录

日期：2026-10-17。输入：`QQAgent_v0.3.0审查结果.zip`，FINAL-R1、正式台账与历史 QA/XM 复核表。

## 结论与范围

27 个独立主题全部按本地代码核对；44 个历史编号的处置关系保留在外部证据目录的 `all-44-dispositions.csv`。

当前 HEAD：`d4f77b39934877be6908a5dcd3c4c5b100392f3c`。本轮验证对象包括当前未提交及未跟踪的源码、测试，不能用 HEAD 单独代表该快照。原审查基线为 `e26630c6a730a94dc4b622f84d4857b0d7048460`。

最值得优先修复的仍为 XM-001；QA-004 的事件循环阻塞和 QA-013 的取消清理缺口已用本地注入验证；QA-030 的条件矛盾仍存在。其他多项能复现所描述的行为，但业务违约、数据丢失或安全后果并没有因此得到证明。

本轮不修改业务实现、原有测试、正式配置或正式数据库；新增的实验探针只保留在仓库外。没有连接真实 QQ、调用模型供应商、启动真实插件进程、执行 GitHub Actions 或部署容器。

## 环境与证据

- 将当前 `src`、`pytests`、配置、模板、prompt、工作流与运行所需文件复制到临时目录；初次复制的 1,058 个文件逐文件 SHA-256 相等。随后补齐 `docs` 示例文件。
- 最终再次比较 845 个源码和原有测试 Python 文件，与本地工作区无差异。哈希清单见外部证据目录 `source-manifest.json`。
- 使用 `uv sync --frozen --python G:\QQbot\MaiBot\.venv\Scripts\python.exe`，将依赖安装到独立 `.venv312`；Python 3.12.9，pytest 9.0.3，96 个包按本地 `uv.lock` 安装。
- 原项目环境 dry-run 显示多一个 `silk-python`；正式结论采用上述干净环境，未改动原环境。
- 隔离目录：`C:\Users\E'L'Y'~1\AppData\Local\Temp\qqagent-audit-rss_ymqe`。
- 可交付证据：`C:\Users\E'l'y'si'a\Downloads\QQAgent_local_audit_2026-10-17`。包含实验脚本、观察值、最终 JUnit、源码哈希、逐项 CSV 和本报告副本；不含运行配置、凭据或数据库。

实验脚本中，QA-001 和 XM-013 提取本地函数的原始 AST 执行，仅替换其依赖；不是完整主进程、OS 信号或真实 Adapter 集成测试。其他动态探针直接调用复制后未修改的生产类或函数，并 mock 外部依赖。

## 27 个主题逐项结论

“复现”指观察到具体实现行为，不自动等于认定其为 Bug；“静态确认”不能冒充动态故障复现。

| ID | 本地结果 | 实测或检查依据 | 修改判断 |
|---|---|---|---|
| XM-001 | **可复现，接管缺口仍在** | 真实 Receiver 调用真实 `claim` 和 Runtime `register_message`，独立服务容量设为运行 1、等待 1。第 3 条被标记 handled，进入历史，普通 message_cache 为 0，独立队列也未接纳 | 优先修准入与所有权；现有服务级“拒绝后可重试”测试不能覆盖入口丢处理资格 |
| QA-001 | **部分确认** | 执行 `bot.py::graceful_shutdown` 原函数的隔离 harness，显式调用仅覆盖 ON_STOP、插件、图片和通用任务管理器；标签、记忆、独立 @ 的 shutdown 未调用。`src/main.py::main` 清单包含它们 | 关闭责任差异仍在；未验证真实 SIGINT、线程事务 drain 或数据丢失 |
| QA-003 | **异常被记录后返回，可复现** | `load_all_sessions_from_db` 注入 SQLite OperationalError，`initialize()` 正常返回 None，缓存为空 | 失败没有向启动调用者传播；后续能否按需恢复取决于数据库恢复情况 |
| QA-004 | **事件循环阻塞可复现** | 在生产 `get_or_create_session` 的同步 DB 上下文注入 250ms 延迟，10ms 心跳延迟约 251ms | 确认同步入口可阻塞；该数字是注入实验，不是生产延迟统计 |
| QA-011 | **后处理缺口可复现** | 驱动确认成功，存库注入 OperationalError；生产发送函数仍返回消息、仅发送 1 次，后续记忆通知未执行 | 保留送达成功正确；需考虑本地后处理恢复，不能直接改成失败重发 |
| QA-012 | **部分成功语义可复现** | 真实 Broker 双驱动，一 SENT 一 FAILED；批次 has_success=True，同时保留各一条成功/失败回执 | 广播定义如此；未证明上层产品承诺全部送达，不认定总体错误 |
| QA-013 | **取消清理缺口可复现** | 驱动抛 CancelledError，取消向上传播，tracker pending 仍存在；同 ID begin_tracking 抛 ValueError；推进单调时钟超过 TTL 后可清理 | 不是永久泄漏；修复需保留 UNKNOWN 和晚到 ACK 的保护语义 |
| QA-016 | **两人四条触发可复现** | a,b,a,b 的 4 条不同消息 ID、相同文本，结果 False/False/False/True | 若验收要求三人则需修；当前实现与原有测试一致 |
| QA-023 | **启动失败不抛出，可复现** | 正常构造 PluginRuntimeManager，仅将 `_start_supervisors` 注入异常；start 返回 None、is_running=False，状态订阅者收到通知 | 有日志、回滚、通知，不是完全静默；必需性应由启动契约明确 |
| QA-024 | **Hook 异常后继续，可复现** | 真实 EventBus 高权重 intercept handler 抛错，后一个 handler 仍执行，返回 continue=True | 未证明授权绕过；检索 src 未发现实际内建 `intercept=True` 订阅调用；真实外部插件动态注册不在本轮范围 |
| XM-005 | **处理失败后同 ID 重投被拒，可复现** | 真实 accept_inbound 首次返回 True；dispatcher 注入异常；同一 envelope 第二次返回 False，dispatcher 只调用一次 | 确认去重时序；未验证真实 Adapter 的 ACK、重投能力及至少一次处理要求 |
| XM-006 | **生命周期不随开关自动切换，可复现** | 配置 disabled 时真实服务 start；切换配置对象 enabled 并通知消息后，标签提取 `_worker` 仍为 None，旧摘要无 shutdown 调用 | 本探针模拟配置快照变化，没有走文件 watcher；现有热重载测试另行通过。若声明需重启，则属于生效提示问题 |
| XM-013 | **仅确认时序条件，损坏未复现** | 执行 `_init_components` 原函数，模拟插件 start 内入站，观察顺序为插件期入站→会话恢复 | 真实适配器何时开始推送、懒读与缓存覆盖是否实际产生异常，仍未证实 |
| QA-009 | **静态维护事实仍在，未复现运行缺陷** | Planner 仍协调回合、工具、异常和观测；已有 Planner 生命周期测试通过 | 无依据仅按复杂度拆引擎 |
| QA-010 | **静态跨模块私有调用仍在** | reply 工具仍读取 runtime `_chat_history`，调用 SendService `_send_to_target_with_message` | 当前签名故障未复现；按需求逐步建立公开边界 |
| QA-015 | **跨账号共享冷却可复现** | 不同 account_id 对应不同会话 ID，但 GroupEmojiGate 同 platform/group 键在首次发送成功后拒绝再次占位 | 源码明确群级共享额度；是否需要账号隔离是产品选择 |
| QA-017 | **初始化副作用可复现** | 新进程导入 DB 模块创建 data 目录；首次 get_db_session 将 `_db_initialized` 从 False 变为 True | 行为存在；未证明迁移失败或测试污染 |
| QA-018 | **GET 写入可复现** | 真实 FastAPI TestClient 访问标签 overview，使用全新临时 SQLite；HTTP 200，数据库被创建、11 条 categories 被写入 | 读请求与初始化耦合仍在；未证明越权或跨群污染 |
| QA-020 | **静态反向依赖仍在** | `host_service.py:21` 仍导入 WebUI 的 `_update_toml_doc` | 通用纯工具位置问题；未复现记忆依赖 WebUI 服务启动的故障 |
| QA-021 | **职责集中事实仍在，未复现运行缺陷** | HostService 仍包含生命周期、invoke、配置更新等；14 个配置更新/回滚测试、11 个 runtime 边界测试通过 | 不以类长为理由重写 |
| QA-022 | **维护事实仍在，未复现模型重试算法缺陷** | 编排器仍含兼容/重试/token 路径；发现两个原有重试测试 fixture 漏字段，补齐后原断言通过 | 先修测试构造，不能据 AttributeError 判定真实客户端重试错误 |
| QA-025 | **路由失败后返回部分应用，可复现** | get_all_routers 注入异常，create_app 正常返回；robots 存在，/api/webui 路由数为 0 | 缺 API 的应用可被创建；未验证真实部署健康检查是否误报 |
| QA-026 | **线程启动早于服务就绪，可复现** | 使用真实 ThreadedWebUIServer 和真实线程，替换底层 Server.start 为受控等待→OSError；startup_event 已置位且 is_running=True，稍后后台错误保存 | 本轮模拟端口错误，没有实际占用 7999；start 不向调用者同步传播失败 |
| QA-027 | **导入配置 IO 可复现** | 新进程审计 hook 观察到 5 次 config TOML open；模块尾部 initialize 调用存在 | 未在过期正式配置上执行升级写入；升级分支由原有隔离测试覆盖 |
| QA-030 | **静态条件矛盾确定存在** | 唯一启用事件 workflow_dispatch；Job if 要求 push，手动事件无法满足 | 可直接修 YAML；未远程运行 Actions，PR Ruff 不能据此判为失效 |
| QA-031 | **静态浮动镜像事实仍在** | Compose 默认 `sengokucola/maibot:latest` | 未启动 Docker，也未核实真实部署 digest；不能认定本机运行版本错误 |
| XM-007 | **静态共享私有状态仍在** | gates、reply、独立 @ 仍读取 runtime 私有历史；上下文与门控相关已有测试通过 | 维护传播风险仍在，无独立运行事故复现 |

## 已有测试结果

### 第一组：消息、发送、回放、会话、配置及插件

执行范围：priority_at_concurrency、priority_at_reply、decision_replay 目录、chat_manager_group_name、chat_manager_named_options、auto_repeat、planner_lifecycle、tag_memory_routes、config_manager_hot_reload、adapter_lifecycle。

结果：**140 passed、1 failed，另有 19 个 subtests 通过**。JUnit 的 tests=160 包含 subtests，不能与 pytest 主测试数量直接相加。

失败为 `test_adapter_plugins_can_be_taken_offline_and_restored_exactly`：测试通过 `object.__new__(PluginRuntimeManager)` 跳过构造器，未初始化 `_status_listeners`；正常生产构造器有该字段。仅在外部诊断 wrapper 中补 `_status_listeners=set()`，原测试原样执行通过。

最早一次使用原环境及未补齐 docs 的副本还发生过示例 JSON 缺失；补齐文档后、干净环境下该测试通过，属于本轮隔离准备问题，不计代码缺陷。

### 第二组：维护边界及原有保障

执行范围：host_service_config_update、runtime_lifecycle_boundaries、llm_retry_attempts、llm_service_cache_stats、webui/test_app、config_manager_startup_upgrade、tag_memory_service、tag_memory_store、context_history、dynamic_reply_gate。

结果：**91 passed、2 failed、1 skipped**。

两个失败均为 `test_timed_out_request_is_sent_at_most_max_retry_times` 的 Chat Completions/Responses 参数项：测试用 `object.__new__(LLMOrchestrator)` 仅设置 request_type，遗漏新增观测路径使用的 task_name/session_id；报错发生于真正重试断言之前。仅在外部 wrapper 补齐这两个生产构造器字段后，原测试两项均通过，仍满足每个 Provider 最多 3 次出站、前两次 warning/末次 error。没有修改原测试或重试实现。

跳过项为静态文件 symlink escape 测试：Windows 创建符号链接缺少权限，WinError 1314；不能计为通过。

### 本轮实验探针

结果：**24 passed**。包括 27 主题的动态/静态检查、软容量反证和三个 fixture 诊断 wrapper。9 个维护/部署项在一个静态检查测试中检查，故不是一主题对应一测试。

合计：已有测试 **235 项：231 通过、3 失败、1 跳过**，另有 19 个 subtests；外部探针 **24 项全部通过**。探针通过代表成功断言所描述行为，并不代表完成修复。

实验脚本初稿曾因错误 mock 函数名、软容量方法名及配置导入会间接初始化数据库而失败；已修正探针设置，最终完整 24 项运行通过。没有把这些探针准备错误计为项目问题。

## 撤销与归并项

- QA-005：实测 110 个活跃会话保留，符合 24 小时活跃保护的软容量实现；继续维持撤销，不据此推定泄漏。
- QA-008：`interest_weights:not_integrated` 仍是未集成功能；没有已承诺需求依据，继续维持撤销。
- QA-028：原有回放测试覆盖普通事件与关键回复事实的不同路径，包含重启后 UNKNOWN 抑制、重复回执与晚到 ACK；相关测试通过。继续维持撤销，磁盘完全失效时的绝对可靠性没有被本轮证明。
- QA-002、QA-029、XM-003 → QA-001；QA-006、QA-007 → XM-007；QA-014 → XM-001；QA-019、XM-009 → QA-018（其余性能/跨库关注点仍分别处理）；XM-008 → QA-015；XM-010 → QA-020；XM-011 → QA-024。
- XM-002 分别对应 QA-003/023/025/026；XM-004 分别对应 QA-011/012/013；XM-012 分别对应 QA-030/031。不能重复计数，也不能因此假定相同修复策略。

## 下一步修改顺序

1. XM-001：原子准入/所有权，入口级回归；QA-030 可独立小修。
2. QA-013：取消后 tracker 生命周期；QA-004：同步 DB 操作移出事件循环，保留会话并发一致性。
3. QA-001/003/023/025/026：统一生命周期责任与可观察启动结果，按必需/可选组件定义失败策略。
4. XM-006：明确重启生效或实现真实热切换；QA-011：补本地后处理恢复边界。
5. XM-005/QA-012/015/016/024：按 Adapter 和业务契约决定行为，不凭静态差异直接改默认规则。
6. 其余维护债随需求推进；XM-013 在真实适配器早入站条件下补集成验证。

## 复跑与限制

从上述隔离目录执行 `.\.venv312\Scripts\python.exe -m pytest test_audit_reproduction.py -q` 可复跑本轮探针。已有测试范围及结果可由两个 JUnit 文件逐项查阅。

本轮是有针对性的本地复核，不是全仓测试、全前端测试或生产系统压测。QA-001 的真实终止信号与提交损失、XM-013 的真实竞态后果、Adapter 的外部 ACK/重投、外部插件的安全契约和实际镜像部署仍需对应运行环境；这些项目没有被标成“无法复现所以不存在”。
