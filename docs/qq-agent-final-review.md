# QQ Agent 三批最终代码审查与修复记录

日期：2026-10-14。原始静态审查基准及远端最后核实提交为 `3d1b22c336e54c2bd06c060d61acdf5532e0bbfb`（v0.2.2）。修复在 `codex/qq-agent-batch-one-fixes` 隔离分支依次追加，原 `G:\QQbot\MaiBot` 工作区的未提交改动未混入三批提交。未推送、发布、部署或发送真实 QQ 群测试消息。

## Commit 顺序

1. 第一批：`3bd52514242899569a278c0c5f326720273a0d7e`，修复第一批 QQ Agent 回复与标签记忆问题。表情失败降级一次文字、已送达后不重复回复、两秒标签召回隔离、有限重试与持久化隔离、数据库锁定/重启复用输出、每轮冲突请求预算与续跑。
2. 第二批：`eaef072dcce2b3445dae9d9e76a59c9c05ef8da0`，修复并发、标签冲突与表情权重问题。全局 8/单会话 2 在途任务、有界 128/32 等待队列；独立标签对与事务状态聚合；表情附件保留历史但不计入普通发言统计。
3. 第三批：本报告所在提交，说明为“修复第三批触发模式可观测性与适配器补丁格式”。保留既定兼容策略并修复确定的补丁应用故障；最终完整哈希另记录于关机前状态文件和聊天最终回复。

第二批继承第一批，本批继承第二批，未回退前两批代码或删除原测试。第 6 项分段回复概率按要求暂不处理。未修改动态权重系数及门控公式、实际 bot/model 配置、legacy_migration 或 A_Memorix 实现。

## 第三批确认与设计选择

### 默认模式与实际配置

交接文档原本明确要求其它部署显式选择 dynamic，因此保留正式兼容默认 frequency：新默认模板及缺字段旧配置仍 frequency，明确选择 frequency/dynamic 的配置升级后均保持原选择。只澄清配置字段说明，没有改变字段默认值、增加升级 Hook 或强制切换。

新增 `log_reply_trigger_mode`，系统启动及会话运行时输出实际 `mode`、权重评分和动态门控状态。自动测试使用真实 mode_policy 配置读取，验证 dynamic 路径确实调用权重评分及动态门控，frequency 普通消息路径不调用两者；没有用固定 True 的模式桩替代实际配置读取。

只读核实原目录实际配置：`reply_trigger_mode=dynamic`、群聊 `talk_value=0.5`。这是文件设置核实，不是正在运行服务的现场观测，不能把仓库默认 frequency 当成用户配置错误。本次未启动机器人。交接文档区分历史测试设置、仓库兼容默认和本次文件核实。

### 适配器版本和补丁

`adapter-patches/MaiBot-SnowLuma-Adapter/README.md` 原本明确写明 0.1.1 是“按本次要求”的本地定制版本，实际本地适配器也保持该值。这不是无依据误写，故未改成 1.1.6，也未修改上游版本比较。

Host 接受合法的 0.1.1，但语义版本比较把 1.1.5 判为更高；从上游更新插件可能覆盖定制回戳，需再次核对基线及补丁。该兼容性边界已写入补丁 README，未擅自修改自动更新机制。

发现并修复确定故障：统一 diff 中五处空白上下文行缺少前导空格，原文件报 `corrupt patch ...:7`。只恢复五个格式空格，版本字段、回戳代码及原功能不变。

完整基线验证来自 https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter 的 `921413c917efd029c2cea5e4889cb06b07098bb1`（1.1.5），保存在独立验证目录 `C:\Users\E'l'y'si'a\.codex\worktrees\qq-agent-adapter-validation`：

- `git apply --check`、实际 apply、反向 `--check` 均通过。
- 应用后 CHANGELOG、README、manifest、config.py、runtime/router.py 五个文件，统一 LF 后与原目录本地定制文件 SHA-256 一致。
- config.py、runtime/router.py 编译和 Ruff 通过。
- Manifest 使用实际检测到的 SDK 2.10.0、Host 1.3.5 校验通过，无错误或警告；声明 SDK 下限为 2.5.0。
- 离线补丁测试重建 hunk 上下文并用真实 Git 验证应用，执行补丁中的实际回戳方法；覆盖群/私聊、重复事件、自身回显、其它目标、开关及失败不重试。完整基线应用是另外的手动集成验证，未用离线上下文替代。

验证时曾在适配器根目录运行 Python，遇到本仓库 types.py 遮蔽标准库 types；随后从 Host 目录对绝对路径编译成功。曾以假定 SDK 2.0.0 检查 Manifest 被下限拒绝，改用实际检测 SDK 2.10.0 后通过。这两次验证命令问题没有导致适配器代码或版本修改。

## 自动验证结果及明确保留的问题

- 三批联合相关 suite 完整运行：301 passed、4 failed、14 subtests passed。
- 补充配置 Schema 与第三批新测试：34 passed、1 failed（其中 15 个第三批新测试与上轮重复）。第三批新测试单独运行：模式配置 6 passed、补丁 9 passed。
- 五项失败均已在第二批 `eaef072dc` 的干净独立工作区、同一 pyproject/uv 环境中复现，不属于本批新引入；不得宣称全绿。

保留的失败：

1. `test_startup_bindings_use_defaults_when_config_file_missing`。
2. `test_startup_bindings_can_read_addresses_from_bot_config`。
3. `test_resolve_bindings_prefer_initialized_global_config`。

以上三项仍构造 BindAddress(host=...)，实际实现已是 hosts 列表；默认 WebUI 绑定也已含 IPv6。没有为使测试通过而回退该接口。

4. `test_legacy_env_bindings_are_migrated_when_fields_missing_or_default`：断言期待旧 8080 默认端口被 PORT 覆盖，而现有固定 legacy_migration 使用 8000 判断默认值。本批禁止修改 legacy_migration，未扩展到该范围外行为。
5. `test_debug_schema_exposes_clear_context_command_switch`：仍期待已不存在的 enable_clear_context_command 字段，生成器找不到而 StopIteration。未恢复无关旧功能或删除该测试。

第二批基线定向结果：startup_bindings 四失败一通过；Debug Schema 单项失败。基线 worktree 用于确认完毕后可归档，其证据已保存在此报告。失败详细堆栈也保留于本次聊天工具输出。

三批涉及的 Python 实现和新增/更新测试 Ruff 均通过。普通源文件 diff 检查通过；.patch 文件检查允许统一 diff 必需的空白上下文前导空格，应用后的源码按正常规则 diff 检查通过。

## 未验证风险与完成边界

- 三批功能没有发现交叉回退：第一批降级、召回隔离、缓存与预算，第二批并发限制、混合裁决及附件来源，第三批模式读取与日志均由联合回归覆盖。
- 真实 QQ/DeepSeek 行为、运行窗口效果、实际资源压力和部署重启仍未验证；本次不发布、部署或发群消息。
- 默认 frequency 和定制版本 0.1.1 是保留的设计选择；上游自动更新保留回戳并无保证。
- @ 队列极端过载会明确拒绝入队；网络回执未知时端到端严格恰好一次依赖平台能力；已有模型输出持久化依赖可写磁盘。详见前两批验收文档。
- 五项存量测试失败及暂缓的第 6 项仍需后续单独决定，不在本批擅自处理。
- 按用户要求，保存此报告、第三批 commit 及关机状态记录后执行关机；没有通过关机掩盖失败结果。

## 第三批修改文件

`src/config/official_configs.py`（说明）、`src/maisaka/mode_policy.py`、`src/main.py`、`src/maisaka/runtime.py`、`QQ_GROUP_WEIGHT_TEST_HANDOFF.md`、补丁 README 和 v0.1.1.patch、两个新回归文件、`changelogs/changelog.md` 和本报告。未修改实际适配器工作区文件。
