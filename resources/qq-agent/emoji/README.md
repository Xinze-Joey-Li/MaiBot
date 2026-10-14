# QQ-Agent 表情资源

本目录包含 148 张图片及按 SHA256 索引的评分元数据。图片通过普通 Git 分发（约 497 MiB；公开 fork 不支持新增 LFS 对象），不包含聊天数据库、群标识、主人账号或个人配置。

新机器拉取完整仓库即可获得图片。启动 MaiBot 一次初始化数据库，然后停止机器人，在项目根目录运行：

```powershell
uv run python scripts/import_qq_agent_emojis.py
```

工具先验证图片哈希、备份 SQLite 数据库，然后在事务内按哈希注册。重复导入不会增加重复记录；已有标签合并去重，使用计数、时间记录及手动禁用状态保留。失败时数据库事务回滚，已复制的图片保留供重试；不会调用模型或发送消息。建议仅在机器人停止时运行。

评分自动读取本目录 metadata.json。本地 data/emoji_selection_metadata.json 的同哈希字段优先覆盖仓库字段，因此既有评分偏好保留。仅下载元数据不会自动注册图片，需要执行上述导入。

实际 data/ 继续被 Git 忽略，数据库备份与运行历史不提交。Dashboard 构建产物亦不提交；拉取前端变更后在 dashboard 运行 `bun install --frozen-lockfile` 和 `bun run build`。
