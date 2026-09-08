# TelegramAgentBot

[English](README.md)

通过 Telegram 控制正在运行的 Codex CLI 和 Claude Code 会话。每个话题对应一个 tmux 窗口，方便远程发送指令，也能在本机连接同一个终端。

机器人转发公开回复、进度和交互提示。重启机器人后话题绑定可以恢复；底层 tmux 会话独立于机器人进程存在。

## 运行要求

- Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 tmux。
- 服务用户已安装并认证 Codex CLI 或 Claude Code。
- 已开启 threaded mode 的 Telegram bot，以及受限的 `ALLOWED_USERS` 列表。
- 配套服务脚本支持 Linux/systemd 和 macOS/launchd。

## 快速开始

Linux 或 VPS：

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git \
  ~/.telegram-agent-bot/app/TelegramAgentBot
cd ~/.telegram-agent-bot/app/TelegramAgentBot
./scripts/bootstrap-linux.sh
```

macOS 克隆后运行 `./scripts/bootstrap-macos.sh`。安装脚本配置依赖、hook 和服务定义，并保留已有配置文件。

参考[配置模板](.env.example)，编辑 `~/.telegram-agent-bot/.env`：

| 配置 | 用途 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather 提供的机器人凭据 |
| `ALLOWED_USERS` | 允许操作的 Telegram 数字用户 ID |
| `TELEGRAM_AGENT_BOT_AGENT_TYPE` | `codex` 或 `claude` |
| `TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH` | 机器人展示的项目目录 |
| `TELEGRAM_AGENT_BOT_TMUX_SOCKET_NAME` | 独立 tmux socket 名称 |

用同一系统用户完成 agent CLI 认证，再启动 Linux 服务：

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
```

macOS 启动、Linux 退出登录后常驻、日志和升级方法见[部署说明](docs/deployment.md)。服务源码应与 agent 任务会管理的目录分开存放。

## 使用会话

1. 在 Telegram 话题内发送消息。
2. 选择项目，以及已有会话或新建会话。
3. 选择 agent 和可用的模型设置。
4. 在同一话题中继续发送文字、语音、图片或文件。

| 命令 | 操作 |
| --- | --- |
| `/steer <内容>` | 引导当前回合 |
| `/queue <内容>` | 排队发送后续输入 |
| `/interrupt [内容]` | 中断，可附带替换指令 |
| `/esc` | 发送 Escape 并丢弃未发送输入 |
| `/history` | 查看话题历史 |
| `/health` | 查看主机和机器人健康状态 |
| `/unbind` | 解除话题绑定，保留终端 |
| `/kill` | 停止绑定窗口并解除绑定 |

每个 bot 状态目录使用一个 Telegram 聊天。不同聊天中的话题 ID 可能重复，机器人会拒绝冲突的跨聊天绑定。完整命令、认证与队列行为见[功能说明](docs/features.md)。

## 运维与安全

机器人可以操作真实终端。请限制允许用户，保护 `.env` 和状态目录，并检查 agent 权限设置。tmux 和可选后端 socket 应通过 SSH 或私有网络访问。

无需重启会话即可检查路由：

```bash
telegram-agent-bot doctor --json
```

升级或重启前检查活动任务和待发送输入。健康检查通过不等于 agent 任务已经完成。

## 文档

- [配置](docs/configuration.md) · [部署与升级](docs/deployment.md)
- [功能与命令](docs/features.md) · [文档索引](docs/README.md)
- [后端插件](docs/agent_backend_plugins.md) · [Socket 后端](plugins/socket_backend/README.md)
- [GitHub Issue 桥接](docs/github_codex_bridge.md) · [VPS 清理](docs/vps_cleanup.md)

开发时先运行 `uv sync --dev`，再执行[贡献指南](CONTRIBUTING.md)中的检查。内置字体保留 `src/telegram_agent_bot/fonts/` 下的各自许可证。

## 支持与贡献

[问题与支持](SUPPORT.md) · [贡献指南](CONTRIBUTING.md) · [安全问题](SECURITY.md) · [行为准则](CODE_OF_CONDUCT.md)

## 许可证

[MIT](LICENSE)。
