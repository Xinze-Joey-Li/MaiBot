# QQ 群权重测试准备

日期：2026-10-13。分支 feature/qq-agent。机器人尚未启动，无真实 QQ 权重测试结果。

## 当时本地测试设置（历史记录）

- config/bot_config.toml：旧 `reply_trigger_mode="frequency"` 行已注释，实际值为 `dynamic`。
- 群聊 `talk_value=0.5`；私聊 `private_talk_value=1`。时间段规则未启用。
- 当前权重沿用 `ReplyLikelihoodWeights` 原离线拟合系数，运行中不自动学习或更新系数。
- 新消息按权重评分与动态门控额度决定是否进入 Planner；旧消息数量阈值只在重新显式选择 frequency 时执行。
- 独立 @ 回复、自动复读及现有回复分段设置未调整。

## 验证结果

`uv run --no-sync python -m pytest pytests/maisaka/test_reply_likelihood_weights.py pytests/maisaka/test_dynamic_reply_gate.py pytests/maisaka/test_message_turn_scheduler.py -q`：20 passed。

针对改动的 Ruff 和 git diff --check 通过。旧动态门控测试桩缺少消息 timestamp 和 pending_messages 参数，已与既有生产接口对齐；未以兜底掩盖生产异常。

## QQ 实测

用户确认启动后只启动一个 MaiBot 实例；QQ 测试消息由用户发出，不由代理主动群发。

先不 @，分别观察普通群友聊天、带问号的提问、只发媒体、名字提及。每组记录发送时间和是否进入 Planner；日志查 `动态门控`、`概率`、`阈值`、`预计回复`、`目标`、`已回复`、`保留比例`和`权重贡献`。贡献为 logit 加减分，不是概率百分点。

`talk_value=0.5` 是预计回复保留比例，非每两条必回、非 50% 随机抽签。冷启动样本不足 20 批时使用离线阈值表，初始保留比例 0.5 对应约 0.336；随着窗口和实际 reply 次数变化可能收紧。只靠时间流逝不保证唤醒新评分；记录积压与新消息到达。

计数区分：入站消息、实际逻辑回合、内部 Planner 请求、成功 reply、失败工具尝试及实际 QQ 分段。@ 与自动复读另列。最多 3 条分段、一个逻辑回合可连续 reply 的既有行为仍保留，权重测试并非已解决这些连发来源。

拟合的 recent_self_ratio 系数为正，不能把它当成刚回复后冷却；门控的一小时额度反馈可能收紧后续进入 Planner，但不在已启动回合内额外重新门控每次工具调用。

首次启动可能出现旧数据回顾与记忆相关报错，应与新消息权重结果分开记录。尚未验证真实 QQ、运行窗口效果或重启后的完整集成。本次代码随 v0.2.0 发布，备注“上线回复权重模型”；实际 bot_config.toml 被 Git 忽略，不包含在发布中。其他部署需自行将群聊 talk_value 设为 0.5、reply_trigger_mode 设为 dynamic；不要上传带账号或密钥的实际配置。

## 2026-10-14 配置与调用链审查

仓库正式兼容默认值仍为 `frequency`，保留显式 `frequency`、显式 `dynamic` 和旧配置缺少该字段时的升级行为；不会在升级时强制切换权重模式。QQAgent 权重测试及希望采用权重的新部署应显式选择 `dynamic`。此默认选择由既有部署说明及兼容要求确认，不能据此推断用户实际配置错误。

本次只读核实 `G:\QQbot\MaiBot\config\bot_config.toml`：实际字段为 `dynamic`、群聊 `talk_value=0.5`；这证明文件设置，不代表已启动服务的现场行为。未修改或上传该实际配置。

启动和每个会话运行时均输出 `回复触发模式: mode=... 权重评分=... 动态门控=...`。动态分支由 `MessageTurnScheduler` 调用 `DynamicReplyTurnGate.evaluate`，继而调用权重评分和动态额度门；自动测试验证实际模式读取后的调用链，不以始终返回 True 的模式测试桩代替配置读取。频率模式不调用普通消息的权重评分分支。权重系数未修改、未重新拟合；本次仍未启动机器人或发送真实 QQ 测试消息。
