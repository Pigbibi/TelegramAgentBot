# VPS 清理定时器

[English](vps_cleanup.md)

可选清理定时器用于小型 Linux 主机，只处理明确列出的可重建缓存、临时文件和
GitHub Actions runner 产物。它不是通用磁盘清理器。

安装前先预览：

```bash
./scripts/install-vps-cleanup.sh
~/.telegram-agent-bot/bin/telegram-agent-cleanup --dry-run --force
```

清理器会保护 AgentBot 应用目录和自定义保护路径。Runner 正在执行任务时不会删除
`_work`，也不会删除 Runner 注册文件。uv 缓存只通过有超时的 `uv cache prune`
处理；运行中进程引用该缓存时会直接跳过。

启用 timer 前，应核对发现的 Runner 根目录、transcript 保留时间和所有自定义保护
路径。完整阈值、范围和 systemd 命令见[英文文档](vps_cleanup.md)。
