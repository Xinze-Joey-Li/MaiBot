# XM-001 修复记录

日期：2026-10-17。

根因：Receiver 的 claim 在独立服务准入前写入 priority_at_handled；Runtime 因此只保存历史，后续 start_reply 在关闭或满载时拒绝，普通 Planner 已失去本次处理资格。

修复范围：遍历 src 中 claim/start_reply、priority_at_handled 的生产引用，覆盖 Receiver、PriorityAtReplyService、Maisaka Runtime，并核对普通 reply 工具的防重复判断。

- claim 只标记 priority_at_requested 候选，不标记已接管。
- Runtime 先构建包含当前消息的独立上下文快照，再同步准入；准入成功后提交历史和 handled 标记，中间不 await。
- 总队列满、单会话队列满、服务关闭导致拒绝时，继续本次普通消息注册、触发状态更新和原有调度门控。回到普通调度不承诺一定回复，仍由原有门控和 Planner 决定。
- 已运行、排队、近期完成的独立消息仍按原去重窗口归独立服务；构建上下文期间并发重投也只接纳一次。
- 普通 reply 工具继续依据真正的 handled 标记阻止重复回复，拒绝项不再被该判断拦截。

验证：独立 @、并发队列、入口级集成、Planner 生命周期与 decision_replay 回归组合 108 passed，另有 19 subtests passed；随后新增上下文并发重投测试，入口文件 4 passed。共 109 个不同主测试通过。Ruff 定向检查及 git diff --check 通过。运行有 Pydantic、SWIG、Starlette 弃用警告。

入口集成测试执行真实 Receiver、Runtime.register_message 和独立服务；外部存储、模型发送、后台服务等使用 mock，回放使用临时数据库。没有启动真实 QQ、发测试消息或调用模型。不包含生产负载和真实平台送达验证。
