# 戳一戳功能变更为直接回戳 — v0.1.1

本目录保存 MaiBot-SnowLuma-Adapter 的完整改动补丁。适配器是独立仓库，因此补丁随 MaiBot fork 分发，不改变主程序，也不提交插件本地配置、聊天数据或密钥。

适用基线：Mai-with-u/MaiBot-SnowLuma-Adapter，提交 `921413c917efd029c2cea5e4889cb06b07098bb1`（原版本 1.1.5）。补丁内插件版本按本次要求设为 0.1.1，是本地定制版本号。

## 行为

- 群聊和私聊收到用户戳当前机器人账号的事件时，直接回戳对应用户。
- 每个事件独立处理，不设置本地限流；平台冷却由 QQ 控制。
- 消费已自动处理的事件和自身回戳回显，不调用模型或触发 Planner 文字回复。
- `notice.auto_poke_back` 默认开启；关闭该开关恢复原通知转发行为，关闭 `notice.enabled` 也停用回戳。
- 其他人之间的戳一戳保留原通知转发行为，发送失败正常暴露错误，不自动重试。

## 应用补丁

先停止 MaiBot。确保适配器基线正确且没有冲突修改，在 MaiBot 根目录运行 PowerShell：

```powershell
$pokePatchPath = (Resolve-Path -LiteralPath './adapter-patches/MaiBot-SnowLuma-Adapter/v0.1.1.patch').Path
git -C plugins/MaiBot-SnowLuma-Adapter apply --check $pokePatchPath
# 上一步通过后执行：
git -C plugins/MaiBot-SnowLuma-Adapter apply $pokePatchPath
```

仅下载 MaiBot fork 不会自动应用补丁。如果本地已有此次修改，不需要再次应用。不要覆盖现有配置；新开关默认值由插件配置模型提供。

## 验证

已用当前虚拟环境完成 Mock：群聊、私聊、不同用户及同用户连续事件、自身回显、其他目标、开关、平台失败；Ruff 与 diff 检查通过。另在基线干净临时目录校验补丁可应用且结果与本地五个源文件一致。未运行真实 QQ 回戳测试，机器人保持停止。
