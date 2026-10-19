# Dashboard 旧主题问题排查与修复

## 实际根因

本机 WebUI 启动环境 MAIBOT_WEBUI_USE_LOCAL_DASHBOARD 未设置。修改前 _is_local_dashboard_enabled 只认显式 true，故 _resolve_static_path 实际选择 G:/QQbot/MaiBot/.venv/Lib/site-packages/maibot_dashboard/dist（pip 版本 1.8.4），而非项目源码构建。项目 dashboard/dist/index.html 最后构建时间为 2026-10-13 06:09:12，后续源码改动没有进入这份旧产物。因此即使启用本地资源，旧构建也不能显示最新修改。

源码 main.tsx 已传 defaultTheme=light，但 ThemeProvider 自身原默认是 system；本次统一为 light。已有有效 light/dark/system 的 localStorage 选择仍优先，不清空或重置偏好。非法值才使用默认。

本机 webui.webui_style=0，代表 modern 视觉风格，区别于浅色/深色模式。前端原有后台同步会覆盖 dashboardStyle，但不覆盖 themeMode。此次保持该协议与行为，不把它误判为深色覆盖。

当前没有可连接的旧浏览器页面。测试浏览器显示两端 light+modern；受浏览器只读接口限制，无法直接读取原浏览器 localStorage，未断言用户原偏好一定为空。现有偏好保持不变，通过主题切换 UI 可自行选择。

## 改动

- src/webui/app.py：未设置环境变量时自动优先本地有效 dist；显式 0/false/no/off 恢复 pip 包。无本地构建时提示并兼容包回退。INFO 记录 source/path/env，源码指纹不匹配或缺少指纹时警告重建。入口和 dashboard-build.json 使用 no-cache，哈希 assets 保持 immutable。
- dashboard/vite.config.ts：正常 build 自动生成 dashboard-build.json，记录源码/公共资源/构建配置及锁文件的 SHA256 指纹和构建时间。
- dashboard/src/components/theme-provider.tsx：统一 light 默认且验证已有模式值，不改变风格配置同步或用户自选。
- pytests/webui/test_app.py：本地优先、显式包回退、指纹及过期构建检测；旧测试对没有 index 的 dist 仍要求包回退，未删除断言。
- dashboard/src/components/__tests__/theme-background.test.tsx：保留显式 system 行为测试，新增新用户浅色和已有深色偏好保留。

## 构建与现场验证

依赖按 bun.lock 使用与 CI 相同的 Bun 1.2.1、frozen-lockfile 安装；未改变锁文件。前端 tsc -b 与 vite build 成功，存在既有大分块提示，不是构建错误。

16 项主题相关 Vitest 通过。后端相关 pytest 最终 14 通过、1 跳过。Ruff 通过。

临时单独启动 WebUI 后端 8001 和 Vite 7999，没有启动 bot.py 或 QQ 网关。浏览器实际根节点两端均 class=light、data-dashboard-style=modern；7999 返回 /@vite/client 开发入口，8001 返回的 index 原始字节与本地 dist 完全一致；构建指纹 JSON 与本地相同，Python 与 Node 指纹算法比对一致。启动日志明确 source=local，path=G:/QQbot/MaiBot/dashboard/dist，env=auto，构建与源码一致。

Dockerfile 已采用 Bun 构建阶段、复制项目 dist 并显式 MAIBOT_WEBUI_USE_LOCAL_DASHBOARD=1；本次没有 Docker 环境或运行容器，未将 Docker 旧镜像认定为本机根因，也未完成镜像运行测试。旧镜像需要按现有 Dockerfile 重新构建后才包含这些修改。

## 使用方式

源码、public、package.json、bun.lock 或构建配置变化后，执行 cd dashboard 然后 bun install --frozen-lockfile，再 bun run build；仅启动 Vite 开发页不更新生产 dist。下一次启动日志会显示来源及是否需重建，无需清空配置或浏览器全部缓存。

如需明确本地选择，设置 MAIBOT_WEBUI_USE_LOCAL_DASHBOARD=1；如需已安装发行包，设为 0。后台已有风格与浏览器已有颜色模式保持兼容。临时浏览器与 7999/8001 测试进程已关闭，机器人仍未启动。本次未提交或推送。
