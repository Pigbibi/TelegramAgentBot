# TelegramCodexBot

[English README](README.md)

> TelegramCodexBot 通过 Telegram 远程控制 Codex 会话。
> CLI / 包名是 `telegram-codex-bot`。

这个版本的目标很直接：通过 Telegram 远程控制真实运行在 tmux 里的 Codex 会话，让手机端和桌面端可以围绕同一个终端会话来回切换，而不是再起一个独立 SDK 会话。

## 功能概览

TelegramCodexBot 是一个通过 Telegram 远程控制 Codex 会话的工具：

- 新建窗口默认直接跑 `codex`
- 会话监控默认面向 `~/.codex` 下的现代 Codex transcript
- Telegram 转发、topic 隔离、清理流程按长时间跑 Codex 的方式做了加固
- 保留 tmux-first 的用法，手机和桌面都围绕同一个真实终端会话工作
- 默认后端是本机 tmux，同时预留插件接口给中心 bot / 远端 agent 节点模式
- 支持把 GitHub issue 中的结构化任务注入到 Codex tmux 会话

## 主要功能

- **Topic 级会话映射** —— 每个 Telegram topic 对应一个 tmux 窗口和一个活跃 Codex 会话
- **实时通知** —— 助手回复、thinking/commentary、tool use/result、本地命令输出都可以转发到 Telegram
- **交互式 UI 支持** —— AskUserQuestion、ExitPlanMode、权限提示可以直接在 Telegram 里点按钮操作
- **语音转文字** —— 语音消息可以通过 OpenAI 转录后继续发给 Codex
- **恢复已有会话** —— 在目录里挑已有 Codex session 继续跑
- **已关闭会话默认隐藏** —— topic 删除或清理后，对应 session 默认不再出现在 Resume 列表里，但 transcript 文件不会删
- **topic / 窗口清理** —— 对 stale topic、stale tmux 窗口和残留绑定做更稳的清理
- **额度 / 登录故障恢复** —— usage-limit 和登录失效会在 Telegram 明确提示；如已保存备用账号，也可以选择启用自动失败转移
- **持久化状态** —— thread bindings、display name、offset、monitor state 重启后还能保留
- **可插拔 agent backend** —— 默认仍是本机 tmux，高级用法可以通过插件接入中心 bot / 多 agent 节点

## 依赖前提

- 本机已安装 **tmux**
- 本机已安装并可正常使用 **Codex CLI**
- 你有一个已开启 topic/thread 模式的 **Telegram Bot**

## 安装

### 方式 1：直接从 GitHub 安装

```bash
# 用 uv
uv tool install git+https://github.com/Pigbibi/TelegramCodexBot.git

# 或 pipx
pipx install git+https://github.com/Pigbibi/TelegramCodexBot.git
```

### 方式 2：从源码安装

```bash
git clone https://github.com/Pigbibi/TelegramCodexBot.git
cd TelegramCodexBot
uv sync
```

## 新电脑快速部署（macOS）

新电脑或全新环境可以直接这样装：

```bash
git clone https://github.com/Pigbibi/TelegramCodexBot.git
cd TelegramCodexBot
chmod +x scripts/bootstrap-macos.sh
./scripts/bootstrap-macos.sh
```

脚本会做这些事：

- 执行 `uv sync`
- 如果 `~/.telegram-codex-bot/.env` 不存在，就从 `.env.example` 生成一份
- 在当前生效的 Codex home 里执行 `telegram-codex-bot hook --install`
- 生成可复用的 `~/.telegram-codex-bot/bin/telegram-codex-bot-launch`
- 生成一份 macOS 的 LaunchAgent plist

脚本跑完后，通常只需要处理这几项：

1. `TELEGRAM_BOT_TOKEN`
2. `ALLOWED_USERS`
3. 如果你要语音转文字，再补 `OPENAI_API_KEY`
4. 执行 `codex login`

这个项目**没有单独的 `GPT_SUBSCRIPTION=` 环境变量**。
它直接复用本机 Codex 登录态：

```bash
codex login
```

如果你平时有多账号切换，或需要在不 SSH 到服务器的情况下重新登录，可以直接用 Telegram 命令：

```text
/codexlogin          # 重新登录默认 CODEX_HOME
/codexlogin backup   # 登录并保存一个名为 backup 的账号
/codexaccount list
/codexaccount use backup
```

如果 `~/.telegram-codex-bot/.env` 里还是占位值，脚本只会写好 launchd 文件，
不会自动启动服务。改完 `.env` 后再手动执行：

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.telegramcodexbot.plist
launchctl kickstart -k "gui/$(id -u)/io.github.telegramcodexbot"
```

查看服务状态：

```bash
launchctl print "gui/$(id -u)/io.github.telegramcodexbot" | sed -n '1,40p'
tail -n 50 ~/.telegram-codex-bot/logs/telegram-codex-bot.err.log
```

## Linux / VPS 快速部署

如果目标机是 Linux 或带 systemd 的 VPS，可以直接这样装：

```bash
git clone https://github.com/Pigbibi/TelegramCodexBot.git
cd TelegramCodexBot
chmod +x scripts/bootstrap-linux.sh
./scripts/bootstrap-linux.sh
```

这个脚本会：

- 执行 `uv sync`
- 如果需要，从 `.env.example` 生成 `~/.telegram-codex-bot/.env`
- 执行 `telegram-codex-bot hook --install`
- 写入 `~/.telegram-codex-bot/bin/telegram-codex-bot-launch`
- 生成用户级 systemd service：`~/.config/systemd/user/io.github.telegramcodexbot.service`

后续步骤：

1. 修改 `~/.telegram-codex-bot/.env`
2. 执行 `codex login`
3. 如果脚本没有自动拉起服务，再手动执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now io.github.telegramcodexbot.service
```

如果是 VPS，希望重启后不用登录也能继续跑，再执行一次：

```bash
sudo loginctl enable-linger "$USER"
```

查看服务状态：

```bash
systemctl --user status io.github.telegramcodexbot.service --no-pager
tail -n 50 ~/.telegram-codex-bot/logs/telegram-codex-bot.err.log
```

## 配置

### 1）先创建 Telegram Bot

1. 去找 [@BotFather](https://t.me/BotFather)
2. 创建 bot，拿到 token
3. 打开 bot 设置的小程序
4. 开启 **Threaded Mode**

### 2）创建 `~/.telegram-codex-bot/.env`

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
TELEGRAM_CODEX_BOT_CODEX_COMMAND=codex
TELEGRAM_CODEX_BOT_AUTO_UPDATE=true
TELEGRAM_CODEX_BOT_SHOW_COMMENTARY_MESSAGES=true
```

多数情况下，真正需要手改的就这一份 `.env`。

### GitHub 桥接

如果你希望把 GitHub issue 交给 Codex 会话处理，请把桥接配置保留在
本机，并参考下面的模板文档：

- `docs/github_codex_bridge.md`
- `docs/github_codex_bridge.sample.json`

桥接器支持两种本地模式：

- `targets`：直接轮询一个或多个仓库，把每个 issue 交给对应的 tmux
  窗口
- `orchestrator`：消费控制面仓库发布的月度 issue，把任务转发给一个
  runner 窗口

真实配置应放在 `~/.telegram-codex-bot/github_codex_bridge.json`，不要提交到仓库。

### 必填变量

| 变量 | 说明 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | 从 @BotFather 获取的 bot token |
| `ALLOWED_USERS` | 允许控制 bot 的 Telegram 用户 ID，多个用逗号分隔 |

### 常用可选变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TELEGRAM_CODEX_BOT_DIR` | `~/.telegram-codex-bot` | 配置和状态目录 |
| `TELEGRAM_CODEX_BOT_BACKEND` | `local` | agent backend ID。默认 `local` 保持单机 tmux 行为 |
| `TELEGRAM_CODEX_BOT_BACKEND_PLUGINS` | _(空)_ | 可选 backend 插件模块，多个用逗号分隔 |
| `TELEGRAM_CODEX_BOT_TMUX_SESSION_NAME` | `telegram-codex-bot` | bot 使用的 tmux session 名称 |
| `TELEGRAM_CODEX_BOT_CODEX_COMMAND` | `codex` | 创建新窗口时运行的命令 |
| `TELEGRAM_CODEX_BOT_CODEX_PROJECTS_PATH` | `~/.codex` | transcript 扫描根目录 |
| `TELEGRAM_CODEX_BOT_DEFAULT_PROJECTS_PATH` | `~/Projects` | 创建新会话时默认展示的目录 |
| `TELEGRAM_CODEX_BOT_PROJECT_ROOTS` | _(空)_ | 可选：进入目录浏览前展示的命名根目录 |
| `TELEGRAM_CODEX_BOT_MONITOR_POLL_INTERVAL` | `2.0` | 轮询间隔，单位秒 |
| `TELEGRAM_CODEX_BOT_ENABLE_ACCOUNT_ROTATION` | `false` | 遇到 `usage_limit_exceeded` 后是否自动切到下一个已保存账号 |
| `TELEGRAM_CODEX_BOT_STATUS_POLL_INTERVAL` | `1.0` | 终端状态轮询间隔，单位秒；用于持续编辑 Telegram 里的 `Working (...)` 状态 |
| `TELEGRAM_CODEX_BOT_AGENT_INPUT_QUEUE_MAX_SIZE` | `20` | Codex 忙时，每个会话最多缓存多少条 Telegram 输入 |
| `TELEGRAM_CODEX_BOT_AGENT_INPUT_QUEUE_MAX_WAIT_SECONDS` | `1800` | Codex 长时间不回到可输入状态时，输入最多排队等待多少秒 |
| `TELEGRAM_CODEX_BOT_AUTO_UPDATE` | `false` | 启动时自动检查并 fast-forward 更新 git 源码安装 |
| `TELEGRAM_CODEX_BOT_UPDATE_INTERVAL_SECONDS` | `86400` | 两次自动更新检查之间的最短间隔 |
| `TELEGRAM_CODEX_BOT_UPDATE_REQUIRE_IDLE` | `true` | 仅在没有活跃 Codex pane 时应用自动更新 |
| `TELEGRAM_CODEX_BOT_UPDATE_BUSY_RETRY_SECONDS` | `300` | 因正在工作而延后更新时，多久后再检查一次空闲状态 |
| `TELEGRAM_CODEX_BOT_UPDATE_REMOTE` | git remote | 可选：指定用于更新的 git remote |
| `TELEGRAM_CODEX_BOT_UPDATE_BRANCH` | git branch | 可选：指定用于更新的 git branch |
| `TELEGRAM_CODEX_BOT_UPDATE_RUN_UV_SYNC` | `true` | git 更新成功后是否执行 `uv sync` |
| `TELEGRAM_CODEX_BOT_CODEX_UPDATE_CHECK` | `false` | 在空闲更新循环里检查 Codex CLI 的 npm 新版本 |
| `TELEGRAM_CODEX_BOT_CODEX_AUTO_UPDATE` | `false` | 空闲且发现新版本时，执行 `npm install -g @openai/codex@latest` |
| `TELEGRAM_CODEX_BOT_SHOW_COMMENTARY_MESSAGES` | `false` | 是否把 Codex commentary/thinking 转发到 Telegram |
| `TELEGRAM_CODEX_BOT_SHOW_HIDDEN_DIRS` | `false` | 目录浏览器里是否显示点目录 |
| `OPENAI_API_KEY` | _(空)_ | 语音转录使用 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 自定义 OpenAI 兼容接口 |

消息格式默认走 MarkdownV2，并在需要时自动降级为纯文本。

### 项目根目录

如果想在目录浏览前先选择电脑、VPS 或已挂载的工作区，可以配置命名根目录：

```ini
TELEGRAM_CODEX_BOT_PROJECT_ROOTS=Local=~/Projects,Remote=/mnt/remote-projects
```

只要设置了 `TELEGRAM_CODEX_BOT_PROJECT_ROOTS`，新 Telegram topic 第一次发消息后都会先显示
电脑/VPS 选择器，即使只配置了一个根目录。选中后才进入原来的目录浏览，并且不会
浏览到该根目录之外。其他电脑或 VPS 需要先从运行 telegram-codex-bot 的机器挂载成本地路径，
例如 SSHFS 或 NFS。

### Agent Backend

TelegramCodexBot 默认使用 `local` backend，也就是当前稳定的单机模式：
Telegram 控制本机 tmux session，bot 读取本机 Codex transcript。

单机模式是默认支持路径。backend 接口是为了把“中心 bot + 远端 agent 节点”这类
多端能力做成可选插件，不影响普通本机使用。

如果暂时不跑远端 agent 节点，`TELEGRAM_CODEX_BOT_BACKEND` 保持不填或设为
`local` 即可。不需要安装 `plugins/socket_backend`，也不需要启动
`telegram-codex-agent-node` 或配置 socket 节点地址。

```ini
TELEGRAM_CODEX_BOT_BACKEND=local
```

插件模块示例：

```ini
TELEGRAM_CODEX_BOT_BACKEND=socket-cluster
TELEGRAM_CODEX_BOT_BACKEND_PLUGINS=telegram_codex_bot_socket_backend
TELEGRAM_CODEX_BOT_SOCKET_NODES=macbook=127.0.0.1:8765
```

仓库内置了一个可选 socket backend package：`plugins/socket_backend/`。它提供
`socket-cluster` 中心 backend 和 `telegram-codex-agent-node` 远端节点 CLI。
设计和运行说明记录在 `docs/agent_backend_plugins.md`。

核心 backend 接口覆盖这些操作：`prepare()`、`start(message_callback)`、
`stop()`、`create_session()`、`send_message()`、`send_control()`、`capture()`。
backend 也可以实现可选浏览能力，用于远端 root 选择、目录浏览和 resume-session 查询。
默认 `local` backend 会把这些操作转给现有 tmux、session manager 和 transcript monitor。

本地 target 会同时写入旧版 `thread_bindings` 和新版 `thread_targets`，方便回滚；
非本地 backend 可以用 `backend_id`、`node_id`、`session_id` 来定位远端 agent，
不必依赖 tmux window id。

当前仓库不再内置旧项目的演示截图或视频，README 也不嵌入旧媒体。

### 更新

如果是通过 bootstrap 脚本部署的源码安装，可以在 `.env` 里开启：

```ini
TELEGRAM_CODEX_BOT_AUTO_UPDATE=true
```

bot 运行期间会按 `TELEGRAM_CODEX_BOT_UPDATE_INTERVAL_SECONDS` 周期检查配置的 git remote。
默认 `TELEGRAM_CODEX_BOT_UPDATE_REQUIRE_IDLE=true`，所以更新前会先确认 Telegram
发送队列为空，并且没有 Codex tmux pane 正在工作或等待交互输入。如果还有任务，
就按 `TELEGRAM_CODEX_BOT_UPDATE_BUSY_RETRY_SECONDS` 延后再检查。

如果工作区是干净的，并且可以 fast-forward，就执行 `git pull --ff-only`，
随后执行 `uv sync` 并重启 telegram-codex-bot 自身，让新代码生效。已有 Codex tmux
窗口和对话不会被 kill。

手动命令：

```bash
telegram-codex-bot update --check
telegram-codex-bot update
telegram-codex-bot codex-update --check
telegram-codex-bot codex-update
telegram-codex-bot --version
```

自更新只处理 git checkout；`pipx install` 或 `uv tool install` 这类非源码安装会跳过。有本地改动的 checkout 也会跳过，避免覆盖你的修改。

Codex CLI 检查和 telegram-codex-bot 自更新是分开的。示例 `.env` 会开启
`TELEGRAM_CODEX_BOT_CODEX_UPDATE_CHECK=true`，它只会报告 npm 是否有新版本；只有你确认
运行 telegram-codex-bot 的用户有权限更新全局 npm 包时，才建议打开
`TELEGRAM_CODEX_BOT_CODEX_AUTO_UPDATE=true`。否则可以用手动命令在合适权限下更新。

### 非交互服务器 / VPS 场景

如果你在服务器上跑 Codex，不希望在终端里停在审批提示：

```ini
TELEGRAM_CODEX_BOT_CODEX_COMMAND=IS_SANDBOX=1 codex --dangerously-bypass-approvals-and-sandbox
```

## 多账号登录、切换与额度失败转移

默认情况下，新 session 使用服务用户正常的 `~/.codex` 登录，并且**不会自动切换账号**。这样单账号部署最可控。

Telegram 命令：

```text
/codexlogin          # 给默认 CODEX_HOME 发起 Codex device login
/codexlogin backup   # 登录到隔离账号 home，并保存为 backup
/codexaccount list
/codexaccount use backup
/codexaccount clear  # 回到服务用户默认 CODEX_HOME
```

命名账号保存在 `~/.telegram-codex-bot/accounts/`。切换只影响新创建的 topic；已有 topic 仍绑定原 tmux window。如果希望当前 topic 使用新账号，请先 `/unbind`，再重新发消息创建新 session。

如果确实需要额度自动失败转移，可以设置 `TELEGRAM_CODEX_BOT_ENABLE_ACCOUNT_ROTATION=true`。当某个 live session 产生 `usage_limit_exceeded` 时，TelegramCodexBot 会把这个窗口标记为已耗尽；下一条消息到来时，可以在下一个已保存账号上新开 tmux 窗口，并把消息转发过去。这属于 **切到新 session**，不是把原 session 无缝续活。

## 会话追踪

默认会扫描 `~/.codex` 下的 Codex transcript。

如果你想启用自动 session 追踪 hook，可以执行：

```bash
telegram-codex-bot hook --install
```

这个命令会在当前生效的 Codex home 里启用 hooks：

- 如果设置了 `CODEX_HOME`，就写到 `$CODEX_HOME/config.toml` 和 `$CODEX_HOME/hooks.json`
- 否则写到默认的 `~/.codex/config.toml` 和 `~/.codex/hooks.json`

等价的手动配置如下。

`~/.codex/config.toml`

```toml
[features]
codex_hooks = true
```

`~/.codex/hooks.json`

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "telegram-codex-bot hook",
            "statusMessage": "Registering Codex session",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

hook 会把窗口和 session 的映射写到 `$TELEGRAM_CODEX_BOT_DIR/session_map.json`，这样清上下文或重启后，bot 仍然能更稳地把 tmux 窗口和 Codex session 对上。

## 使用方式

```bash
# 安装成工具后
telegram-codex-bot

# 从源码运行
uv run telegram-codex-bot
```

### Bot 命令

| 命令 | 说明 |
| --- | --- |
| `/start` | 显示欢迎信息 |
| `/history` | 查看当前 topic 的消息历史 |
| `/screenshot` | 抓取当前终端截图 |
| `/esc`, `/interrupt` | 给 Codex 发送 Escape |
| `/kill` | 杀掉绑定的 tmux 窗口并清理 topic 绑定 |
| `/unbind` | 解绑 topic，但不杀当前 tmux 窗口 |
| `/usage` | 打开 Codex 的 usage 界面并回传解析结果 |
| `/codexlogin [name]` | 从 Telegram 发起 Codex device login |
| `/codexaccount` | 查看、保存、选择或清除 Codex 账号 |

### 会转发给 Codex 的 slash 命令

| 命令 | 说明 |
| --- | --- |
| `/clear` | 清上下文 |
| `/compact` | 压缩上下文 |
| `/cost` | 查看 token / cost |
| `/help` | 查看 Codex 帮助 |
| `/memory` | 编辑 AGENTS.md |
| `/model` | 切换模型 |

其他未知 slash 命令会原样转发给 Codex。

## Topic 工作流

**1 个 topic = 1 个 tmux 窗口 = 1 个活跃会话。**

### 从 Telegram 新建会话

1. 在 Telegram 里创建一个新 topic
2. 发任意一条消息
3. 在目录浏览器里选目录
4. 选择恢复已有 session 或创建新 session
5. TelegramCodexBot 创建 tmux 窗口，并把你刚发的消息转进去

如果 bot 在这个目录下发现已有的**可追踪** tmux 窗口，也可以直接给你选来绑定。
没有可靠 session 映射的窗口会被故意跳过，避免 topic 误绑到一个之后回不来消息的终端。

### 持续工作

topic 绑定以后，直接继续发文字或语音消息就行。

### 结束工作

你可以：

- 直接关闭 / 删除 Telegram topic
- 用 `/kill`
- 或者用 `/unbind` 只解绑，不杀 tmux 窗口

如果你关闭 / 删除 topic，或者 bot 在清理死 topic / 死窗口，对应的
Codex session 会默认从 Resume 列表里隐藏；底层 transcript 仍然保留在
`~/.codex` 里，不会直接删除。

## 通知内容

监控器会轮询 transcript，并可转发：

- 助手回复
- commentary / thinking 输出
- tool use 和 tool result
- 本地命令输出
- tmux 里已经公开可见的过程进度，比如 `Explored`、`Ran`、`Searched`、`Searching the web`
- 额度耗尽事件

这里的“过程进度”只来自终端里已经显示出来的公开文本，不会把模型隐藏推理整段透出来。

## 手动在 tmux 里运行 Codex

```bash
tmux attach -t telegram-codex-bot
tmux new-window -n myproject -c ~/Code/myproject
codex
```

窗口需要运行在配置好的 `telegram-codex-bot` tmux session 里。

## 数据存储

| 路径 | 说明 |
| --- | --- |
| `$TELEGRAM_CODEX_BOT_DIR/state.json` | thread 绑定/target、窗口状态、display name、offset，以及已隐藏的关闭会话 ID |
| `$TELEGRAM_CODEX_BOT_DIR/session_map.json` | hook 生成的 tmux window ↔ session 映射 |
| `$TELEGRAM_CODEX_BOT_DIR/monitor_state.json` | monitor 的 byte offset |
| `$TELEGRAM_CODEX_BOT_DIR/pending_topic_deletions.json` | 本地清理后延迟执行的 topic 删除队列 |
| `~/.codex/` | Codex transcript 根目录（只读） |
| `~/.telegram-codex-bot/accounts/` | 可选的账号 home 和快照目录 |

## 目录结构

```text
src/telegram_codex_bot/
├── __init__.py
├── account_manager.py
├── agent_io.py
├── backends/
├── bot.py
├── bridge.py
├── config.py
├── hook.py
├── main.py
├── markdown_v2.py
├── monitor_state.py
├── screenshot.py
├── session.py
├── session_monitor.py
├── terminal_parser.py
├── tmux_manager.py
├── transcribe.py
├── transcript_parser.py
├── utils.py
└── handlers/
```

## 许可证

本项目以 MIT 许可证发布。
版权和许可证声明保留在 `LICENSE` 文件中。
随包字体保留各自的许可证文件，位置在 `src/telegram_codex_bot/fonts/`。
