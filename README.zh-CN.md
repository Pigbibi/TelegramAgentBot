# TelegramAgentBot

[English](README.md)

TelegramAgentBot 将 Telegram 群组话题连接到 tmux 中运行的智能体会话。你可以在 Telegram 里选择项目、新建或恢复会话、发送消息和文件、回答交互问题，并查看智能体的公开输出。同一个会话也能在服务器终端中继续使用。

支持 **Codex**、**Claude Code**（第三方 API 或官方登录）和 **Cursor Agent**。只要服务用户安装并认证了相应 CLI，同一部署就能提供这些选项。

## 运行要求

- Python 3.12+、[uv](https://docs.astral.sh/uv/) 和 tmux
- 至少一个已安装并由服务用户完成认证的智能体 CLI
- 已开启 Topics 的 Telegram 超级群组及其中的 Bot
- 用于 `ALLOWED_USERS` 的 Telegram 数字用户 ID

仓库提供 Linux systemd 和 macOS launchd 服务配置。Bot 能操作真实终端，只应允许有权使用这些项目和 CLI 凭据的人访问。

## 安装

Linux：

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git \
  ~/.telegram-agent-bot/app/TelegramAgentBot
cd ~/.telegram-agent-bot/app/TelegramAgentBot
./scripts/bootstrap-linux.sh
```

macOS 克隆仓库后运行 `./scripts/bootstrap-macos.sh`。安装脚本会安装依赖和会话 hook，并创建服务定义；已有配置文件会保留。

参考 [`.env.example`](.env.example) 编辑 `~/.telegram-agent-bot/.env`，至少设置：

| 变量 | 值 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather 提供的 Token |
| `ALLOWED_USERS` | 可信操作人的数字用户 ID，多个用逗号分隔 |
| `TELEGRAM_AGENT_BOT_AGENT_TYPE` | 默认 CLI：`codex`、`claude`、`claudeofficial` 或 `cursor` |
| `TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH` | 项目选择器显示的目录 |

启动 Linux 服务：

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
```

macOS 启动、Linux 退出登录后常驻、升级和日志见[部署说明](docs/deployment.md)。服务源码应放在智能体不会编辑的目录中。

## 选择智能体

| 智能体 | 登录与配置 | 运行中输入 |
| --- | --- | --- |
| Codex | 认证 `codex` CLI | 可引导当前回合，或使用 CLI 原生队列安排下一回合 |
| Claude Code API（`claude`） | 在仅文件所有者可读的 `claude.env` 中配置兼容服务 | 可引导当前回合；后续输入保存在 AgentBot 持久队列中 |
| Claude Code 官方模式（`claudeofficial`） | 使用 `claude` CLI 自身的登录，不加载 `claude.env` | 可引导当前回合；后续输入保存在 AgentBot 持久队列中 |
| Cursor Agent（`cursor`） | 用服务用户运行 `agent login` | 可引导当前回合，或使用 CLI 原生队列安排下一回合 |

创建话题会话时，可以选择智能体及该 CLI 支持的模型设置。Cursor 凭据保存在 Cursor CLI 中；Codex 和 Claude Code 可以使用 AgentBot 的账户快照。命令、模型、权限和项目根目录见[配置说明](docs/configuration.md)。

## 使用话题

1. 在 Telegram 超级群组中发送消息，然后选择项目及新会话或已有会话。
2. 选择智能体和可用设置。
3. 在同一话题中继续发送文字、语音、图片或文件。首条消息中的附件会保留到会话准备好再发送。

常用命令：

| 命令 | 用途 |
| --- | --- |
| `/steer <内容>` | 引导当前回合 |
| `/queue <内容>` | 排队发送后续输入 |
| `/interrupt [内容]` | 中断，可附带替换输入 |
| `/history` | 查看话题历史 |
| `/health` | 查看 Bot 和主机健康状态 |
| `/unbind` | 解除话题绑定，保留终端 |
| `/kill` | 停止绑定窗口并解除绑定 |

Bot 重启后会保留话题绑定和待发送输入。空闲 tmux 窗口可能暂停，下一条消息到来时再恢复。如果输入已送到 CLI，却没有得到 transcript 确认，AgentBot **不会自动重发**：先检查智能体会话，确认它没有收到后再手动发送。完整命令和消息行为见[功能说明](docs/features.md)。

## 安全运行

- 仅让服务用户读取 `.env`、账户文件和 `$TELEGRAM_AGENT_BOT_DIR`。
- 操作人需要独立项目目录或 CLI 凭据时，使用不同的 Bot Token 和独立部署。
- 通过 SSH 或私有网络访问 tmux 与可选后端 socket。
- 升级或重启前检查正在运行的任务和待发送输入。

无需改动会话即可检查路由：

```bash
telegram-agent-bot doctor --json
```

`doctor` 通过只说明路由状态正常，不代表智能体任务已完成。日志和恢复步骤见[部署说明](docs/deployment.md#troubleshooting)。

## 文档与开发

- [文档索引](docs/README.md) · [功能](docs/features.md) · [配置](docs/configuration.md) · [部署](docs/deployment.md)
- [后端插件](docs/agent_backend_plugins.md) · [Socket 后端](plugins/socket_backend/README.md)
- [GitHub Issue 桥接](docs/github_codex_bridge.md) · [VPS 清理定时器](docs/vps_cleanup.md)

开发时运行 `uv sync --extra dev`，再阅读[贡献指南](CONTRIBUTING.md)。使用帮助和漏洞报告见[支持](SUPPORT.md)及[安全说明](SECURITY.md)。

项目使用 [MIT 许可证](LICENSE)。内置字体保留 `src/telegram_agent_bot/fonts/` 中各自的许可证。
