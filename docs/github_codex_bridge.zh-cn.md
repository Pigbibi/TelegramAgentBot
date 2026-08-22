# GitHub issue bridge

[English](github_codex_bridge.md)

`telegram-agent-bridge` 轮询 GitHub issue，把符合条件的 issue 转成结构化任务，并
发送到已经运行 Codex 的 tmux 窗口。它是独立任务注入器，不替代 TelegramAgentBot
的会话监控。

支持两种配置：

- `targets`：每个仓库分别配置 issue 过滤条件和目标 tmux 窗口；
- `orchestrator`：从一个控制仓库读取完整任务，发送给一个 runner 窗口。

真实配置必须保存在仓库之外，例如
`~/.telegram-agent-bot/github_codex_bridge.json`，并设置为所有者可读。启用持续轮询
前，先用 `--dry-run` 检查生成的 prompt，再用 `--once` 验证仓库权限和 tmux 目标。

自动合并应保持显式 opt-in，并由 label、PR、GitHub 必需检查和独立 merge gate
共同约束。完整字段和命令见[英文文档](github_codex_bridge.md)。
