# QQ Agent 第二批 Bug 修复验收

已获取远端 `origin/feature/qq-agent`，最新仍为 `3d1b22c336e54c2bd06c060d61acdf5532e0bbfb`。本批实际基于本地第一批独立提交 `3bd52514242899569a278c0c5f326720273a0d7e` 追加，第一批代码与回归测试保留。仅处理第 5、7、8 项；第 6 项按要求暂不处理，不处理第三批。

## 并发控制（第 5 项）

`PriorityAtReplyService` 默认全局最多 8 个、每会话最多 2 个在途任务。等待队列全局最多 128 条、每会话最多 32 条。仅有运行槽位时才创建 asyncio.Task，排队项保留独立历史快照，不加入普通 Planner 队列或占普通发言额度。满额会话的排队项可以被跳过，给其它会话留出槽位。

正常流量下接纳的消息全部被处理。超额消息返回 False 并明确记录 session_id、message_id、运行和排队数量，拒绝创建更多任务；拒绝项不写入完成去重缓存，平台重投时可重新接纳。不额外发送过载提示或调用模型，极端过载拒绝项不保证得到聊天回复。

排队、运行和完成阶段均去重。异常或取消释放槽位；关闭先停止接纳和派发、清空等待队列，再取消并等待在途任务，不会在关闭期间启动新的排队项。保留第一批表情失败后只降级一次文字回复的机制。

## 独立标签对（第 7 项）

不再要求同一新标签对所有旧标签采用同一动作。每个新旧标签对单独校验引用、动作和 revision，重复对拒绝。新标签受到任一 keep_old 则最终不生效，存在 uncertain 则不作为有效事实召回；因此被 B 否定的新标签不能同时淘汰 A。replace_old 与 uncertain 混合时保留存疑状态，不产生两份相互矛盾的有效事实。

先聚合全部标签状态，再在同一事务内统一更新旧标签、新标签和证据，结果不依赖裁决顺序。一个旧事实同时被多个仍有效的新标签替代，仍因新标签间关系未经判断而拒绝；合法的 replace_old/keep_old 不同标签对则允许。每对模型理由、双方来源及最终应用状态都保留。

人工修改一个标签对会重新计算关联关系，不让单次修改覆盖其它合法裁决。后续批次更新旧事实时，整条旧连通裁决链失效，旧网页不能恢复过时关系。过期 revision 或后续校验失败时，整批事务回滚。

## 表情附件与动态权重（第 8 项）

文字后的表情以 `reply_emoji_attachment` 来源同步历史，并在发送前写入持久化 additional_config 标记。runtime 和 reasoning engine 识别该来源并保留附件在聊天上下文中；重启恢复仍能识别。动态门控忽略该来源的发言次数、近期消息总数和最后正常发言时间。

真正的普通模型文字回复仍按原有 guided_reply 规则计数；独立 @、暖群和复读的既有统计豁免保持不变。未改任何动态权重系数或门控计算公式。旧记录若没有来源标记，不通过图片类型猜测归属。

## 修改文件

实现文件：`src/services/priority_at_reply_service.py`、`src/services/tag_conflict_service.py`、`src/services/tag_memory_store.py`、`src/services/send_service.py`、`src/emoji_system/reply_attachment.py`、`src/maisaka/runtime.py`、`src/maisaka/reasoning_engine.py`、`src/maisaka/turn_trigger/gates.py`。

新增回归：`pytests/maisaka/test_priority_at_concurrency.py`、`pytests/maisaka/test_reply_attachment_history.py`、`pytests/services/test_mixed_tag_conflicts.py`。更新 `pytests/maisaka/test_reply_emoji_tags.py`，明确附件来源断言。未删除任何第一批测试。另更新 `changelogs/changelog.md` 和本文档。

## 验证与限制

- 198 项相关 pytest 及 14 项子测试通过，涵盖第一批完整修复回归、密集及突发 @、全局/单群并发、队列过载、其它群推进、排队去重、快照、任务失败后继续派发和关闭取消。
- 事务及冲突测试覆盖六种混合动作组合、每对证据、重复对、多个有效替代者、过期 revision 整批回滚、人工修改关联状态与过时裁决链不可恢复。
- 表情测试覆盖单次文字加附件与只有文字统计一致、多次独立文字仍计数、附件不更新时间、持久化来源、重启恢复。群冷却和静默表情测试仍通过。
- Ruff 和 git diff --check 通过。验证使用与第一批相同的 uv 环境；模型/发送使用模拟对象，标签状态使用真实临时 SQLite。
- 并发压力测试仅验证程序限额和队列行为，没有运行真实 QQ/DeepSeek 资源压力测试；默认容量需结合实际服务能力观察。明确过载拒绝策略属于极端流量边界，不承诺无限积压。
- 不部署、不推送；原工作区未提交改动保留。第二批以单独 commit 追加在第一批之后。
