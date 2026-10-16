# QA-013 修复记录

日期：2026-10-17。用户输入 QA-13，按审查台账 QA-013「发送取消时出站跟踪未完成清理」处理。

根因：PlatformIOManager.send_message 在驱动抛出 CancelledError 时先等待回放观察后直接抛出，绕过 finish_tracking；begin_tracking 后的 send_attempt 记录等待、发送后的回执记录等待也缺少统一清理边界。原 pending 只能依赖 TTL 惰性过期。

修复：

- 将发送尝试记录和驱动发送放入同一 try/finally，先同步 finish_tracking，再等待回执记录；取消继续向上传播。
- 驱动结果已确认时，回执记录期间取消仍保留已确认结果；普通传输异常继续保留 UNKNOWN，不伪造失败或触发重发。
- OutboundTracker 将 UNKNOWN 从 pending 移入单独的限时记录，保持同内部消息 ID、同驱动的防重发保护。Broker 再次收到同 ID 时返回已有 UNKNOWN，不增加驱动发送。
- 独立迟到 ACK 可清除 UNKNOWN 并建立平台消息 ID 索引；ACK 先于取消结果到达时不会重新打开 UNKNOWN。无驱动身份且存在多个候选时不猜测归属。
- UNKNOWN 按既有 TTL 惰性清理，clear 同时清空新索引。开启回放时原持久化 unknown/sent 防重发账本继续生效，不依赖内存 TTL 保证跨重启安全。

验证：

- 平台 IO、决策回放及 tests/platform_io/test_manager.py 组合：94 passed。
- 随后补充 ACK 先到竞态和真实回放落库验证，取消测试文件最终 7 passed；合计覆盖 96 个不同主测试。
- 覆盖三个等待点的真实 task.cancel、回执观察异常、UNKNOWN TTL、多驱动隔离、重复提交不重发、迟到 ACK、ACK 先到竞态。
- 开启回放的集成测试使用临时库，验证取消落库 unknown，独立 ACK 回调转为 sent，前后重试都未增加发送次数。
- 修改文件定向 Ruff 与 git diff --check 通过。组合回归有 Starlette、Pydantic 和 SWIG 弃用警告。

未连接真实 QQ、发送平台消息或调用模型；没有验证生产适配器取消语义或真实迟到 ACK 时序。保留已有未提交改动。
