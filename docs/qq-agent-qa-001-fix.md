# QA-001 修复记录

日期：2026-10-17。本轮仅修改本地源码、测试和说明，未提交或推送，未操作运行中的机器人。

## 根因与修复目标

Worker、src.main 模块入口与 WebUI restart 使用不同清理列表；Worker 和 WebUI 的硬退出可能早于线程内 SQLite 事务或 daemon 回放队列完成。asyncio 包装任务取消不能作为线程工作完成的证明。

## 实现

新增 src/common/runtime_shutdown.py，MainSystem 注册主任务及周期任务，所有入口复用同一个受 shield 保护的关停任务。关闭过程先标记全局停止接收，停止启动/调度主任务及网络、周期生产者，等待已验收的 Broker 入站分发，再停止 Planner 并处理各服务：

- WebUI 在关停流程中停止接收请求，进程关停使用 timeout=None 等待请求及 WebUI 线程结束；该线程清理时也等待自己的 default executor，覆盖其 SQLite to_thread 工作。
- 人物事实和旧摘要写回先拒绝新入队，等待已接纳的队列完成；worker 提前退出或缺失而队列尚未排空时显式报错，避免无限 join 或静默丢弃。
- 独立 @ 新增显式 drain 模式，关停时拒绝新接纳，依原全局/单会话并发限制完成已有队列。原默认取消模式保留给已有调用；统一进程关停使用 drain=True。
- 标签及图片的持久化任务可以取消后重启续跑；标签服务继续使用其已有 shutdown 等待受保护 I/O。先完成图片描述及内存写回，再关闭记忆内核和插件等依赖。
- 剩余后台任务取消后，等待主循环 default executor 完全结束，再用独立辅助线程关闭 ReplayStore；ReplayStore.close 拒绝新观察事件、排完队列并 join daemon writer。不能再仅依赖 os._exit 不会执行的 atexit 回调。
- 普通清理异常或步骤意外取消会记录完整错误并继续后续清理，最后抛出 ExceptionGroup。不会记录假的“全部完成”；记忆自动化内部也会尝试清理全部子队列。
- 排除关停调用方与主任务本身，避免模块入口 finally 重新等待同一关停任务时死锁；已在取消中的任务不重复 cancel，以免打断其 finally 清理。

入口修改：

- bot.py 的正常返回、取消、异常和 SystemExit 均经过统一关闭，关闭 loop 前等待 async generators/default executor；清理失败不会以正常重启成功状态退出。
- WebUI restart 不再直接 os._exit；主循环保存退出码 42，主入口完成关停后返回该退出码，由 Runner 重启。
- Runner Windows 子进程使用独立进程组，首次 Ctrl+C 转发 CTRL_BREAK_EVENT（非 Windows 使用 SIGINT）并等待，取消先 terminate 再等 5 秒的行为。用户再次中断才明确强杀。Worker 增加 SIGTERM / Windows SIGBREAK 处理。
- 新入站在 shutdown 标记后拒绝；已接纳的 Broker 后台分发先完成。正常运行时的 XM-005 有界去重/异步验收语义不变。

## 验证

新增 pytests/test_runtime_shutdown.py，最终 **21 passed**，覆盖：

- 多次/并发关停复用同一任务，步骤顺序与异常/取消传播；调用方取消不中断清理。
- 人物事实和旧摘要队列积压排空、拒绝后续入队，死 worker 显式报错。
- 独立 @ 积压按原并发限制完成，关闭中拒绝新任务。
- Broker 停止新接纳但等待此前已接纳的分发。
- Runner Windows/Linux 首次请求优雅退出与显式二次中断；进程对象为 mock，未向真实机器人发送信号。
- Worker 与 WebUI 重启委托统一关停入口。
- WebUI 独立线程等待其真实 SQLite executor；两个真实事件循环间的重启取消不会中断主循环清理。
- 主任务取消进入 finally 后重新等待同一关闭任务，不死锁。
- 独立测试子进程使用真实 coordinator、executor、SQLite、ReplayStore，取消写入的 asyncio 包装任务后仍等待线程事务提交；退出码 42 后重开 SQLite，提交数据与回放事件均保留，integrity_check=ok。

扩展验证按批次记录，不重复相加为总数：

1. 启动、会话、独立 @、标签、平台 IO、决策回放回归组合：236 passed，另有 19 个 subtests（当时包含 16 项关停专项）。
2. 最后补充的关停/记忆写回/WebUI/启动组合：82 passed、1 skipped（当时包含 19 项关停专项）；Windows 符号链接权限限制的既有测试跳过。
3. 最终新增的步骤取消与主任务重入检查后，关停专项完整 21 passed。
4. 所有修改 Python 文件定向 Ruff 和 git diff --check 通过。格式化仅限两个新文件及小型 shutdown 状态文件，没有全仓格式整理。

另有一项既有旧摘要集成测试失败：test_text_to_stream_triggers_real_chat_summary_writeback。测试启用旧摘要写回，却没有关闭新标签系统；当前配置及 v0.3.1.1 的既有逻辑均在标签开启时抑制旧摘要。未改动该测试或产品开关。只在仓库外诊断插件中将该测试的 tag_memory.enabled 设为 False，原测试通过。原样运行的失败不能计为通过。诊断脚本位于 Downloads/MaiBot-QA001-validation-20261017/diagnose_legacy_summary.py。

## 边界与代价

没有进行真实 QQ、真实供应商、实际控制台信号或生产负载验证；所有新增数据库数据来自临时目录，外部插件、模型和网络依赖为 stub/mock。未修改 A_memorix 实现、真实配置或迁移策略。

优雅退出会等待已有内存任务和在途数据库工作，积压或外部调用较慢时退出时间会变长，不再以短超时硬杀来宣称完成。用户二次中断、操作系统强杀、断电以及原有业务处理失败仍无法保证所有工作完成；已持久化任务沿原租约/重试机制恢复。本修复不承诺物理故障下的全局 exactly-once。
