# 本地与云端 v0.2.4 同步核对

远端 feature/qq-agent、v0.2.4 解引用与本地 HEAD 均为 535eda936df8cbfada8c0ff5f5ba976f9cfa83e9。没有缺失的已提交代码，没有本地未推送 commit；差异来自未提交工作区。

本次保密修复共 9 个已跟踪文件，尚未同步：src/config/official_configs.py、src/config/config.py、src/services/owner_control_service.py、三个语言的 owner_security.prompt、pytests/services/test_owner_control.py、主人使用文档和 changelogs/changelog.md。

源码默认不绑定主人；空账号不授权；安全提示不包含真实账号，旧自定义模板占位符仅填私密身份说明。测试改用虚构账号。全部受版本控制文件扫描，真实账号明文命中为 0。本地配置原绑定保留，实际配置受 Git 忽略。51 项测试、19 个子测试及 Ruff 通过。

原有本地差异：QQ_GROUP_WEIGHT_TEST_HANDOFF.md 未提交更新；12 个未跟踪文件为两个 QQ 交接文件、表情导入记录、基础分析、三批修复及最终审查、门控续轮分析、整合启动记录、参与分析、Planner 轮次统计和缓存分析。部分包含真实群信息或本机路径，未上传。本审计报告本身也仅在本地。

实际 bot_config.toml、model_config.toml、数据库、日志及表情运行元数据属于受忽略的本地数据。人格模板 config/qq-agent-personality.md 已同步；实际配置值不随仓库同步。

云端 v0.2.4 仍包含此次修复前的真实账号，旧提交和旧 Prompt 日志也可能保留。此任务未创建新 commit、推送或改写标签/历史。移除当前源码无法追溯收回已公开内容；Git 历史清理需要明确重写分支和标签，且不能保证清除其他人的 clone、fork 或缓存。

本机管理员或可读取实际配置的代码仍能获取真实绑定；认证逻辑必须能够使用它。本次实现公开源码和模型提示隔离，不声称抵抗任意本机代码执行。
