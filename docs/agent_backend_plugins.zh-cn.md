# Agent backend 插件

[English](agent_backend_plugins.md)

TelegramAgentBot 默认使用 `local` backend，在运行机器上直接管理 tmux 和 agent
transcript。只有 Telegram bot 与 agent 会话需要分布在不同机器时，才需要安装
backend 插件。

插件通过 `telegram_agent_bot.backends` Python entry point 注册，并实现会话创建、
消息发送、控制键、终端截取和事件订阅接口。需要支持 Telegram 目录选择器时，还应
实现远端根目录、目录列表和可恢复会话查询。

仓库内的 [socket backend](../plugins/socket_backend/README.md) 提供
`socket-cluster` 和 `telegram-agent-node`。它支持远端目录浏览、会话管理、文件上传
和 transcript 事件回传。

Socket 协议本身不提供公网 TLS 或身份认证。节点应绑定在 `127.0.0.1`，并通过 SSH
隧道或受控私有网络连接。完整接口、配置和插件设计约束见
[英文文档](agent_backend_plugins.md)。
