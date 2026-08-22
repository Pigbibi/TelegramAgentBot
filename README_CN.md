# TelegramAgentBot

[English](README.md)

[![Check](https://github.com/Pigbibi/TelegramAgentBot/actions/workflows/check.yml/badge.svg)](https://github.com/Pigbibi/TelegramAgentBot/actions/workflows/check.yml)
[![Secret Scan](https://github.com/Pigbibi/TelegramAgentBot/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/Pigbibi/TelegramAgentBot/actions/workflows/secret-scan.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

通过 Telegram 控制 Codex CLI 和 Claude Code，同时保留原来的终端工作流。
TelegramAgentBot 把每个 Telegram 论坛话题映射到一个 tmux 窗口，将消息发送给
所选 agent，并把会话中的公开输出转发回 Telegram。

tmux 是会话的事实来源。你可以随时在电脑上重新连接同一个终端；机器人重启也
不会丢失底层 CLI 会话。

## 主要功能

- 一个 Telegram 话题对应一个 tmux 窗口和一个 agent 会话。
- 直接在 Telegram 中选择 Codex 或 Claude Code、模型和推理级别。
- 向实时会话发送文本、语音、图片、文件和控制键。
- 接收 agent 回复、公开进度、工具摘要、命令输出和交互式提示。
- 恢复已追踪会话，并在机器人重启后保留话题绑定。
- agent 忙碌时持久化排队输入，并为小型服务器限制并发。
- 查看主机健康状态，并接收带冷却时间的运维告警。
- 默认使用本机 tmux；也可以安装 socket backend 连接远端 agent 节点。
- 通过可选 GitHub bridge 把 issue 派发给正在运行的 Codex 窗口。

## 工作原理

```text
Telegram 论坛话题
        │
        ▼
TelegramAgentBot ─── 持久化绑定和输入队列
        │
        ▼
tmux 窗口 ─── Codex CLI 或 Claude Code
        │
        ▼
agent transcript ─── 公开输出返回 Telegram
```

TelegramAgentBot 控制的是真实 CLI 进程，不会另建一套 SDK 会话，也不会暴露模型
的私有推理内容。

## 运行要求

- Python 3.12 或更高版本
- 推荐使用 [uv](https://docs.astral.sh/uv/) 安装
- tmux
- 已为服务用户安装并完成认证的 Codex CLI 或 Claude Code
- 启用了 Threaded Mode 的 Telegram bot
- 使用部署脚本时：Linux 需要 systemd 用户服务，macOS 需要 launchd

## 快速开始

### 1. 创建 Telegram bot

1. 在 Telegram 打开 [@BotFather](https://t.me/BotFather)。
2. 创建 bot 并复制 token。
3. 打开 bot 设置，启用 **Threaded Mode**。
4. 获取允许控制机器的 Telegram 数字用户 ID。

只有列入 `ALLOWED_USERS` 的用户才能操作 TelegramAgentBot。

### 2. 安装

Linux 或 VPS：

```bash
mkdir -p ~/.telegram-agent-bot/app
git clone https://github.com/Pigbibi/TelegramAgentBot.git \
  ~/.telegram-agent-bot/app/TelegramAgentBot
cd ~/.telegram-agent-bot/app/TelegramAgentBot
./scripts/bootstrap-linux.sh
```

macOS：

```bash
git clone https://github.com/Pigbibi/TelegramAgentBot.git
cd TelegramAgentBot
./scripts/bootstrap-macos.sh
```

部署脚本会安装依赖、创建应用目录、安装会话追踪 hook，并生成对应平台的服务
配置。已有的配置文件不会被覆盖。

Linux 服务的代码目录不要放在 agent 可以浏览或清理的项目根目录中，例如
`~/Projects`。Linux 部署脚本会检查这项边界。

### 3. 配置

编辑 `~/.telegram-agent-bot/.env`：

```ini
TELEGRAM_BOT_TOKEN=替换为你的_bot_token
ALLOWED_USERS=123456789

TELEGRAM_AGENT_BOT_AGENT_TYPE=codex
TELEGRAM_AGENT_BOT_TMUX_SOCKET_NAME=telegram-agent-bot
TELEGRAM_AGENT_BOT_DEFAULT_PROJECTS_PATH=~/Projects
```

如果默认使用 Claude Code：

```ini
TELEGRAM_AGENT_BOT_AGENT_TYPE=claude
TELEGRAM_AGENT_BOT_CLAUDE_COMMAND=claude
```

带注释的配置模板见 [`.env.example`](.env.example)，支持的设置和运维说明见
[配置说明](docs/configuration.md)。

### 4. 登录 agent CLI

用运行 TelegramAgentBot 的同一个系统用户执行：

```bash
codex login
```

使用 Claude Code 时，请按其支持的认证方式完成配置，并先确认 `claude` 能在普通
终端中正常启动。

### 5. 启动服务

Linux：

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramagentbot.service
systemctl --user status io.github.telegramagentbot.service --no-pager
```

如果 VPS 在用户未登录时也要运行服务，请启用 linger：

```bash
sudo loginctl enable-linger "$USER"
```

macOS：

```bash
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/io.github.telegramagentbot.plist
launchctl kickstart -k "gui/$(id -u)/io.github.telegramagentbot"
```

日志、升级、手动安装和服务排错见[部署说明](docs/deployment.md)。

## 使用方法

1. 在 Telegram 聊天中创建一个论坛话题。
2. 在话题中发送消息。
3. 选择项目目录。
4. 选择已有的已追踪会话，或创建新会话。
5. 按提示选择 agent、模型、推理级别和 Fast mode。
6. 在同一个话题中继续发送文本或语音消息。

绑定关系始终是：

```text
一个话题 = 一个 backend target = 一个活跃 agent 会话
```

使用 `/unbind` 可以解除 Telegram 绑定但保留 tmux 窗口；使用 `/kill` 会停止窗口
并删除绑定。

### Telegram 命令

| 命令 | 用途 |
| --- | --- |
| `/start` | 显示欢迎信息 |
| `/history` | 查看当前话题的消息历史 |
| `/mode [clean\|trace]` | 选择精简输出或公开工具轨迹 |
| `/screenshot` | 截取当前终端画面 |
| `/esc`、`/interrupt` | 向 agent 发送 Escape |
| `/kill` | 停止绑定窗口并删除话题绑定 |
| `/unbind` | 解除绑定但保留窗口运行 |
| `/usage` | 查看 Codex 用量信息 |
| `/health` | 查看主机和 AgentBot 健康快照 |
| `/agentlogin [name]` | 登录默认 agent |
| `/agentaccount` | 查看、保存、选择或清除 agent 账号 |
| `/codexlogin`、`/codexaccount` | 管理 Codex 登录 |
| `/claudelogin`、`/claudeaccount` | 管理 Claude Code 登录 |
| `/agentcmd`、`/cmd` | 转发任意 agent slash 命令 |

`/clear`、`/compact`、`/goal`、`/help`、`/memory`、`/model` 等命令会转发给
当前 CLI 会话。

## 安全边界

TelegramAgentBot 可以代替用户向终端输入内容，应按接近远程 Shell 的级别保护：

- `ALLOWED_USERS` 只填写可信用户的数字 ID。
- `.env`、账号快照和 bridge 配置应放在 Git 仓库之外，并限制为所有者可读。
- 不要把 tmux socket 或 socket backend 直接暴露到公网。
- 远端节点使用 SSH 隧道或私有网络连接。
- 启用无人值守运行前，先检查 agent 的审批和 sandbox 设置。
- 启用自动账号轮换前，先在目标主机上验证备用账号切换。

安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。不要在公开 issue 中提交 token、
账号凭证、私有 prompt 或漏洞利用细节。

## 文档

- [文档索引](docs/README.md)
- [功能说明](docs/features.md)
- [配置说明](docs/configuration.md)
- [部署与升级](docs/deployment.md)
- [Agent backend 插件](docs/agent_backend_plugins.md)
- [Socket backend](plugins/socket_backend/README.md)
- [GitHub issue bridge](docs/github_codex_bridge.md)
- [VPS 清理定时器](docs/vps_cleanup.md)

## 贡献与支持

欢迎提交可复现的 bug 报告和范围明确的 pull request。贡献前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)，需要帮助时请按 [SUPPORT.md](SUPPORT.md)
选择渠道。参与社区时请遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 许可证

TelegramAgentBot 使用 [MIT License](LICENSE)。`src/telegram_agent_bot/fonts/`
下的字体继续遵循各自的许可证。
