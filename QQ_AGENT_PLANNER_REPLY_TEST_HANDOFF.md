# MaiBot Planner 不回复问题：测试交接

记录日期：2026-10-13。本文供新对话接续测试使用，不表示已经完成真实 QQ 或付费 API 联调。

## 给新对话的任务

请先阅读本文件和仓库 AGENTS.md，基于当前本地代码验证 Planner 回复修复。先检查运行状态，再执行 Mock 回归测试，协助用户完成 QQ 实测并分析日志。不要重新实现本次修复，不要重置、重新克隆、覆盖未提交修改，也不要提交或推送代码。发现新问题时先提供证据，再进行最小必要修复。

仓库路径：`G:\QQbot\MaiBot`；GitHub：`Xinze-Joey-Li/MaiBot`；分支：`feature/qq-agent`。
环境：Windows 11、Python 3.12、NapCat OneBot、MaiBot-SnowLuma-Adapter、DeepSeek Flash / Pro。
机器人名称：蓝色大肥鱼。历史私聊会话：`qq_private_2543365942`。

## 问题与证据边界

- 私聊消息正常接收，回复频率为 100%，可以进入 Planner。
- 模型曾输出“直接调用 reply”，但历史 `output_items` 只有 `AssistantMessageItem`，没有 `FunctionCallItem`，因此没有进入 Replyer 或发送 QQ 消息。
- 原 `_handle_planner_no_tool_retry()` 名称包含“重试”，实际行为是立即结束本轮。
- 原末尾提醒强调输出分析、思考是否调用工具，可能导致模型只写行动计划。这属于最可能的原因判断，尚未用真实 API 联调证明其为唯一原因。
- Dashboard 能力测试明确要求调用工具，与实际 Planner 的决策上下文不同；能力测试通过不等于真实 Planner 必定调用。
- 源码检查和 Mock 测试未发现工具定义传输或原生调用解析丢失。历史预览日志是解析后的结果，不能据此断言 API 原始响应没有工具调用。
- Embedding 未配置造成的表达索引报错独立记录，不作为本问题的直接原因。

历史请求：

```text
logs/maisaka_prompt/planner/qq_private_2543365942/1791845277818.json
logs/maisaka_prompt/planner/qq_private_2543365942/1791845892330.json
```

## 当前修复

1. `src/maisaka/chat_loop_service.py`：末尾提醒改为加载 `maisaka_planner_action_reminder`，明确决定回复时必须实际调用 `reply`，使用真实 `msg_id`；允许无需回复时结束。
2. `src/maisaka/reasoning_engine.py`：只有末句明确声明调用 `reply`、实际无工具调用、回复工具可用等条件同时满足时，进行一次纠错。普通分析、含糊建议、否定、条件句、引用及疑问不触发纠错。
3. 纠错预算属于整个逻辑轮次，不因其他工具执行而重置；本轮调用过 `reply` 后，不会因后续文字计划再触发纠错。纠错仍可选择沉默。
4. 纠错沿用原目标消息与真实上下文；原有新消息中断链路保留。参考提示经过生命周期清理，并在逻辑轮结束时移除。
5. 没有强制回复、绕过频率调度、将分析文本作为聊天正文或修改权重算法。

新增模板位于 `prompts/zh-CN/`、`prompts/en-US/`、`prompts/ja-JP/`，每种语言各有：

```text
maisaka_planner_action_reminder.prompt
maisaka_planner_tool_correction.prompt
```

新增测试：`pytests/maisaka/test_planner_tool_correction.py`、`pytests/test_openai_planner_tool_roundtrip.py`。更新日志位于 `changelogs/changelog.md`。

## 必须保留的本地工作

本次修复尚未提交。以下文件包含另一项权重任务的原有未提交修改，不属于本次修复，不要覆盖、清理或回滚：

```text
src/maisaka/turn_trigger/gates.py
src/maisaka/turn_trigger/reply_likelihood.py
pytests/maisaka/test_reply_likelihood_weights.py
```

`changelogs/changelog.md` 同时有原有权重记录和本次修复记录，需要保留两者。

## 第一阶段：本地 Mock 回归

在仓库根目录运行，使用当前项目虚拟环境，不自动同步或升级依赖：

```powershell
Set-Location -LiteralPath 'G:\QQbot\MaiBot'
git status --short
uv run --no-sync python --version
uv run --no-sync python -m pytest pytests/maisaka/test_planner_tool_correction.py pytests/test_openai_planner_tool_roundtrip.py pytests/maisaka/test_chat_loop_day_boundary.py pytests/test_openai_length_truncation.py pytests/maisaka/test_planner_messages.py -q
uv run --no-sync ruff check src/maisaka/chat_loop_service.py src/maisaka/reasoning_engine.py pytests/maisaka/test_planner_tool_correction.py pytests/test_openai_planner_tool_roundtrip.py
git diff --check
```

本次已完成的结果：43 项测试通过；Ruff 和 diff 检查通过。这些测试不访问真实 QQ 或付费模型。已有 Pydantic / SWIG 弃用警告不是测试失败。

另一个已有测试文件 `pytests/maisaka/test_reasoning_engine.py` 在修复前已有两项失败：`test_successful_stop_request_finishes_after_full_tool_batch` 和 `test_failed_stop_request_keeps_planner_running` 调用 `_handle_tool_calls()` 时缺少 `cycle_detail`、`state` 参数。其余九项通过。不要将这两项直接归因于本次修复，也不要借机修改无关实现。

## 第二阶段：真实 QQ 验证

先确认 MaiBot、NapCat、适配器各自的运行状态与启动方式。确保运行的是本仓库的新代码；需要时只重启 MaiBot，不重复启动多个实例。不要修改现有配置、打印 API Key 或在日志汇报中泄露无关聊天记录。

真实消息由用户在 QQ 中发送；下面是测试用例，不表示授权代理自行向 QQ 用户或群发送消息。

| 用例 | 操作 | 检查结果 |
| --- | --- | --- |
| 私聊正常回复 | 发送“大肥鱼在吗”，等待本轮结束后再发送“考考你，1+1等于几” | 接收→频率调度→Planner；当模型决定回复时，有实际 `reply` 调用、Replyer 执行及 QQ 可见消息 |
| 连续追问 | 在没有收到回复时发送“到底在不在啊” | 检查模型是否仍仅输出计划；若发生明确工具遗漏，最多纠错一次，不无限思考或重复发送 |
| 群聊旁观 | 测试群成员正常互相聊天，不 @ 机器人，也不特意要求机器人回复 | 无需回复可以直接结束；普通分析不会因无工具输出而自动纠错。不要把所有群消息均不回复作为硬性预期 |
| 模型对照 | 条件允许时分别验证已配置的 Flash / Pro | 记录实际请求模型名及每次结果；不要为了测试擅自修改配置文件或增加付费请求 |

本次目标是普通 Planner 链路。实际 @ 机器人会走已有独立优先回复机制，不能用它证明本次修复有效。

## 如何判定纠错正确

出现自然复现时，日志应出现一次：

```text
Planner 声明调用 reply 但未调用，进行一次纠错
```

对应轮结束原因是 `planner_no_tool_retry`。下一次请求中应保留真实聊天内容、原目标消息及纠错提醒。后续允许两种结果：实际调用 `reply` 并发送，或模型重新判断无需回复并结束。第二次仍只有文字计划也必须结束，不能再次纠错。

如果新提醒让模型第一遍就正常调用，没有纠错日志属于正常情况。不要为了制造纠错而破坏真实工具定义或修改生产 Prompt；遗漏调用、群聊静默和一次重试上限已有 Mock 覆盖。

## 日志收集与后续定位

每个用例记录测试时间、聊天类型、发送内容、实际模型名、是否调用工具、是否纠错、QQ 是否收到回复。找到对应的新 Planner JSON，检查 `request_items`、`tool_definitions`、`output_items`、`generation_attempts`，不要误读上面的旧请求。

- 仍只有文本：检查末尾是否为新提醒、是否符合保守纠错条件、纠错预算及内部轮次上限；仅写“应该回复一下”不会触发纠错。
- 有 `FunctionCallItem` 但未执行：检查响应 Hook 与 `_handle_planner_response_actions()`；如需确认 API 原始响应，单独获取证据，避免从预览结果反推。
- 已进入 `reply` / Replyer，但 QQ 无消息：继续检查 Replyer 错误、发送服务、适配器和 NapCat 返回信息，区分工具遗漏与发送失败。
- 无需回复时结束：属于允许行为，不要通过强制 `tool_choice` 或自动发送分析文本改变自主决策。

最终汇报 Mock 结果、每个真实用例、对应日志证据、是否有重复回复或额外纠错、未验证的部分。未经用户另行要求，不提交、不推送。

## 新对话可复制的开场消息

> 请阅读 `G:\QQbot\MaiBot\QQ_AGENT_PLANNER_REPLY_TEST_HANDOFF.md`，接续验证 Planner 不回复问题的修复。先检查当前代码和运行状态并执行文档中的 Mock 测试，再协助我在 QQ 中实测和分析日志。保留全部未提交修改，尤其是 `src/maisaka/turn_trigger/` 的其他任务改动；不要重置、重复实现、提交或推送。
