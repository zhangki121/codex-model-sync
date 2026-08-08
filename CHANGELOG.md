# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-08-08

### 新增

- 图形化桌面应用（tkinter），无需命令行操作，支持预览（只读）与执行同步
- 系统托盘后台运行：关闭/最小化窗口后驻留托盘继续监控
- 自动同步：检测到 Codex 退出或模型/供应商配置变化时自动执行同步
- 开机自启（监控模式）
- 自动备份，一键打开备份目录
- 同步定时任务（automation.toml）模型与会话 jsonl 的 model_provider 标签（可选）

### 优化

- 进程检测改用系统 API 直接枚举，不再每 2 秒启动 `tasklist` / `powershell` 子进程
- 子进程调用统一使用 `CREATE_NO_WINDOW`，不再弹出终端窗口

### 修复

- 应用自身进程名包含 "Codex" 被误判为 Codex 本体、导致自动同步被取消的问题

