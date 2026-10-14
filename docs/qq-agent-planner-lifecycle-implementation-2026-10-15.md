# Planner 生命周期本地优化交付

## 实现与文件

- src/maisaka/planner_lifecycle.py：纯本地结果判定和三模式读取。
- src/maisaka/builtin_tool/reply.py：完整发送、附件处理和回执记录结束后，标记任务已完成且无需 Planner 解读普通回执；失败返回不声明完成。
- src/maisaka/reasoning_engine.py：收集本批真实执行结果，整批动作执行后判断终止；记录群/会话、逻辑回合、cycle、轮次、工具名与成功状态、原因、待处理消息及实际输入输出 Token。优化模式禁止下一内部轮次消费新消息，并在正常收尾后重新门控。
- src/maisaka/runtime.py：优化模式不因新消息打断旧 Planner 并直接合并重试，消息仍经过既有缓存接收和去重。
- src/maisaka/turn_trigger/scheduler.py：优化模式运行中的旧回合不为新消息提前入队；停止后原门控正常评估。
- src/config/official_configs.py、src/config/config.py：新增配置字段，模板生成版本 8.14.74；无新升级 hook，不改 legacy_migration。
- pytests/maisaka/test_planner_lifecycle.py：新增本地结束、未知结果、失败、明确续链、多动作真实执行批次、缓存不消费、群隔离、重复调度、重新门控、三模式及中断隔离测试。
- pytests/maisaka/test_reasoning_engine.py：更新旧工具批次测试夹具以适配已有参数签名和消息别名解析，保留原断言。
- changelogs/changelog.md：记录功能。

## 原问题与结束条件

原逻辑在 reply 工具返回后统一进入 tool_continue，下一轮由 LLM 再决定是否沉默；后续内部轮次直接消费旧回合期间新消息，没有经过新回合的外部门控。

新的结果契约使用 ToolExecutionResult.metadata：只有本批所有结果 success=True、planner_task_completed=True、planner_continuation_required=False，且没有 post_history_messages 或 stop_after_execution 时，允许本地结束。未声明、失败、需分析和显式续链均保守继续。判断不依赖工具名称，不解析 Prompt 或自然语言计划，不额外调用 LLM。

目前仅完整成功的普通 reply 声明这两个标记。查询、检索及插件默认未知，继续原工具链；插件若要声明完成，必须按该契约确保无未完成必要操作。原显式 wait/暂停/终止工具优先执行其原有路径。

所有当前响应的工具按原执行器依次完成才判定，不在第一条 reply 处 break。既有执行器 wait 的暂停语义保持原样。

## 消息安全

新消息仍在原会话 message_cache 中；旧续轮不 collect，不推进 _last_processed_index，不把消息读入旧回合。工具链使用原已消费消息继续。优化模式的新消息不会中断旧 Planner 再走直接重试链路。

finally 完成状态清理、结束 continuation 后，对尚未消费消息仅调用一次原 scheduler。门控放行才 enqueue，拒绝时不消费、不强制放行、不自旋；后续真实新消息或既有延迟检查可以再次评估。每群使用自己的 runtime、消息游标与调度器，没有全局共享回合状态。

## 历史追加回复、wait 和节省

三次成功追加 reply 分别是“记住我了？”接话、另一群成员新发“大肥鱼尾巴”调侃、对“设为精华”后笑翻反馈的补刀。均针对回合期间新的互动，并非完成同一条发送任务必需的剩余分段。优化后可由新的外部门控回合决定，可能延迟或不回复；不承诺复现旧轨迹。

三次续轮 wait：两次成功 reply 后等待对方（60 秒、30 秒），一次失败 reply 后等待。失败保留恢复续轮及 wait；显式首轮安排 wait 或混合工具中的 wait 不会被本地终止覆盖。成功 reply 后不再为“等待对方”专门请求一次 LLM，新消息继续在缓存等待正常调度。

33 次历史拟省略请求对应 27 次沉默、3 次成功追加 reply、2 次 wait、1 次失败 reply；其中 27 次沉默包括失败续轮后那次第 3 轮。全样本另有 2 次失败首轮之后的沉默、1 次失败首轮之后的 wait，不包含在这 33 次中。

历史潜力为 229,521 输入、4,492 输出 Token，其中输入命中 113,280、未命中 116,241。是旧轨迹反事实上限，包含有用动作，后续消息重新触发也会产生消耗，不等于实际节省金额。

## 配置与回退

配置归属为 bot_config.toml 的 [chat.reply_timing]：

```toml
[chat.reply_timing]
planner_lifecycle_mode = "observe"
```

- legacy（默认）：保留旧结束与新消息合并/打断行为。
- observe：同样保留旧行为，只记录本地会不会结束。
- optimized：启用本地结束和新消息重新门控。

建议在停机时设置并重启，避免在工具执行中间切换生命周期语义。回退设置 legacy 后重启。未知模式由配置 Literal 验证拒绝。没有手工修改实际配置来启用优化，机器人保持关闭。

## 验证与剩余风险

131 项相关 pytest、19 个子测试全部通过，修改代码及测试 Ruff 全部通过；覆盖实际工具批次执行、本地决策与真实响应处理分支、调度放行/拒绝、隔离、去重、原有纠错与整轮循环、独立 @、表情历史和动态门控。测试使用 Mock 模型，无新增付费请求。

尚未完成真实 QQ 压测、任意第三方插件必要操作的现场验证以及热切换中途的语义验证，因此按要求保留 legacy 默认，不擅自启用生产 optimized。未知结果均继续，风险主要是对新消息及时响应的行为变化及长期运行调度时序，不能用单元测试宣称现场完全无风险。

人格、权重算法、Replyer 提示词和表情概率决策均未在本任务改动。已有 Token 缓存优化和前三批修改保留；未提交或推送。
