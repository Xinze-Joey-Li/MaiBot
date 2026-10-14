# 可选表情包（148 张）

评分 metadata.json 和下载清单随代码提供，图片不再放在最新版本的 Git 树中。原始图片固定从 v0.2.5 对应提交下载，逐文件核验 SHA256 与大小；约 497 MiB。普通完整克隆仍会下载历史图片，请使用浅克隆或 GitHub 最新分支源码 ZIP：

```powershell
git clone --depth 1 --branch feature/qq-agent https://github.com/Xinze-Joey-Li/MaiBot.git
```

不需要这批表情：无需运行任何工具，机器人可使用自己的表情库，评分文件不会自动注册图片。

需要这批表情：在项目根目录执行以下命令。无参数只显示文件状态，不联网：

```powershell
uv run python scripts/manage_qq_agent_emojis.py
uv run python scripts/manage_qq_agent_emojis.py --download
```

下载可重试，已通过校验的文件直接复用；校验失败不会覆盖现有文件。下载只取得资源，不访问聊天数据库。首次启动 MaiBot 初始化数据库后，停止机器人并选择注册：

```powershell
uv run python scripts/manage_qq_agent_emojis.py --register
# 或一次下载并注册（机器人必须停止）
uv run python scripts/manage_qq_agent_emojis.py --download --register
```

注册前备份数据库，按哈希去重、合并标签，保留已有计数、时间及手动禁用状态。失败时数据库事务回滚，复制的图片保留供重试。不调用模型或发送消息。本地 data/emoji_selection_metadata.json 按字段优先覆盖仓库评分。更新不会删除已下载图片或已注册表情。

图片来源提交必须继续保留；不要删除 v0.2.5 或重写其资源历史。当前完整历史体积未缩减，浅克隆请选最新分支而非 v0.2.5。
