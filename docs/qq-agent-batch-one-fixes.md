# QQ Agent 第一批 Bug 修复验收

基准及已获取的远端分支：`3d1b22c336e54c2bd06c060d61acdf5532e0bbfb`（v0.2.2）。四项问题均仍存在。修复在独立工作区中基于该提交进行；原目录未提交的标签记忆、WebUI 等改动未纳入本批。

## 修复范围

1. `src/services/priority_at_reply_service.py`：保留单独 @ 的 50% 表情/文字分支。固定表情缺配置、不可用、读文件失败、明确发送失败后只调用一次文字回复；表情成功后直接返回，文字流程自身的异常继续暴露。
2. `src/services/send_service.py`：平台已确认送达后，历史保存、数据库或 Hook 错误仍记录异常并保留成功回执，避免表情已发出却再降级发送文字。
3. `src/services/tag_memory_recall.py`：共用入口内部隔离召回异常并设置两秒超时；Planner、Replyer 无需重复包裹整段主回复流程，取消信号保持传播。
4. `src/services/tag_extraction_service.py`、`src/services/tag_memory_store.py`：最多五次失败，60/120/240/480 秒指数退避；隔离任务保留冻结消息与缓存，同群新消息可创建后续任务。模型输出先写原子持久化文件，再写 SQLite，写锁与重启不导致重复生成已成功的输出。失败计数也持久化在 SQLite 锁之外。
5. `src/services/tag_conflict_service.py`：先按分类筛选，再用名称、摘要词汇相关性优先检查；每次任务调度最多四次模型请求，未完成项保留在检查点，下一轮继续。低相似项仍检查，避免同义词、改名造成遗漏。全量完成后再校验裁决一致性并提交证据，记录请求数和 Token 用量。
6. `src/webui/routers/tag_memory.py`：提供带认证和群归属校验的任务查询及人工恢复接口。恢复旧任务保留原消息证据，不伪造与已提交后续批次的相邻话题关系。
7. 标签数据库迁移至 schema 7，事务迁移已有任务，取消每群只能保留一个失败任务的唯一限制。未修改实际 bot/model 配置、legacy_migration 或 A_Memorix。

## 人工恢复

- `GET /api/webui/tag-memory/{session_id}/extraction-jobs`：查看任务状态、失败次数、重试时间和错误。
- `POST /api/webui/tag-memory/{session_id}/extraction-jobs/{job_id}/retry`：仅恢复该真实群会话的隔离任务，重置失败次数，保留有效模型缓存。接口沿用 WebUI 认证；未增加前端页面。
- 失败任务的消息只在该任务成功提交时消费，不放回新任务重复提取。

## 修改文件补充

除上述七个实现文件外，本批修改六个回归测试文件：`pytests/maisaka/test_priority_at_reply.py`、`pytests/maisaka/test_reply_emoji_tags.py`、`pytests/services/test_batch_one_memory.py`、`pytests/services/test_tag_conflicts.py`、`pytests/services/test_tag_extraction.py`、`pytests/webui/test_tag_memory_routes.py`；另更新 `changelogs/changelog.md` 和本验收文档，共 15 个文件。

## 验证

- 受影响 pytest：150 项及 14 项子测试通过。
- 最终数据库迁移及故障恢复修改后，39 项定向测试通过。
- 覆盖默认无表情配置、读图与发送失败、成功不重复回复、主要文字错误传播、Planner/Replyer 数据库异常和超时、取消、五次持续模型失败、指数退避、隔离及同群推进、跨群恢复拒绝、重启恢复、真实 SQLite WAL 写锁后的模型结果复用、冲突版本和证据校验。
- 10 个新标签对 200 个历史标签：25 轮内完成全部 2,000 个比较对，共 100 次模型请求，每轮最多四次，没有直接丢弃预算外检查；该机制限制单轮调用峰值，不承诺减少完成所有必要比较的累计请求数。
- 使用与基准相同的 pyproject/uv 依赖环境；模型和 QQ 发送使用模拟对象，数据库与 HTTP 认证/归属检查使用真实临时 SQLite/FastAPI。
- Ruff 与 git diff --check 通过。已有第三方弃用提示不属于本批问题。

## 遗留风险及上线注意

- 未运行真实 QQ/DeepSeek 服务，未部署；应在线验证一次成功表情、一次表情不可用的文字回复和一次隔离任务恢复。
- 平台已确认送达可以防止发送后处理故障造成重复；网络中断造成平台送达状态未知时，端到端严格恰好一次仍取决于平台回执或幂等能力。
- SQLite 的同步查询放在线程中；两秒召回超时不会阻塞事件循环，但已经开始的线程查询仍可能运行至自身 SQLite 超时。
- 备份和迁移记忆库时应一并保留 `memory_index.db.job-cache`（实际名称跟随数据库文件名）。成功任务会删除缓存，隔离任务的消息与输出保留到人工恢复；磁盘也无法写入时无法保证崩溃前的结果落盘。
- 历史标签变更会使旧冲突检查点失效并重新校验；大历史库完成全量必要检查仍可能较慢。

仅完成第一批，不继续第二批。代码提交与当前部署分离。
